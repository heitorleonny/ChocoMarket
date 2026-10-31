from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Generator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.common.types import OrderStatusEnum, UserRoleEnum, UserStatusEnum
from app.database import Base, create_database_engine, create_session_factory, get_db
from app.main import app
from app.orders.model import OrderItemModel, OrderModel
from app.products.model import ProductModel
from app.users.model import UserModel

SECRET = "test-secret"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token(user_id: UUID) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "sub": str(user_id),
        "iss": "test-issuer",
        "aud": "test-audience",
        "exp": time.time() + 300,
    }
    encoded_header = _base64url(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _base64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = hmac.new(SECRET.encode(), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_claims}.{_base64url(signature)}"


@pytest.fixture(autouse=True)
def order_listing_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHOCOMARKET_JWT_SECRET", SECRET)
    monkeypatch.setenv("CHOCOMARKET_JWT_ISSUER", "test-issuer")
    monkeypatch.setenv("CHOCOMARKET_JWT_AUDIENCE", "test-audience")
    monkeypatch.setenv("CHOCOMARKET_CURSOR_SECRET", "test-cursor-secret")


@pytest.fixture
def order_listing_client() -> Generator[tuple[TestClient, Session], None, None]:
    database_engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=database_engine)
    session_factory = create_session_factory(database_engine)
    db = session_factory()

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client, db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()
        database_engine.dispose()


def _user(
    *,
    role: UserRoleEnum,
    email: str,
    name: str,
) -> UserModel:
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    return UserModel(
        id=uuid4(),
        name=name,
        email=email,
        password="argon2-hash",
        role=role,
        status=UserStatusEnum.ACTIVE,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _order(
    owner: UserModel,
    product: ProductModel,
    created_at: datetime,
    *,
    status: OrderStatusEnum = OrderStatusEnum.CONFIRMED,
) -> OrderModel:
    order = OrderModel(
        id=uuid4(),
        user_id=owner.id,
        status=status,
        total_cents=250,
        created_at=created_at,
        updated_at=created_at,
    )
    order.items = [
        OrderItemModel(
            id=uuid4(),
            product_id=product.id,
            quantity=1,
            unit_price_cents=250,
            subtotal_cents=250,
            created_at=created_at,
            updated_at=created_at,
        )
    ]
    return order


def _seed_users_and_product(db: Session) -> tuple[UserModel, UserModel, UserModel, ProductModel]:
    customer = _user(
        role=UserRoleEnum.CUSTOMER,
        email="customer@example.com",
        name="Customer One",
    )
    other_customer = _user(
        role=UserRoleEnum.CUSTOMER,
        email="other@example.com",
        name="Customer Two",
    )
    admin = _user(
        role=UserRoleEnum.ADMIN,
        email="admin@example.com",
        name="Admin",
    )
    product = ProductModel(
        id=uuid4(),
        name="Chocolate",
        description="Dark chocolate",
        price_cents=250,
        stock_quantity=10,
        status="active",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    db.add_all([customer, other_customer, admin, product])
    db.commit()
    return customer, other_customer, admin, product


def test_customer_listing_is_owner_scoped_and_ignores_user_id(order_listing_client) -> None:
    client, db = order_listing_client
    customer, other_customer, _admin, product = _seed_users_and_product(db)
    base = datetime(2024, 2, 1, tzinfo=UTC)
    own = _order(customer, product, base)
    other = _order(other_customer, product, base + timedelta(days=1))
    db.add_all([own, other])
    db.commit()

    response = client.get(
        "/v1/orders",
        params={"user_id": str(other_customer.id)},
        headers={"Authorization": f"Bearer {_token(customer.id)}"},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [str(own.id)]
    assert response.json()["meta"] == {
        "limit": 20,
        "next_cursor": None,
        "has_next": False,
    }


def test_admin_listing_applies_user_status_and_inclusive_date_filters(order_listing_client) -> None:
    client, db = order_listing_client
    customer, other_customer, admin, product = _seed_users_and_product(db)
    start = datetime(2024, 3, 1, tzinfo=UTC)
    matching = _order(customer, product, start)
    canceled = _order(
        customer,
        product,
        start + timedelta(days=1),
        status=OrderStatusEnum.CANCELED,
    )
    other = _order(other_customer, product, start)
    db.add_all([matching, canceled, other])
    db.commit()

    response = client.get(
        "/v1/orders",
        params={
            "user_id": str(customer.id),
            "status": "confirmed",
            "created_after": start.isoformat(),
            "created_before": start.isoformat(),
        },
        headers={"Authorization": f"Bearer {_token(admin.id)}"},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [str(matching.id)]


def test_admin_listing_cursor_is_stable_and_bound_to_filters(order_listing_client) -> None:
    client, db = order_listing_client
    customer, other_customer, admin, product = _seed_users_and_product(db)
    start = datetime(2024, 4, 1, tzinfo=UTC)
    orders = [
        _order(customer, product, start + timedelta(days=index))
        for index in range(3)
    ]
    orders.append(_order(other_customer, product, start + timedelta(days=3)))
    db.add_all(orders)
    db.commit()
    headers = {"Authorization": f"Bearer {_token(admin.id)}"}

    first = client.get("/v1/orders", params={"limit": 2}, headers=headers)
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["data"]) == 2
    assert first_body["meta"]["has_next"] is True

    second = client.get(
        "/v1/orders",
        params={"limit": 2, "cursor": first_body["meta"]["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200
    second_body = second.json()
    assert len(second_body["data"]) == 2
    assert second_body["meta"] == {
        "limit": 2,
        "next_cursor": None,
        "has_next": False,
    }
    assert {
        item["id"] for item in first_body["data"]
    }.isdisjoint({item["id"] for item in second_body["data"]})
    assert all(
        "password" not in item and "password_hash" not in item
        for item in first_body["data"] + second_body["data"]
    )

    incompatible = client.get(
        "/v1/orders",
        params={
            "limit": 2,
            "status": "canceled",
            "cursor": first_body["meta"]["next_cursor"],
        },
        headers=headers,
    )
    assert incompatible.status_code == 422
    assert incompatible.headers["content-type"].startswith("application/problem+json")


def test_customer_can_retrieve_own_order_with_complete_safe_projection(order_listing_client) -> None:
    client, db = order_listing_client
    customer, _other_customer, _admin, product = _seed_users_and_product(db)
    created_at = datetime(2024, 5, 1, tzinfo=UTC)
    order = _order(customer, product, created_at)
    db.add(order)
    db.commit()

    response = client.get(
        f"/v1/orders/{order.id}",
        headers={"Authorization": f"Bearer {_token(customer.id)}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "id",
        "user_id",
        "status",
        "total_cents",
        "items",
        "created_at",
        "updated_at",
    }
    assert body["id"] == str(order.id)
    assert body["user_id"] == str(customer.id)
    assert body["status"] == "confirmed"
    assert body["total_cents"] == 250
    assert body["items"] == [
        {
            "id": str(order.items[0].id),
            "product_id": str(product.id),
            "quantity": 1,
            "unit_price_cents": 250,
            "subtotal_cents": 250,
        }
    ]
    assert "password" not in body
    assert "password_hash" not in body


def test_customer_cannot_retrieve_missing_or_other_customer_order(order_listing_client) -> None:
    client, db = order_listing_client
    customer, other_customer, _admin, product = _seed_users_and_product(db)
    other_order = _order(other_customer, product, datetime(2024, 5, 2, tzinfo=UTC))
    db.add(other_order)
    db.commit()
    headers = {"Authorization": f"Bearer {_token(customer.id)}"}

    invisible = client.get(f"/v1/orders/{other_order.id}", headers=headers)
    missing = client.get(f"/v1/orders/{uuid4()}", headers=headers)

    assert invisible.status_code == 404
    assert missing.status_code == 404
    assert invisible.headers["content-type"].startswith("application/problem+json")
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert invisible.json()["code"] == "NOT_FOUND"
    assert missing.json()["code"] == "NOT_FOUND"


def test_admin_can_retrieve_existing_canceled_order(order_listing_client) -> None:
    client, db = order_listing_client
    customer, _other_customer, admin, product = _seed_users_and_product(db)
    order = _order(
        customer,
        product,
        datetime(2024, 5, 3, tzinfo=UTC),
        status=OrderStatusEnum.CANCELED,
    )
    db.add(order)
    db.commit()

    response = client.get(
        f"/v1/orders/{order.id}",
        headers={"Authorization": f"Bearer {_token(admin.id)}"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(order.id)
    assert response.json()["status"] == "canceled"
