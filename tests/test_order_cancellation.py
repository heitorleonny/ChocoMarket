from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.common.types import OrderStatusEnum, UserRoleEnum, UserStatusEnum
from app.core.errors import ConflictError, ForbiddenError
from app.database import Base, create_database_engine, create_session_factory, get_db
from app.main import app
from app.orders.model import OrderItemModel, OrderModel
from app.orders.service import cancel_order
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
def cancellation_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHOCOMARKET_JWT_SECRET", SECRET)
    monkeypatch.setenv("CHOCOMARKET_JWT_ISSUER", "test-issuer")
    monkeypatch.setenv("CHOCOMARKET_JWT_AUDIENCE", "test-audience")


@pytest.fixture
def cancellation_client(
    tmp_path: Path,
) -> tuple[TestClient, Session, UserModel, UserModel, UserModel, ProductModel, OrderModel]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'order-cancellation.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    db = session_factory()
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    customer = UserModel(
        id=uuid4(),
        name="Order Customer",
        email="order-customer@example.com",
        password="argon2-hash",
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.ACTIVE,
        created_at=timestamp,
        updated_at=timestamp,
    )
    other_customer = UserModel(
        id=uuid4(),
        name="Other Customer",
        email="other-customer@example.com",
        password="argon2-hash",
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.ACTIVE,
        created_at=timestamp,
        updated_at=timestamp,
    )
    admin = UserModel(
        id=uuid4(),
        name="Order Admin",
        email="order-admin@example.com",
        password="argon2-hash",
        role=UserRoleEnum.ADMIN,
        status=UserStatusEnum.ACTIVE,
        created_at=timestamp,
        updated_at=timestamp,
    )
    product = ProductModel(
        id=uuid4(),
        name="Dark Chocolate",
        description="Dark chocolate",
        price_cents=1_590,
        stock_quantity=3,
        status="active",
        created_at=timestamp,
        updated_at=timestamp,
    )
    order = OrderModel(
        id=uuid4(),
        user_id=customer.id,
        status=OrderStatusEnum.CONFIRMED,
        total_cents=3_180,
        created_at=timestamp,
        updated_at=timestamp,
    )
    order.items = [
        OrderItemModel(
            id=uuid4(),
            product_id=product.id,
            quantity=2,
            unit_price_cents=1_590,
            subtotal_cents=3_180,
            created_at=timestamp,
            updated_at=timestamp,
        )
    ]
    db.add_all([customer, other_customer, admin, product, order])
    db.commit()

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client, db, customer, other_customer, admin, product, order
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()
        engine.dispose()


def test_owner_cancel_restores_stock_once_and_replays_canceled_order(
    cancellation_client,
) -> None:
    client, db, customer, _other, _admin, product, order = cancellation_client
    headers = {"Authorization": f"Bearer {_token(customer.id)}"}

    canceled = client.post(f"/v1/orders/{order.id}/cancel", headers=headers)

    assert canceled.status_code == 200
    assert canceled.json()["id"] == str(order.id)
    assert canceled.json()["status"] == "canceled"
    assert db.get(ProductModel, product.id).stock_quantity == 5

    replay = client.post(f"/v1/orders/{order.id}/cancel", headers=headers)

    assert replay.status_code == 200
    assert replay.json() == canceled.json()
    assert db.get(ProductModel, product.id).stock_quantity == 5
    assert db.get(OrderModel, order.id).status is OrderStatusEnum.CANCELED


def test_admin_can_cancel_another_customer_order(cancellation_client) -> None:
    client, db, customer, _other, admin, product, order = cancellation_client
    headers = {"Authorization": f"Bearer {_token(admin.id)}"}

    response = client.post(f"/v1/orders/{order.id}/cancel", headers=headers)

    assert response.status_code == 200
    assert response.json()["user_id"] == str(customer.id)
    assert response.json()["status"] == "canceled"
    assert db.get(ProductModel, product.id).stock_quantity == 5


def test_other_customer_cannot_cancel_invisible_order(cancellation_client) -> None:
    client, db, _customer, other_customer, _admin, product, order = cancellation_client
    headers = {"Authorization": f"Bearer {_token(other_customer.id)}"}

    response = client.post(f"/v1/orders/{order.id}/cancel", headers=headers)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "NOT_FOUND"
    assert db.get(ProductModel, product.id).stock_quantity == 3
    assert db.get(OrderModel, order.id).status is OrderStatusEnum.CONFIRMED


def test_visible_order_with_unsupported_role_is_forbidden_without_mutation(
    cancellation_client,
) -> None:
    _client, db, customer, _other, _admin, product, order = cancellation_client
    requester = SimpleNamespace(id=customer.id, role="support")

    with pytest.raises(ForbiddenError) as error:
        cancel_order(db, requester, order.id)

    assert error.value.status_code == 403
    assert db.get(ProductModel, product.id).stock_quantity == 3
    assert db.get(OrderModel, order.id).status is OrderStatusEnum.CONFIRMED


def test_order_in_non_cancellable_state_returns_conflict_without_mutation(
    cancellation_client,
) -> None:
    _client, db, customer, _other, _admin, product, order = cancellation_client
    order.status = "processing"

    with pytest.raises(ConflictError) as error:
        cancel_order(db, customer, order.id)

    assert error.value.status_code == 409
    db.rollback()
    assert db.get(ProductModel, product.id).stock_quantity == 3
    assert db.get(OrderModel, order.id).status is OrderStatusEnum.CONFIRMED
