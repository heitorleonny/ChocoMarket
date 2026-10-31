"""End-to-end HTTP flows for the product catalog on an isolated database."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.common.types import ProductStatusEnum
from app.products.model import ProductModel
from tests.support import ApiHarness, assert_no_credentials, assert_problem, bearer, utc


def _catalog(api: ApiHarness) -> dict[str, ProductModel]:
    return {
        "active": api.create_product(name="Barra ao leite", price_cents=800, created_at=utc()),
        "inactive": api.create_product(
            name="Barra branca", status=ProductStatusEnum.INACTIVE, created_at=utc(minutes=1)
        ),
        "out_of_stock": api.create_product(
            name="Trufa", stock_quantity=0, price_cents=300, created_at=utc(minutes=2)
        ),
        "premium": api.create_product(
            name="Barra 70%", price_cents=2_500, created_at=utc(minutes=3)
        ),
    }


def test_public_catalog_needs_no_token_and_shows_only_active(api: ApiHarness) -> None:
    # Requirements 1.4 and 3.1
    products = _catalog(api)

    listing = api.client.get("/v1/products")
    detail = api.client.get(f"/v1/products/{products['active'].id}")
    customer = api.client.get("/v1/products", headers=bearer(api.create_user()))

    assert listing.status_code == 200
    expected = [str(products["active"].id), str(products["premium"].id)]
    assert [item["id"] for item in listing.json()["data"]] == expected
    assert [item["id"] for item in customer.json()["data"]] == expected
    assert detail.status_code == 200
    assert_no_credentials(listing.json())


@pytest.mark.parametrize("hidden", ["inactive", "out_of_stock"])
def test_unavailable_products_are_hidden_from_public_and_customers(
    api: ApiHarness, hidden: str
) -> None:
    # Requirement 3.3
    product = _catalog(api)[hidden]

    for headers in ({}, bearer(api.create_user())):
        response = api.client.get(f"/v1/products/{product.id}", headers=headers)
        assert_problem(response, 404)
        assert product.name not in response.text


def test_admin_sees_every_status_and_combines_filters(api: ApiHarness) -> None:
    # Requirements 3.2 and 3.4
    products = _catalog(api)
    admin = bearer(api.create_admin())

    everything = api.client.get("/v1/products", params={"status": "inactive"}, headers=admin)
    combined = api.client.get(
        "/v1/products",
        params={"name": "BARRA", "min_price_cents": 800, "max_price_cents": 2_500},
        headers=admin,
    )
    detail = api.client.get(f"/v1/products/{products['out_of_stock'].id}", headers=admin)

    assert [item["id"] for item in everything.json()["data"]] == [str(products["inactive"].id)]
    assert {item["name"] for item in combined.json()["data"]} == {
        "Barra ao leite",
        "Barra branca",
        "Barra 70%",
    }
    assert detail.json()["status"] == "out_of_stock"


def test_admin_product_lifecycle(api: ApiHarness) -> None:
    # Requirements 3.5–3.11
    admin = bearer(api.create_admin())

    created = api.client.post(
        "/v1/products",
        json={"name": "Bombom", "price_cents": 250, "stock_quantity": 0},
        headers=admin,
    )
    product_id = created.json()["id"]
    restocked = api.client.patch(f"/v1/products/{product_id}", json={"stock_quantity": 5}, headers=admin)
    emptied = api.client.patch(f"/v1/products/{product_id}", json={"stock_quantity": 0}, headers=admin)
    deleted = api.client.delete(f"/v1/products/{product_id}", headers=admin)
    restock_inactive = api.client.patch(
        f"/v1/products/{product_id}", json={"stock_quantity": 3}, headers=admin
    )
    reactivated = api.client.patch(
        f"/v1/products/{product_id}", json={"status": "active"}, headers=admin
    )

    assert created.status_code == 201
    assert created.headers["Location"] == f"/v1/products/{product_id}"
    assert created.json()["status"] == "out_of_stock"
    assert created.json()["description"] == ""
    assert restocked.json()["status"] == "active"
    assert emptied.json()["status"] == "out_of_stock"
    assert deleted.status_code == 204 and deleted.content == b""
    assert restock_inactive.json()["status"] == "inactive"
    assert reactivated.json()["status"] == "active"
    assert reactivated.json()["name"] == "Bombom"
    assert api.count(ProductModel) == 1


def test_product_creation_idempotency(api: ApiHarness) -> None:
    admin = bearer(api.create_admin())
    headers = {**admin, "Idempotency-Key": "products-integration-01"}
    body = {"name": "Caixa", "price_cents": 5_000, "stock_quantity": 2}

    first = api.client.post("/v1/products", json=body, headers=headers)
    replay = api.client.post("/v1/products", json=body, headers=headers)
    conflict = api.client.post("/v1/products", json={**body, "price_cents": 1}, headers=headers)

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert_problem(conflict, 409)
    assert api.count(ProductModel) == 1


@pytest.mark.parametrize(
    ("field", "value", "valid"),
    [
        ("price_cents", 1, True),
        ("price_cents", 1_000_000, True),
        ("price_cents", 0, False),
        ("price_cents", 1_000_001, False),
        ("stock_quantity", 0, True),
        ("stock_quantity", 1_000_000, True),
        ("stock_quantity", -1, False),
        ("stock_quantity", 1_000_001, False),
        ("name", "n" * 160, True),
        ("name", "n" * 161, False),
        ("description", "d" * 2_000, True),
        ("description", "d" * 2_001, False),
    ],
)
def test_product_limits_on_create_and_patch(
    api: ApiHarness, field: str, value: object, valid: bool
) -> None:
    # Requirement 3.12
    admin = bearer(api.create_admin())
    existing = api.create_product()
    before = api.snapshot()

    created = api.client.post(
        "/v1/products",
        json={"name": "Limite", "price_cents": 100, "stock_quantity": 1, field: value},
        headers=admin,
    )
    patched = api.client.patch(f"/v1/products/{existing.id}", json={field: value}, headers=admin)

    if valid:
        assert created.status_code == 201
        assert patched.status_code == 200
        assert patched.json()[field] == value
    else:
        assert_problem(created, 422)
        assert_problem(patched, 422)
        assert api.snapshot() == before


def test_missing_product_is_404_without_creating_anything(api: ApiHarness) -> None:
    # Requirement 3.13
    admin = bearer(api.create_admin())
    missing = uuid4()
    before = api.snapshot()

    for response in (
        api.client.get(f"/v1/products/{missing}", headers=admin),
        api.client.patch(f"/v1/products/{missing}", json={"stock_quantity": 1}, headers=admin),
        api.client.delete(f"/v1/products/{missing}", headers=admin),
    ):
        assert_problem(response, 404)
    assert api.snapshot() == before


def test_customer_cannot_mutate_products(api: ApiHarness) -> None:
    product = api.create_product()
    customer = bearer(api.create_user())
    before = api.snapshot()

    for response in (
        api.client.post(
            "/v1/products",
            json={"name": "X", "price_cents": 1, "stock_quantity": 1},
            headers=customer,
        ),
        api.client.patch(f"/v1/products/{product.id}", json={"price_cents": 1}, headers=customer),
        api.client.delete(f"/v1/products/{product.id}", headers=customer),
    ):
        assert_problem(response, 403)
    assert api.snapshot() == before
