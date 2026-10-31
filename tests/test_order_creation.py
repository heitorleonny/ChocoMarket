from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.common.types import UserRoleEnum, UserStatusEnum
from app.database import Base, create_database_engine, create_session_factory, get_db
from app.main import app
from app.orders.model import OrderModel
from app.orders.schemas import CreateOrderRequest
from app.orders.service import create_order_customer
from app.products.model import ProductModel
from app.products.service import product_from_create
from app.products.schemas import CreateProductRequest
from app.users.model import UserModel


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
    signature = hmac.new(b"test-secret", signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_claims}.{_base64url(signature)}"


@pytest.fixture
def order_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHOCOMARKET_JWT_SECRET", "test-secret")
    monkeypatch.setenv("CHOCOMARKET_JWT_ISSUER", "test-issuer")
    monkeypatch.setenv("CHOCOMARKET_JWT_AUDIENCE", "test-audience")

    engine = create_database_engine(f"sqlite:///{tmp_path / 'orders.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    setup = session_factory()
    customer = UserModel(
        id=uuid4(),
        name="Order Customer",
        email="order-customer@example.com",
        password="argon2-hash",
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    product = product_from_create(
        CreateProductRequest(
            name="Dark Chocolate",
            price_cents=1_590,
            stock_quantity=5,
        )
    )
    setup.add_all([customer, product])
    setup.commit()
    product_id = product.id
    customer_id = customer.id
    setup.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client, session_factory, customer_id, product_id
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_create_order_commits_snapshot_stock_and_idempotent_replay(order_client) -> None:
    client, session_factory, customer_id, product_id = order_client
    headers = {
        "Authorization": f"Bearer {_token(customer_id)}",
        "Idempotency-Key": "order-create-key-001",
    }
    payload = {"items": [{"product_id": str(product_id), "quantity": 2}]}

    created = client.post("/v1/orders", json=payload, headers=headers)
    assert created.status_code == 201
    assert created.headers["Location"] == f"/v1/orders/{created.json()['id']}"
    assert created.json()["status"] == "confirmed"
    assert created.json()["total_cents"] == 3_180
    assert created.json()["items"][0]["unit_price_cents"] == 1_590

    replay = client.post("/v1/orders", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == created.json()

    db = session_factory()
    product = db.get(ProductModel, product_id)
    assert product is not None
    assert product.stock_quantity == 3
    assert db.scalar(select(func.count()).select_from(OrderModel)) == 1
    db.close()

    conflict = client.post(
        "/v1/orders",
        json={"items": [{"product_id": str(product_id), "quantity": 1}]},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")


def test_create_order_validates_all_items_before_mutating_any_state(order_client) -> None:
    client, session_factory, customer_id, product_id = order_client
    setup = session_factory()
    unavailable = product_from_create(
        CreateProductRequest(name="Limited Chocolate", price_cents=250, stock_quantity=1)
    )
    setup.add(unavailable)
    setup.commit()
    unavailable_id = unavailable.id
    setup.close()

    response = client.post(
        "/v1/orders",
        json={
            "items": [
                {"product_id": str(product_id), "quantity": 2},
                {"product_id": str(unavailable_id), "quantity": 2},
            ]
        },
        headers={
            "Authorization": f"Bearer {_token(customer_id)}",
            "Idempotency-Key": "order-validation-key-01",
        },
    )
    assert response.status_code == 409

    db = session_factory()
    assert db.scalar(select(func.count()).select_from(OrderModel)) == 0
    assert db.get(ProductModel, product_id).stock_quantity == 5
    assert db.get(ProductModel, unavailable_id).stock_quantity == 1
    db.close()


def test_create_order_requires_valid_idempotency_key(order_client) -> None:
    client, session_factory, customer_id, product_id = order_client
    auth = {"Authorization": f"Bearer {_token(customer_id)}"}

    missing = client.post(
        "/v1/orders",
        json={"items": [{"product_id": str(product_id), "quantity": 1}]},
        headers=auth,
    )
    short = client.post(
        "/v1/orders",
        json={"items": [{"product_id": str(product_id), "quantity": 1}]},
        headers={**auth, "Idempotency-Key": "short"},
    )
    assert missing.status_code == 422
    assert short.status_code == 422

    db = session_factory()
    assert db.scalar(select(func.count()).select_from(OrderModel)) == 0
    assert db.get(ProductModel, product_id).stock_quantity == 5
    db.close()


@pytest.mark.concurrency
def test_concurrent_orders_cannot_reserve_the_same_stock(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'concurrent-orders.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    setup = session_factory()
    first_user = UserModel(
        id=uuid4(),
        name="First Customer",
        email="first@example.com",
        password="argon2-hash",
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.ACTIVE,
    )
    second_user = UserModel(
        id=uuid4(),
        name="Second Customer",
        email="second@example.com",
        password="argon2-hash",
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.ACTIVE,
    )
    product = product_from_create(
        CreateProductRequest(name="Single Stock", price_cents=500, stock_quantity=1)
    )
    setup.add_all([first_user, second_user, product])
    setup.commit()
    first_id, second_id, product_id = first_user.id, second_user.id, product.id
    setup.close()

    payload = CreateOrderRequest(
        items=[{"product_id": product_id, "quantity": 1}]
    )

    def submit(user_id: UUID, key: str):
        db = session_factory()
        try:
            return create_order_customer(
                db,
                db.get(UserModel, user_id),
                payload,
                idempotency_key=key,
            )
        except Exception as error:  # noqa: BLE001 - assert one controlled failure below
            return error
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: submit(*args),
                [
                    (first_id, "concurrent-order-key-01"),
                    (second_id, "concurrent-order-key-02"),
                ],
            )
        )

    successes = [result for result in results if not isinstance(result, Exception)]
    assert len(successes) == 1
    assert any(getattr(result, "status_code", None) == 409 for result in results)

    check = session_factory()
    assert check.get(ProductModel, product_id).stock_quantity == 0
    assert check.scalar(select(func.count()).select_from(OrderModel)) == 1
    check.close()
    engine.dispose()
