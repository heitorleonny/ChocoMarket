"""End-to-end HTTP flows for order creation on an isolated database."""

from __future__ import annotations

from itertools import count
from uuid import uuid4

import pytest

from app.common.types import ProductStatusEnum
from app.orders.model import OrderModel
from app.products.model import ProductModel
from tests.support import ApiHarness, assert_problem, bearer

_keys = count()


def _headers(user, key: str | None = None) -> dict[str, str]:
    return {**bearer(user), "Idempotency-Key": key or f"orders-create-{next(_keys):08d}"}


def _items(*lines: tuple[object, int]) -> dict:
    return {
        "items": [
            {"product_id": str(getattr(product, "id", product)), "quantity": quantity}
            for product, quantity in lines
        ]
    }


def test_multi_item_order_snapshots_prices_and_reserves_stock(api: ApiHarness) -> None:
    customer = api.create_user()
    admin = api.create_admin()
    dark = api.create_product(price_cents=1_590, stock_quantity=5)
    milk = api.create_product(price_cents=990, stock_quantity=3)

    created = api.client.post("/v1/orders", json=_items((dark, 2), (milk, 3)), headers=_headers(customer))
    api.client.patch(f"/v1/products/{dark.id}", json={"price_cents": 9_999}, headers=bearer(admin))
    fetched = api.client.get(created.headers["Location"], headers=bearer(customer))

    assert created.status_code == 201
    body = created.json()
    assert created.headers["Location"] == f"/v1/orders/{body['id']}"
    assert body["status"] == "confirmed"
    assert body["user_id"] == str(customer.id)
    assert body["total_cents"] == 2 * 1_590 + 3 * 990
    assert {item["product_id"]: item["unit_price_cents"] for item in fetched.json()["items"]} == {
        str(dark.id): 1_590,
        str(milk.id): 990,
    }
    assert fetched.json()["total_cents"] == body["total_cents"]
    assert api.stock(dark.id) == 3
    assert api.stock(milk.id) == 0


def test_last_unit_moves_product_out_of_stock_and_off_the_catalog(api: ApiHarness) -> None:
    customer = api.create_user()
    product = api.create_product(stock_quantity=2)

    created = api.client.post("/v1/orders", json=_items((product, 2)), headers=_headers(customer))

    assert created.status_code == 201
    assert api.get(ProductModel, product.id).status is ProductStatusEnum.OUT_OF_STOCK
    assert api.client.get("/v1/products").json()["data"] == []
    assert_problem(api.client.get(f"/v1/products/{product.id}"), 404)


@pytest.mark.parametrize("problem", ["missing", "inactive", "out_of_stock", "insufficient"])
def test_unfulfillable_item_rejects_whole_order(api: ApiHarness, problem: str) -> None:
    # Requirements 4.5 and 4.6
    customer = api.create_user()
    good = api.create_product(stock_quantity=10)
    bad = {
        "missing": uuid4(),
        "inactive": api.create_product(status=ProductStatusEnum.INACTIVE),
        "out_of_stock": api.create_product(stock_quantity=0),
        "insufficient": api.create_product(stock_quantity=1),
    }[problem]
    before = api.snapshot()

    response = api.client.post(
        "/v1/orders", json=_items((good, 3), (bad, 2)), headers=_headers(customer)
    )

    assert_problem(response, 409)
    assert api.snapshot() == before


def test_duplicate_product_in_payload_is_422(api: ApiHarness) -> None:
    # Requirement 4.4
    customer = api.create_user()
    product = api.create_product()
    before = api.snapshot()

    response = api.client.post(
        "/v1/orders", json=_items((product, 1), (product, 2)), headers=_headers(customer)
    )

    assert_problem(response, 422)
    assert api.snapshot() == before


@pytest.mark.parametrize("key", [None, "k" * 15, "k" * 129])
def test_missing_or_invalid_idempotency_key_is_422(api: ApiHarness, key: str | None) -> None:
    # Requirement 4.10
    customer = api.create_user()
    product = api.create_product()
    headers = bearer(customer) if key is None else {**bearer(customer), "Idempotency-Key": key}
    before = api.snapshot()

    response = api.client.post("/v1/orders", json=_items((product, 1)), headers=headers)

    assert_problem(response, 422)
    assert api.snapshot() == before


@pytest.mark.parametrize("key", ["k" * 16, "k" * 128])
def test_idempotency_key_length_limits_are_accepted(api: ApiHarness, key: str) -> None:
    customer = api.create_user()
    product = api.create_product()

    response = api.client.post("/v1/orders", json=_items((product, 1)), headers=_headers(customer, key))

    assert response.status_code == 201


def test_retry_replays_and_key_reuse_with_other_body_conflicts(api: ApiHarness) -> None:
    # Requirements 4.8 and 4.9
    customer = api.create_user()
    product = api.create_product(stock_quantity=10)
    headers = _headers(customer, "orders-create-retry-01")

    first = api.client.post("/v1/orders", json=_items((product, 2)), headers=headers)
    replay = api.client.post("/v1/orders", json=_items((product, 2)), headers=headers)
    conflict = api.client.post("/v1/orders", json=_items((product, 3)), headers=headers)

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert_problem(conflict, 409)
    assert api.count(OrderModel) == 1
    assert api.stock(product.id) == 8


def test_idempotency_keys_are_scoped_per_customer(api: ApiHarness) -> None:
    first_customer = api.create_user()
    second_customer = api.create_user()
    product = api.create_product(stock_quantity=10)
    key = "orders-create-shared-key"

    first = api.client.post("/v1/orders", json=_items((product, 1)), headers=_headers(first_customer, key))
    second = api.client.post("/v1/orders", json=_items((product, 1)), headers=_headers(second_customer, key))

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert second.json()["user_id"] == str(second_customer.id)
    assert api.stock(product.id) == 8


def test_only_customers_create_orders(api: ApiHarness) -> None:
    admin = api.create_admin()
    product = api.create_product()
    before = api.snapshot()

    response = api.client.post("/v1/orders", json=_items((product, 1)), headers=_headers(admin))

    assert_problem(response, 403)
    assert api.snapshot() == before
