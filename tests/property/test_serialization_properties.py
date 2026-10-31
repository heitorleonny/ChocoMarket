"""Property-based tests: public serialization conforms to the contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from hypothesis import given, strategies as st

from app.common.types import (
    OrderStatusEnum,
    ProductStatusEnum,
    UserRoleEnum,
    UserStatusEnum,
)
from app.orders.model import OrderItemModel, OrderModel
from app.orders.service import order_response_from_model
from app.products.model import ProductModel
from app.products.schemas import ProductResponse
from app.users.model import UserModel
from app.users.schemas import user_response_from_model
from tests.contract.openapi import component_validator
from tests.support import assert_no_credentials

timestamps = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(UTC),
)
texts = st.text(min_size=1, max_size=40).filter(str.strip)
prices = st.integers(min_value=1, max_value=1_000_000)

users = st.builds(
    UserModel,
    id=st.uuids(),
    name=st.text(min_size=1, max_size=120),
    email=st.from_regex(r"[a-z0-9]{1,20}@[a-z]{1,10}\.(com|br)", fullmatch=True),
    password=st.just("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"),
    role=st.sampled_from(list(UserRoleEnum)),
    status=st.sampled_from(list(UserStatusEnum)),
    created_at=timestamps,
    updated_at=timestamps,
)

products = st.builds(
    ProductModel,
    id=st.uuids(),
    name=st.text(min_size=1, max_size=160),
    description=st.text(max_size=200),
    price_cents=prices,
    stock_quantity=st.integers(min_value=0, max_value=1_000_000),
    status=st.sampled_from(list(ProductStatusEnum)),
    created_at=timestamps,
    updated_at=timestamps,
)


@st.composite
def orders(draw: st.DrawFn) -> OrderModel:
    timestamp = draw(timestamps)
    items = []
    for product_id in draw(st.lists(st.uuids(), unique=True, min_size=1, max_size=50)):
        quantity = draw(st.integers(min_value=1, max_value=100))
        price = draw(prices)
        items.append(
            OrderItemModel(
                id=draw(st.uuids()),
                product_id=product_id,
                quantity=quantity,
                unit_price_cents=price,
                subtotal_cents=quantity * price,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
    order = OrderModel(
        id=draw(st.uuids()),
        user_id=draw(st.uuids()),
        status=draw(st.sampled_from(list(OrderStatusEnum))),
        total_cents=sum(item.subtotal_cents for item in items),
        created_at=timestamp,
        updated_at=draw(timestamps),
    )
    order.items = items
    return order


@given(
    entity=st.one_of(
        users.map(lambda user: ("User", user_response_from_model(user))),
        products.map(lambda product: ("Product", ProductResponse.model_validate(product))),
        orders().map(lambda order: ("Order", order_response_from_model(order))),
    )
)
def test_public_serialization_is_contract_compatible_and_safe(entity) -> None:
    # Feature: api-chocomarket, Property 24: Serialização pública é compatível e segura
    # **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5**
    schema_name, response_model = entity
    # Serialize exactly as FastAPI does for a response_model, then reparse.
    body = json.loads(response_model.model_dump_json())

    errors = list(component_validator(schema_name).iter_errors(body))
    assert not errors, errors[0].message
    assert_no_credentials(body)
    assert "$argon2" not in json.dumps(body)
    for value in (body.get("price_cents"), body.get("total_cents")):
        assert value is None or type(value) is int
