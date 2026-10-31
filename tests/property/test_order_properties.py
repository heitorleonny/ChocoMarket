"""Property-based tests for order pricing, atomicity and stock effects."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from hypothesis import given, strategies as st

from app.common.types import OrderStatusEnum, ProductStatusEnum
from app.orders.schemas import CreateOrderRequest
from app.orders.service import build_order_model, order_response_from_model
from app.products.model import ProductModel
from tests.support import ApiHarness, api_harness, assert_problem, bearer

prices = st.integers(min_value=1, max_value=1_000_000)
quantities = st.integers(min_value=1, max_value=100)

_key_counter = iter(range(10**12))


def _key() -> str:
    return f"order-property-{next(_key_counter):016d}"


def _post_order(api: ApiHarness, user, items: list[tuple[ProductModel | object, int]]):
    return api.client.post(
        "/v1/orders",
        json={
            "items": [
                {"product_id": str(getattr(product, "id", product)), "quantity": quantity}
                for product, quantity in items
            ]
        },
        headers={**bearer(user), "Idempotency-Key": _key()},
    )


def _items_by_id(order: dict) -> dict:
    """The contract does not order items, so compare them by identity."""

    return {**order, "items": sorted(order["items"], key=lambda item: item["id"])}


@given(
    lines=st.lists(st.tuples(prices, quantities), min_size=1, max_size=50),
)
def test_monetary_calculations_are_exact_integers(lines: list[tuple[int, int]]) -> None:
    # Feature: api-chocomarket, Property 12: Cálculos monetários são inteiros e consistentes
    # **Validates: Requirements 4.2, 8.4**
    products = [SimpleNamespace(id=uuid4(), price_cents=price) for price, _ in lines]
    payload = CreateOrderRequest(
        items=[
            {"product_id": product.id, "quantity": quantity}
            for product, (_, quantity) in zip(products, lines)
        ]
    )

    order = build_order_model(user_id=uuid4(), payload=payload, products=products)
    for item in order.items:
        item.id = uuid4()
    order.id = uuid4()
    response = order_response_from_model(order).model_dump(mode="json")

    expected_subtotals = [price * quantity for price, quantity in lines]
    assert [item["subtotal_cents"] for item in response["items"]] == expected_subtotals
    assert [item["unit_price_cents"] for item in response["items"]] == [p for p, _ in lines]
    assert response["total_cents"] == sum(expected_subtotals)
    for value in [response["total_cents"], *expected_subtotals]:
        assert type(value) is int


@given(
    lines=st.lists(
        st.tuples(prices, st.integers(min_value=1, max_value=10)), min_size=1, max_size=5
    ),
    new_prices=st.lists(prices, min_size=5, max_size=5),
)
def test_order_preserves_purchase_time_prices(
    lines: list[tuple[int, int]], new_prices: list[int]
) -> None:
    # Feature: api-chocomarket, Property 13: Pedido preserva o preço no momento da compra
    # **Validates: Requirements 4.3**
    with api_harness() as api:
        customer = api.create_user()
        admin = api.create_admin()
        products = [api.create_product(price_cents=price, stock_quantity=10) for price, _ in lines]
        created = _post_order(api, customer, list(zip(products, [q for _, q in lines])))
        assert created.status_code == 201, created.text
        original = created.json()

        for product, new_price in zip(products, new_prices):
            patched = api.client.patch(
                f"/v1/products/{product.id}",
                json={"price_cents": new_price},
                headers=bearer(admin),
            )
            assert patched.status_code == 200

        current = api.client.get(f"/v1/orders/{original['id']}", headers=bearer(customer))
        assert current.status_code == 200
        assert _items_by_id(current.json()) == _items_by_id(original)
        assert [item["unit_price_cents"] for item in original["items"]] == [p for p, _ in lines]


@st.composite
def unfulfillable_orders(draw: st.DrawFn) -> tuple[list[dict], list[tuple[int, str, int]]]:
    """Catalog specs plus items where at least one line cannot be fulfilled."""

    catalog = [
        {
            "stock_quantity": draw(st.integers(min_value=0, max_value=20)),
            "status": draw(st.sampled_from(list(ProductStatusEnum))),
        }
        for _ in range(draw(st.integers(min_value=1, max_value=5)))
    ]
    for spec in catalog:
        if spec["stock_quantity"] == 0 and spec["status"] is ProductStatusEnum.ACTIVE:
            spec["status"] = ProductStatusEnum.OUT_OF_STOCK
        if spec["status"] is ProductStatusEnum.OUT_OF_STOCK:
            spec["stock_quantity"] = 0
    indices = draw(st.lists(st.integers(0, len(catalog) - 1), unique=True, min_size=1))
    lines = [(index, "existing", draw(quantities)) for index in indices]
    if draw(st.booleans()):
        lines.append((-1, "missing", draw(quantities)))
    return catalog, lines


def _fulfillable(spec: dict, quantity: int) -> bool:
    return spec["status"] is ProductStatusEnum.ACTIVE and spec["stock_quantity"] >= quantity


@given(order=unfulfillable_orders())
def test_any_unfulfillable_item_rolls_back_the_whole_order(order) -> None:
    # Feature: api-chocomarket, Property 14: Falha de item reverte todo o pedido
    # **Validates: Requirements 4.5, 4.6**
    catalog, lines = order
    if all(kind == "existing" and _fulfillable(catalog[i], q) for i, kind, q in lines):
        lines = [*lines, (-1, "missing", 1)]
    with api_harness() as api:
        customer = api.create_user()
        products = [api.create_product(**spec) for spec in catalog]
        before = api.snapshot()

        response = _post_order(
            api,
            customer,
            [
                (products[index] if kind == "existing" else uuid4(), quantity)
                for index, kind, quantity in lines
            ],
        )

        assert_problem(response, 409)
        assert api.snapshot() == before


@given(
    catalog=st.lists(
        st.tuples(prices, st.integers(min_value=1, max_value=200)), min_size=1, max_size=8
    ),
    data=st.data(),
)
def test_fulfillable_order_changes_every_entity_consistently(catalog, data) -> None:
    # Feature: api-chocomarket, Property 15: Pedido atendível altera todas as entidades de forma consistente
    # **Validates: Requirements 4.1, 4.7**
    with api_harness() as api:
        customer = api.create_user()
        products = [api.create_product(price_cents=p, stock_quantity=s) for p, s in catalog]
        chosen = data.draw(
            st.lists(st.integers(0, len(products) - 1), unique=True, min_size=1)
        )
        lines = [
            (products[i], data.draw(st.integers(1, min(100, products[i].stock_quantity))))
            for i in chosen
        ]

        response = _post_order(api, customer, lines)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == OrderStatusEnum.CONFIRMED.value
        assert body["user_id"] == str(customer.id)
        assert [item["product_id"] for item in body["items"]] == [str(p.id) for p, _ in lines]
        for item, (product, quantity) in zip(body["items"], lines):
            assert item["quantity"] == quantity
            assert item["unit_price_cents"] == product.price_cents
            assert item["subtotal_cents"] == quantity * product.price_cents
        assert body["total_cents"] == sum(q * p.price_cents for p, q in lines)

        ordered = {product.id: quantity for product, quantity in lines}
        for product in products:
            stored = api.get(ProductModel, product.id)
            remaining = product.stock_quantity - ordered.get(product.id, 0)
            assert stored.stock_quantity == remaining
            if product.id in ordered:
                assert stored.status is (
                    ProductStatusEnum.OUT_OF_STOCK if remaining == 0 else ProductStatusEnum.ACTIVE
                )
