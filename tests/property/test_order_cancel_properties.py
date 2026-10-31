"""Property-based tests for order cancellation and stock restoration."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from hypothesis import given, strategies as st

from app.core.errors import ForbiddenError
from app.orders.service import cancel_order
from tests.support import api_harness, assert_problem, bearer

_key_counter = iter(range(10**12))


def _sorted_items(order: dict) -> list[dict]:
    # The contract does not define an item order.
    return sorted(order["items"], key=lambda item: item["id"])


@given(
    lines=st.lists(st.integers(min_value=1, max_value=20), min_size=1, max_size=5),
    canceled_by_admin=st.booleans(),
    repetitions=st.integers(min_value=1, max_value=4),
)
def test_authorized_cancellation_restores_stock_once(
    lines: list[int], canceled_by_admin: bool, repetitions: int
) -> None:
    # Feature: api-chocomarket, Property 20: Cancelamento autorizado devolve estoque uma vez
    # **Validates: Requirements 6.1, 6.2, 6.3**
    with api_harness() as api:
        customer = api.create_user()
        canceler = api.create_admin() if canceled_by_admin else customer
        products = [api.create_product(stock_quantity=quantity) for quantity in lines]
        created = api.client.post(
            "/v1/orders",
            json={
                "items": [
                    {"product_id": str(product.id), "quantity": quantity}
                    for product, quantity in zip(products, lines)
                ]
            },
            headers={
                **bearer(customer),
                "Idempotency-Key": f"cancel-property-{next(_key_counter):016d}",
            },
        )
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]
        assert [api.stock(product.id) for product in products] == [0] * len(lines)

        for _ in range(repetitions):
            response = api.client.post(
                f"/v1/orders/{order_id}/cancel", headers=bearer(canceler)
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "canceled"
            assert _sorted_items(response.json()) == _sorted_items(created.json())
            assert [api.stock(product.id) for product in products] == lines

        listed = api.client.get(
            "/v1/products", params={"limit": 100}, headers=bearer(customer)
        ).json()["data"]
        assert {item["id"] for item in listed} == {str(product.id) for product in products}


@given(
    scenario=st.sampled_from(["foreign", "missing", "unauthorized_role"]),
    quantity=st.integers(min_value=1, max_value=10),
)
def test_unauthorized_or_invalid_cancellation_does_not_mutate(
    scenario: str, quantity: int
) -> None:
    # Feature: api-chocomarket, Property 21: Cancelamento não autorizado ou inválido não muta estado
    # **Validates: Requirements 6.4, 6.5, 6.6**
    with api_harness() as api:
        owner = api.create_user()
        product = api.create_product(stock_quantity=10)
        order = api.create_order(owner, product, quantity=quantity)
        before = api.snapshot()

        if scenario == "unauthorized_role":
            # Contract roles always authorize visible cancellations, so a role
            # outside the contract exercises the 403 branch at the service.
            requester = SimpleNamespace(id=owner.id, role="auditor")
            with api.session() as db, pytest.raises(ForbiddenError):
                cancel_order(db, requester, order.id)
        else:
            target = order.id if scenario == "foreign" else uuid4()
            caller = api.create_user() if scenario == "foreign" else owner
            before = api.snapshot()
            response = api.client.post(f"/v1/orders/{target}/cancel", headers=bearer(caller))
            assert_problem(response, 404)

        assert api.snapshot() == before
