"""Cursor pagination through the real users, products and orders listings."""

from __future__ import annotations

import pytest

from app.common.types import OrderStatusEnum, ProductStatusEnum, UserStatusEnum
from tests.support import ApiHarness, assert_problem, bearer, utc

RESOURCES = ["users", "products", "orders"]


def _seed(api: ApiHarness, resource: str, count: int):
    """Create ``count`` listable records and return (admin, expected IDs).

    Pairs of records share a timestamp so the ID tie-breaker is exercised.
    """

    admin = api.create_admin(created_at=utc(year=2025))
    if resource == "orders":
        product = api.create_product(created_at=utc(year=2025))
        customer = api.create_user(created_at=utc(year=2025))
    records = []
    for index in range(count):
        created_at = utc(minutes=index // 2)
        if resource == "users":
            record = api.create_user(created_at=created_at)
        elif resource == "products":
            record = api.create_product(created_at=created_at)
        else:
            record = api.create_order(customer, product, created_at=created_at)
        records.append((created_at, str(record.id)))
    expected = [record_id for _, record_id in sorted(records)]
    # The requesting admin is the oldest active user, so it is listed first.
    return admin, [str(admin.id), *expected] if resource == "users" else expected


def _traverse(api: ApiHarness, resource: str, admin, params: dict) -> tuple[list[str], list[dict]]:
    ids: list[str] = []
    metas: list[dict] = []
    cursor = None
    while True:
        query = {**params, **({"cursor": cursor} if cursor else {})}
        response = api.client.get(f"/v1/{resource}", params=query, headers=bearer(admin))
        assert response.status_code == 200, response.text
        body = response.json()
        ids.extend(item["id"] for item in body["data"])
        metas.append(body["meta"])
        cursor = body["meta"]["next_cursor"]
        if not body["meta"]["has_next"]:
            return ids, metas


@pytest.mark.parametrize("resource", RESOURCES)
def test_empty_listing_has_terminal_meta(api: ApiHarness, resource: str) -> None:
    admin = api.create_admin()
    params = {"name": "inexistente"} if resource != "orders" else {"status": "canceled"}

    response = api.client.get(f"/v1/{resource}", params=params, headers=bearer(admin))

    assert response.json() == {
        "data": [],
        "meta": {"limit": 20, "next_cursor": None, "has_next": False},
    }


@pytest.mark.parametrize("resource", RESOURCES)
@pytest.mark.parametrize(("count", "limit", "pages"), [(5, None, 1), (25, None, 2), (7, 1, 7), (100, 100, 1), (101, 100, 2)])
def test_traversal_returns_every_record_once(
    api: ApiHarness, resource: str, count: int, limit: int | None, pages: int
) -> None:
    admin, expected = _seed(api, resource, count)
    if resource == "users":
        count += 1  # the requesting admin is listed too
        pages = -(-count // (limit or 20))

    ids, metas = _traverse(api, resource, admin, {} if limit is None else {"limit": limit})

    assert ids == expected
    assert len(metas) == pages
    effective_limit = limit or 20
    assert all(meta["limit"] == effective_limit for meta in metas)
    assert all(meta["has_next"] and meta["next_cursor"] for meta in metas[:-1])
    assert metas[-1] == {"limit": effective_limit, "next_cursor": None, "has_next": False}


@pytest.mark.parametrize("resource", RESOURCES)
@pytest.mark.parametrize("limit", ["0", "101", "abc", "1.5"])
def test_invalid_limit_is_422(api: ApiHarness, resource: str, limit: str) -> None:
    response = api.client.get(
        f"/v1/{resource}", params={"limit": limit}, headers=bearer(api.create_admin())
    )

    assert_problem(response, 422)


def test_cursor_keeps_combined_filters_across_pages(api: ApiHarness) -> None:
    admin = api.create_admin()
    matching = [
        api.create_product(name=f"Barra {index}", price_cents=1_000, created_at=utc(minutes=index))
        for index in range(5)
    ]
    api.create_product(name="Barra cara", price_cents=9_000, created_at=utc(minutes=1))
    api.create_product(name="Trufa", price_cents=1_000, created_at=utc(minutes=2))
    api.create_product(
        name="Barra inativa", price_cents=1_000, status=ProductStatusEnum.INACTIVE,
        created_at=utc(minutes=3),
    )
    params = {"name": "barra", "max_price_cents": 5_000, "status": "active", "limit": 2}

    ids, metas = _traverse(api, "products", admin, params)

    assert ids == [str(product.id) for product in matching]
    assert len(metas) == 3


@pytest.mark.parametrize(
    ("resource", "changed"),
    [
        ("users", {"status": "inactive"}),
        ("products", {"name": "barra"}),
        ("orders", {"status": "confirmed"}),
    ],
)
def test_cursor_from_other_filters_is_rejected(
    api: ApiHarness, resource: str, changed: dict
) -> None:
    admin, _ = _seed(api, resource, 4)
    first = api.client.get(f"/v1/{resource}", params={"limit": 2}, headers=bearer(admin))
    cursor = first.json()["meta"]["next_cursor"]

    response = api.client.get(
        f"/v1/{resource}",
        params={"limit": 2, **changed, "cursor": cursor},
        headers=bearer(admin),
    )

    body = assert_problem(response, 422)
    assert "data" not in body


def test_page_size_may_change_between_pages(api: ApiHarness) -> None:
    # limit is not part of the listing identity bound into the cursor.
    admin, expected = _seed(api, "products", 6)
    first = api.client.get("/v1/products", params={"limit": 2}, headers=bearer(admin)).json()

    rest = api.client.get(
        "/v1/products",
        params={"limit": 10, "cursor": first["meta"]["next_cursor"]},
        headers=bearer(admin),
    ).json()

    assert [item["id"] for item in first["data"] + rest["data"]] == expected
    assert rest["meta"]["has_next"] is False


def test_cursor_cannot_cross_resources_or_customers(api: ApiHarness) -> None:
    first_customer, second_customer = api.create_user(), api.create_user()
    product = api.create_product()
    for minute in range(3):
        api.create_order(first_customer, product, created_at=utc(minutes=minute))
        api.create_order(second_customer, product, created_at=utc(minutes=minute))
        api.create_product(created_at=utc(minutes=minute))
    order_cursor = api.client.get(
        "/v1/orders", params={"limit": 1}, headers=bearer(first_customer)
    ).json()["meta"]["next_cursor"]

    other_customer = api.client.get(
        "/v1/orders", params={"limit": 1, "cursor": order_cursor}, headers=bearer(second_customer)
    )
    other_resource = api.client.get(
        "/v1/products", params={"limit": 1, "cursor": order_cursor}, headers=bearer(first_customer)
    )
    other_status = api.client.get(
        "/v1/users",
        params={"cursor": order_cursor},
        headers=bearer(api.create_admin()),
    )

    for response in (other_customer, other_resource, other_status):
        assert_problem(response, 422)


def test_listing_with_inactive_users_and_canceled_orders_respects_status(api: ApiHarness) -> None:
    admin = api.create_admin(created_at=utc(year=2025))
    inactive = [
        api.create_user(status=UserStatusEnum.INACTIVE, created_at=utc(minutes=index))
        for index in range(3)
    ]
    product = api.create_product()
    canceled = [
        api.create_order(admin, product, status=OrderStatusEnum.CANCELED, created_at=utc(minutes=i))
        for i in range(3)
    ]
    api.create_order(admin, product)

    users, _ = _traverse(api, "users", admin, {"status": "inactive", "limit": 1})
    orders, _ = _traverse(api, "orders", admin, {"status": "canceled", "limit": 2})

    assert users == [str(user.id) for user in inactive]
    assert orders == [str(order.id) for order in canceled]
