"""Property-based tests for order listing and individual lookup."""

from __future__ import annotations

from uuid import uuid4

from hypothesis import given, strategies as st

from app.common.types import OrderStatusEnum
from tests.support import api_harness, assert_problem, bearer, utc

order_statuses = st.sampled_from(list(OrderStatusEnum))


@st.composite
def order_specs(draw: st.DrawFn) -> list[tuple[int, OrderStatusEnum, int]]:
    """``(owner index, status, minute offset)``; offsets may repeat."""

    return draw(
        st.lists(
            st.tuples(st.integers(0, 2), order_statuses, st.integers(0, 6)),
            max_size=12,
        )
    )


def _iso(minutes: int) -> str:
    return utc(minutes=minutes).isoformat().replace("+00:00", "Z")


@given(
    specs=order_specs(),
    requester_index=st.sampled_from([0, 1, "admin"]),
    data=st.data(),
)
def test_order_listing_matches_view_and_filters(specs, requester_index, data) -> None:
    # Feature: api-chocomarket, Property 18: Listagem de pedidos mantém visão e filtros
    # **Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6**
    with api_harness() as api:
        customers = [api.create_user() for _ in range(3)]
        product = api.create_product(stock_quantity=1)
        orders = [
            api.create_order(customers[owner], product, status=status, created_at=utc(minutes=minute))
            for owner, status, minute in specs
        ]
        requester = (
            api.create_admin() if requester_index == "admin" else customers[requester_index]
        )
        status_filter = data.draw(st.none() | order_statuses)
        after = data.draw(st.none() | st.integers(0, 6))
        before = data.draw(st.none() | st.integers(0, 6))
        if after is not None and before is not None and after > before:
            after, before = before, after
        user_filter = data.draw(st.none() | st.sampled_from([*customers, None]))
        params = {
            key: value
            for key, value in {
                "limit": 100,
                "status": status_filter.value if status_filter else None,
                "created_after": _iso(after) if after is not None else None,
                "created_before": _iso(before) if before is not None else None,
                "user_id": str(user_filter.id) if user_filter else None,
            }.items()
            if value is not None
        }

        response = api.client.get("/v1/orders", params=params, headers=bearer(requester))

        assert response.status_code == 200, response.text
        owner = user_filter.id if requester_index == "admin" and user_filter else None
        if requester_index != "admin":
            owner = requester.id
        expected = [
            str(order.id)
            for order in sorted(orders, key=lambda order: (order.created_at, str(order.id)))
            if (owner is None or order.user_id == owner)
            and (status_filter is None or order.status is status_filter)
            and (after is None or order.created_at >= utc(minutes=after))
            and (before is None or order.created_at <= utc(minutes=before))
        ]
        body = response.json()
        assert [order["id"] for order in body["data"]] == expected
        assert body["meta"] == {"limit": 100, "next_cursor": None, "has_next": False}


@given(
    requester=st.sampled_from(["owner", "other", "admin"]),
    status=order_statuses,
    target_exists=st.booleans(),
)
def test_order_lookup_respects_visibility(requester: str, status, target_exists: bool) -> None:
    # Feature: api-chocomarket, Property 19: Consulta individual de pedido respeita visibilidade
    # **Validates: Requirements 5.7, 5.8, 5.10**
    with api_harness() as api:
        owner = api.create_user()
        product = api.create_product()
        order = api.create_order(owner, product, quantity=2, status=status)
        caller = {
            "owner": owner,
            "other": api.create_user(),
            "admin": api.create_admin(),
        }[requester]
        target_id = order.id if target_exists else uuid4()

        response = api.client.get(f"/v1/orders/{target_id}", headers=bearer(caller))

        if target_exists and requester in ("owner", "admin"):
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["id"] == str(order.id)
            assert body["status"] == status.value
            assert body["total_cents"] == 2 * product.price_cents
            assert {"items", "created_at", "updated_at"} <= set(body)
            assert body["items"][0]["subtotal_cents"] == 2 * product.price_cents
        else:
            body = assert_problem(response, 404)
            # Missing and foreign orders are indistinguishable.
            assert body["detail"] == "The requested resource was not found."
            assert str(owner.id) not in response.text
