from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.common.types import OrderStatusEnum
from app.database import Base, create_database_engine, create_session_factory
from app.orders.model import OrderModel
from app.orders.repository import OrderRepository
from app.orders.schemas import (
    CreateOrderRequest,
    OrderItemResponse,
    OrderResponse,
)
from app.orders.service import (
    build_order_model,
    calculate_subtotal_cents,
    calculate_total_cents,
    order_response_from_model,
)
from app.products.service import product_from_create
from app.products.schemas import CreateProductRequest
from app.users.model import UserModel
from app.common.types import UserRoleEnum, UserStatusEnum


@pytest.fixture()
def order_session():
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    with session_factory() as db:
        yield db
    engine.dispose()


def test_create_order_schema_is_strict_and_rejects_duplicate_products() -> None:
    product_id = uuid4()
    payload = CreateOrderRequest(
        items=[{"product_id": product_id, "quantity": 2}],
    )
    assert payload.items[0].product_id == product_id
    assert payload.items[0].quantity == 2

    with pytest.raises(ValidationError):
        CreateOrderRequest(
            items=[
                {"product_id": product_id, "quantity": 1},
                {"product_id": product_id, "quantity": 2},
            ]
        )
    with pytest.raises(ValidationError):
        CreateOrderRequest(items=[{"product_id": product_id, "quantity": True}])
    with pytest.raises(ValidationError):
        CreateOrderRequest(items=[])
    with pytest.raises(ValidationError):
        CreateOrderRequest(
            items=[{"product_id": product_id, "quantity": 1, "unexpected": "x"}]
        )


def test_order_calculations_use_integer_cents_and_validate_boundaries() -> None:
    assert calculate_subtotal_cents(2, 1_590) == 3_180
    assert calculate_total_cents([3_180, 990]) == 4_170

    with pytest.raises(ValueError):
        calculate_subtotal_cents(True, 100)
    with pytest.raises(ValueError):
        calculate_subtotal_cents(0, 100)
    with pytest.raises(ValueError):
        calculate_subtotal_cents(1, 0)
    with pytest.raises(ValueError):
        calculate_total_cents([])
    with pytest.raises(ValueError):
        calculate_total_cents([1, True])


def test_build_order_captures_price_snapshots_and_total() -> None:
    first = product_from_create(
        CreateProductRequest(
            name="Dark Chocolate",
            price_cents=1_590,
            stock_quantity=10,
        )
    )
    first.id = uuid4()
    second = product_from_create(
        CreateProductRequest(
            name="Milk Chocolate",
            price_cents=990,
            stock_quantity=10,
        )
    )
    second.id = uuid4()
    payload = CreateOrderRequest(
        items=[
            {"product_id": first.id, "quantity": 2},
            {"product_id": second.id, "quantity": 1},
        ]
    )
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)

    order = build_order_model(
        user_id=uuid4(),
        payload=payload,
        products=[first, second],
        now=timestamp,
    )

    assert order.status is OrderStatusEnum.CONFIRMED
    assert order.total_cents == 4_170
    assert [(item.product_id, item.unit_price_cents, item.subtotal_cents) for item in order.items] == [
        (first.id, 1_590, 3_180),
        (second.id, 990, 990),
    ]
    first.price_cents = 9_999
    assert order.items[0].unit_price_cents == 1_590
    assert order.items[0].subtotal_cents == 3_180
    assert order.created_at == timestamp
    assert order.updated_at == timestamp


def test_order_response_schema_exposes_only_contract_fields() -> None:
    product = product_from_create(
        CreateProductRequest(name="Chocolate", price_cents=1_590, stock_quantity=2)
    )
    product.id = uuid4()
    order = build_order_model(
        user_id=uuid4(),
        payload=CreateOrderRequest(
            items=[{"product_id": product.id, "quantity": 2}]
        ),
        products=[product],
    )
    order.id = uuid4()
    order.items[0].id = uuid4()

    response = order_response_from_model(order)

    assert isinstance(response.items[0], OrderItemResponse)
    assert response.status is OrderStatusEnum.CONFIRMED
    assert set(response.model_dump()) == {
        "id",
        "user_id",
        "status",
        "total_cents",
        "items",
        "created_at",
        "updated_at",
    }
    assert set(response.items[0].model_dump()) == {
        "id",
        "product_id",
        "quantity",
        "unit_price_cents",
        "subtotal_cents",
    }
    assert "password" not in response.model_dump()


def test_order_repository_flushes_but_does_not_commit(order_session) -> None:
    user = UserModel(
        name="Order Owner",
        email="owner@example.com",
        password="argon2-hash",
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.ACTIVE,
    )
    order_session.add(user)
    order_session.flush()
    product = product_from_create(
        CreateProductRequest(name="Chocolate", price_cents=100, stock_quantity=3)
    )
    order_session.add(product)
    order_session.flush()
    order = build_order_model(
        user_id=user.id,
        payload=CreateOrderRequest(
            items=[{"product_id": product.id, "quantity": 2}]
        ),
        products=[product],
    )

    OrderRepository.create(order_session, order)
    assert OrderRepository.get_by_id(order_session, str(order.id)).id == order.id
    order_session.rollback()
    assert order_session.get(OrderModel, order.id) is None
