"""End-to-end HTTP flows for order queries and cancellation."""

from __future__ import annotations

from uuid import uuid4

from app.common.types import OrderStatusEnum
from tests.support import ApiHarness, assert_problem, bearer, utc


def _ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [order["id"] for order in response.json()["data"]]


def test_listing_scopes_customers_and_filters_admin_view(api: ApiHarness) -> None:
    ana, bia = api.create_user(), api.create_user()
    admin = api.create_admin()
    product = api.create_product()
    ana_old = api.create_order(ana, product, created_at=utc(day=1))
    ana_canceled = api.create_order(
        ana, product, status=OrderStatusEnum.CANCELED, created_at=utc(day=2)
    )
    bia_order = api.create_order(bia, product, created_at=utc(day=3))

    own = api.client.get("/v1/orders", headers=bearer(ana))
    ignores_user_id = api.client.get(
        "/v1/orders", params={"user_id": str(bia.id)}, headers=bearer(ana)
    )
    everyone = api.client.get("/v1/orders", headers=bearer(admin))
    only_bia = api.client.get("/v1/orders", params={"user_id": str(bia.id)}, headers=bearer(admin))
    canceled = api.client.get("/v1/orders", params={"status": "canceled"}, headers=bearer(admin))
    inclusive_range = api.client.get(
        "/v1/orders",
        params={"created_after": "2026-01-02T00:00:00Z", "created_before": "2026-01-03T00:00:00Z"},
        headers=bearer(admin),
    )
    combined = api.client.get(
        "/v1/orders",
        params={"status": "confirmed", "created_before": "2026-01-02T00:00:00Z"},
        headers=bearer(ana),
    )

    assert _ids(own) == [str(ana_old.id), str(ana_canceled.id)]
    assert _ids(ignores_user_id) == _ids(own)
    assert _ids(everyone) == [str(ana_old.id), str(ana_canceled.id), str(bia_order.id)]
    assert _ids(only_bia) == [str(bia_order.id)]
    assert _ids(canceled) == [str(ana_canceled.id)]
    assert _ids(inclusive_range) == [str(ana_canceled.id), str(bia_order.id)]
    assert _ids(combined) == [str(ana_old.id)]


def test_inverted_date_range_is_422(api: ApiHarness) -> None:
    response = api.client.get(
        "/v1/orders",
        params={"created_after": "2026-02-01T00:00:00Z", "created_before": "2026-01-01T00:00:00Z"},
        headers=bearer(api.create_user()),
    )

    assert_problem(response, 422)


def test_order_lookup_visibility(api: ApiHarness) -> None:
    # Requirements 5.7–5.10
    owner, stranger = api.create_user(), api.create_user()
    admin = api.create_admin()
    product = api.create_product(price_cents=700)
    order = api.create_order(owner, product, quantity=3, status=OrderStatusEnum.CANCELED)

    own = api.client.get(f"/v1/orders/{order.id}", headers=bearer(owner))
    by_admin = api.client.get(f"/v1/orders/{order.id}", headers=bearer(admin))
    foreign = api.client.get(f"/v1/orders/{order.id}", headers=bearer(stranger))
    missing_for_admin = api.client.get(f"/v1/orders/{uuid4()}", headers=bearer(admin))

    assert own.status_code == by_admin.status_code == 200
    body = own.json()
    assert body["status"] == "canceled"
    assert body["total_cents"] == 2_100
    assert body["items"][0]["subtotal_cents"] == 2_100
    assert {"created_at", "updated_at"} <= set(body)
    assert_problem(foreign, 404)
    assert_problem(missing_for_admin, 404)
    assert foreign.json()["detail"] == missing_for_admin.json()["detail"]


def test_customer_cancels_own_order_and_retries_safely(api: ApiHarness) -> None:
    # Requirements 6.1 and 6.3
    customer = api.create_user()
    first = api.create_product(stock_quantity=5)
    second = api.create_product(stock_quantity=2)
    created = api.client.post(
        "/v1/orders",
        json={
            "items": [
                {"product_id": str(first.id), "quantity": 4},
                {"product_id": str(second.id), "quantity": 2},
            ]
        },
        headers={**bearer(customer), "Idempotency-Key": "query-cancel-order-01"},
    )
    url = f"/v1/orders/{created.json()['id']}/cancel"

    canceled = api.client.post(url, headers=bearer(customer))
    stocks_after_cancel = (api.stock(first.id), api.stock(second.id))
    repeated = api.client.post(url, headers=bearer(customer))

    assert canceled.status_code == repeated.status_code == 200
    assert canceled.json()["status"] == repeated.json()["status"] == "canceled"
    assert stocks_after_cancel == (5, 2)
    assert (api.stock(first.id), api.stock(second.id)) == (5, 2)
    catalog = api.client.get("/v1/products").json()["data"]
    assert {product["id"] for product in catalog} == {str(first.id), str(second.id)}


def test_admin_cancels_any_order(api: ApiHarness) -> None:
    # Requirement 6.2
    product = api.create_product(stock_quantity=0)
    order = api.create_order(api.create_user(), product, quantity=3)

    response = api.client.post(f"/v1/orders/{order.id}/cancel", headers=bearer(api.create_admin()))

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"
    assert api.stock(product.id) == 3


def test_foreign_or_missing_cancellation_does_not_mutate(api: ApiHarness) -> None:
    # Requirement 6.4
    owner, stranger = api.create_user(), api.create_user()
    product = api.create_product(stock_quantity=1)
    order = api.create_order(owner, product)
    before = api.snapshot()

    foreign = api.client.post(f"/v1/orders/{order.id}/cancel", headers=bearer(stranger))
    missing = api.client.post(f"/v1/orders/{uuid4()}/cancel", headers=bearer(owner))

    assert_problem(foreign, 404)
    assert_problem(missing, 404)
    assert api.snapshot() == before
