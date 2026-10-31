"""Property-based tests for idempotent order and customer creation."""

from __future__ import annotations

from hypothesis import given, strategies as st

from app.core.idempotency import IdempotencyRecordModel
from app.orders.model import OrderModel
from app.users.model import UserModel
from tests.support import ApiHarness, api_harness, assert_no_credentials, assert_problem, bearer

idempotency_keys = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=16,
    max_size=128,
)
retries = st.integers(min_value=1, max_value=4)


@st.composite
def order_bodies(draw: st.DrawFn, product_count: int = 3) -> list[tuple[int, int]]:
    """Return ``(product index, quantity)`` lines for one order body."""

    indices = draw(
        st.lists(st.integers(0, product_count - 1), unique=True, min_size=1, max_size=product_count)
    )
    return [(index, draw(st.integers(min_value=1, max_value=5))) for index in indices]


def _order_json(products, lines: list[tuple[int, int]]) -> dict:
    return {
        "items": [
            {"product_id": str(products[index].id), "quantity": quantity}
            for index, quantity in lines
        ]
    }


def _stocks(api: ApiHarness, products) -> list[int]:
    return [api.stock(product.id) for product in products]


@given(key=idempotency_keys, lines=order_bodies(), retries=retries)
def test_order_retry_with_same_key_and_body_has_no_extra_effect(
    key: str, lines: list[tuple[int, int]], retries: int
) -> None:
    # Feature: api-chocomarket, Property 16: Retry idempotente de pedido não repete efeitos
    # **Validates: Requirements 4.8**
    with api_harness() as api:
        customer = api.create_user()
        products = [api.create_product(stock_quantity=50) for _ in range(3)]
        headers = {**bearer(customer), "Idempotency-Key": key}
        body = _order_json(products, lines)

        first = api.client.post("/v1/orders", json=body, headers=headers)
        assert first.status_code == 201, first.text
        stocks_after_first = _stocks(api, products)

        for _ in range(retries):
            replay = api.client.post("/v1/orders", json=body, headers=headers)
            assert replay.status_code == 201
            assert replay.json() == first.json()
            assert replay.headers["Location"] == first.headers["Location"]

        assert api.count(OrderModel) == 1
        assert _stocks(api, products) == stocks_after_first
        ordered = dict(lines)
        assert stocks_after_first == [50 - ordered.get(i, 0) for i in range(3)]


@given(key=idempotency_keys, first_lines=order_bodies(), second_lines=order_bodies())
def test_order_key_reuse_with_different_body_conflicts(
    key: str, first_lines: list[tuple[int, int]], second_lines: list[tuple[int, int]]
) -> None:
    # Feature: api-chocomarket, Property 17: Fingerprint impede reuso conflitante de chave de pedido
    # **Validates: Requirements 4.9**
    with api_harness() as api:
        customer = api.create_user()
        products = [api.create_product(stock_quantity=50) for _ in range(3)]
        headers = {**bearer(customer), "Idempotency-Key": key}
        first_body = _order_json(products, first_lines)
        second_body = _order_json(products, second_lines)

        first = api.client.post("/v1/orders", json=first_body, headers=headers)
        assert first.status_code == 201, first.text
        after_first = api.snapshot()

        second = api.client.post("/v1/orders", json=second_body, headers=headers)

        if first_lines == second_lines:
            assert second.status_code == 201
            assert second.json() == first.json()
        else:
            assert_problem(second, 409)
        assert api.snapshot() == after_first


registrations = st.fixed_dictionaries(
    {
        "name": st.text(alphabet="Ana Souza", min_size=1, max_size=20).filter(str.strip),
        "email": st.sampled_from(["ana@example.com", "bia@example.com"]),
        "password": st.text(alphabet="Senha123!", min_size=12, max_size=24),
    }
)


@given(key=idempotency_keys, first=registrations, second=registrations, retries=retries)
def test_customer_registration_retry_does_not_duplicate_user(
    key: str, first: dict, second: dict, retries: int
) -> None:
    # Feature: api-chocomarket, Property 27: Retry idempotente de cadastro de usuário não duplica usuário
    # **Validates: Requirements 8.8, 8.9**
    with api_harness() as api:
        headers = {"Idempotency-Key": key}
        created = api.client.post("/v1/users", json=first, headers=headers)
        assert created.status_code == 201, created.text
        assert_no_credentials(created.json())

        for _ in range(retries):
            replay = api.client.post("/v1/users", json=first, headers=headers)
            assert replay.status_code == 201
            assert replay.json() == created.json()
        assert api.count(UserModel) == 1
        after_replays = api.snapshot()

        other = api.client.post("/v1/users", json=second, headers=headers)

        if second == first:
            assert other.status_code == 201
            assert other.json() == created.json()
        else:
            body = assert_problem(other, 409)
            assert_no_credentials(body)
        assert api.snapshot() == after_replays
        assert api.count(IdempotencyRecordModel) == 1
