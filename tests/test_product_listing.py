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

from app.common.types import ProductStatusEnum, UserRoleEnum, UserStatusEnum
from app.database import Base, create_database_engine, create_session_factory, get_db
from app.main import app
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
def product_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHOCOMARKET_JWT_SECRET", SECRET)
    monkeypatch.setenv("CHOCOMARKET_JWT_ISSUER", "test-issuer")
    monkeypatch.setenv("CHOCOMARKET_JWT_AUDIENCE", "test-audience")
    monkeypatch.setenv("CHOCOMARKET_CURSOR_SECRET", "test-cursor-secret")


@pytest.fixture
def product_client() -> Generator[tuple[TestClient, Session], None, None]:
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
        app.dependency_overrides.clear()
        db.close()
        database_engine.dispose()


def _user(
    *,
    role: UserRoleEnum = UserRoleEnum.CUSTOMER,
    status: UserStatusEnum = UserStatusEnum.ACTIVE,
) -> UserModel:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    return UserModel(
        id=uuid4(),
        name="Admin" if role is UserRoleEnum.ADMIN else "Customer",
        email=f"{role.value}-{uuid4()}@example.com",
        password="argon2-hash",
        role=role,
        status=status,
        created_at=now,
        updated_at=now,
    )


def _product(
    name: str,
    created_at: datetime,
    *,
    status: ProductStatusEnum = ProductStatusEnum.ACTIVE,
    price_cents: int = 1000,
    stock_quantity: int = 5,
) -> ProductModel:
    return ProductModel(
        id=uuid4(),
        name=name,
        description=f"Description for {name}",
        price_cents=price_cents,
        stock_quantity=stock_quantity,
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


def test_public_product_reads_only_active_products(product_client) -> None:
    client, db = product_client
    start = datetime(2024, 1, 1, tzinfo=UTC)
    active = _product("Active Chocolate", start)
    inactive = _product(
        "Inactive Chocolate",
        start + timedelta(days=1),
        status=ProductStatusEnum.INACTIVE,
        stock_quantity=0,
    )
    out_of_stock = _product(
        "Sold Out Chocolate",
        start + timedelta(days=2),
        status=ProductStatusEnum.OUT_OF_STOCK,
        stock_quantity=0,
    )
    db.add_all([active, inactive, out_of_stock])
    db.commit()

    response = client.get("/v1/products")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [str(active.id)]
    assert client.get(f"/v1/products/{active.id}").status_code == 200
    hidden = client.get(f"/v1/products/{inactive.id}")
    assert hidden.status_code == 404
    assert hidden.headers["content-type"].startswith("application/problem+json")


def test_customer_reads_active_products_and_admin_combines_filters(product_client) -> None:
    client, db = product_client
    customer = _user()
    admin = _user(role=UserRoleEnum.ADMIN)
    start = datetime(2024, 2, 1, tzinfo=UTC)
    matching = _product(
        "Dark Almond Chocolate",
        start,
        price_cents=1800,
    )
    nonmatching_name = _product(
        "Milk Almond Chocolate",
        start + timedelta(days=1),
        price_cents=1800,
    )
    inactive = _product(
        "Dark Almond Chocolate Reserve",
        start + timedelta(days=2),
        status=ProductStatusEnum.INACTIVE,
        price_cents=1800,
        stock_quantity=0,
    )
    db.add_all([customer, admin, matching, nonmatching_name, inactive])
    db.commit()

    customer_response = client.get(
        "/v1/products",
        headers={"Authorization": f"Bearer {_token(customer.id)}"},
    )
    assert customer_response.status_code == 200
    assert {item["id"] for item in customer_response.json()["data"]} == {
        str(matching.id),
        str(nonmatching_name.id),
    }

    admin_response = client.get(
        "/v1/products",
        params={
            "name": "dark",
            "min_price_cents": 1800,
            "max_price_cents": 1800,
            "status": "inactive",
        },
        headers={"Authorization": f"Bearer {_token(admin.id)}"},
    )
    assert admin_response.status_code == 200
    assert [item["id"] for item in admin_response.json()["data"]] == [str(inactive.id)]
    assert client.get(
        f"/v1/products/{inactive.id}",
        headers={"Authorization": f"Bearer {_token(admin.id)}"},
    ).status_code == 200


def test_public_product_cursor_traverses_stably_and_binds_filters(product_client) -> None:
    client, db = product_client
    start = datetime(2024, 3, 1, tzinfo=UTC)
    products = [
        _product(f"Chocolate {index}", start + timedelta(days=index))
        for index in range(3)
    ]
    db.add_all(products)
    db.commit()

    first = client.get("/v1/products", params={"limit": 2, "name": "Chocolate"})
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["data"]) == 2
    assert first_body["meta"]["has_next"] is True

    second = client.get(
        "/v1/products",
        params={
            "limit": 2,
            "name": "Chocolate",
            "cursor": first_body["meta"]["next_cursor"],
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert len(second_body["data"]) == 1
    assert second_body["meta"] == {
        "limit": 2,
        "next_cursor": None,
        "has_next": False,
    }
    assert {
        item["id"] for item in first_body["data"]
    }.isdisjoint({item["id"] for item in second_body["data"]})

    incompatible = client.get(
        "/v1/products",
        params={
            "limit": 2,
            "name": "Other",
            "cursor": first_body["meta"]["next_cursor"],
        },
    )
    assert incompatible.status_code == 422
    assert incompatible.headers["content-type"].startswith("application/problem+json")
