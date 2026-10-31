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

from app.common.types import UserRoleEnum, UserStatusEnum
from app.database import Base, create_database_engine, create_session_factory, get_db
from app.main import app
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
def jwt_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHOCOMARKET_JWT_SECRET", SECRET)
    monkeypatch.setenv("CHOCOMARKET_JWT_ISSUER", "test-issuer")
    monkeypatch.setenv("CHOCOMARKET_JWT_AUDIENCE", "test-audience")
    monkeypatch.setenv("CHOCOMARKET_CURSOR_SECRET", "test-cursor-secret")


@pytest.fixture
def listing_client() -> Generator[tuple[TestClient, Session], None, None]:
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
    name: str,
    email: str,
    created_at: datetime,
    role: UserRoleEnum = UserRoleEnum.CUSTOMER,
    status: UserStatusEnum = UserStatusEnum.ACTIVE,
) -> UserModel:
    return UserModel(
        id=uuid4(),
        name=name,
        email=email,
        password="argon2-hash",
        role=role,
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


def _seed_admin(db: Session) -> UserModel:
    admin = _user(
        name="Admin Principal",
        email="admin@example.com",
        role=UserRoleEnum.ADMIN,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def test_admin_user_listing_defaults_to_active_and_omits_credentials(listing_client) -> None:
    client, db = listing_client
    admin = _seed_admin(db)
    db.add(
        _user(
            name="Active Customer",
            email="active@example.com",
            created_at=datetime(2024, 1, 2, tzinfo=UTC),
        )
    )
    db.add(
        _user(
            name="Inactive Customer",
            email="inactive@example.com",
            status=UserStatusEnum.INACTIVE,
            created_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
    )
    db.commit()

    response = client.get(
        "/v1/users",
        headers={"Authorization": f"Bearer {_token(admin.id)}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [user["email"] for user in body["data"]] == [
        "admin@example.com",
        "active@example.com",
    ]
    assert body["meta"] == {"limit": 20, "next_cursor": None, "has_next": False}
    assert all("password" not in user and "password_hash" not in user for user in body["data"])


def test_admin_user_listing_combines_all_filters(listing_client) -> None:
    client, db = listing_client
    admin = _seed_admin(db)
    matching = _user(
        name="Ana Souza",
        email="ana@example.com",
        created_at=datetime(2024, 1, 2, tzinfo=UTC),
    )
    db.add(matching)
    db.add(
        _user(
            name="Ana Admin",
            email="ana-admin@example.com",
            role=UserRoleEnum.ADMIN,
            created_at=datetime(2024, 1, 3, tzinfo=UTC),
        )
    )
    db.add(
        _user(
            name="Bruno Souza",
            email="bruno@example.com",
            created_at=datetime(2024, 1, 4, tzinfo=UTC),
        )
    )
    db.commit()

    response = client.get(
        "/v1/users",
        params={
            "name": "ana",
            "email": "ana@example.com",
            "role": "customer",
            "status": "active",
        },
        headers={"Authorization": f"Bearer {_token(admin.id)}"},
    )

    assert response.status_code == 200
    assert [user["email"] for user in response.json()["data"]] == ["ana@example.com"]


def test_admin_user_listing_cursor_traverses_without_duplicates(listing_client) -> None:
    client, db = listing_client
    admin = _seed_admin(db)
    for index in range(3):
        created_at = datetime(2024, 1, 2, tzinfo=UTC) + timedelta(days=index)
        db.add(
            _user(
                name=f"Customer {index}",
                email=f"customer-{index}@example.com",
                created_at=created_at,
            )
        )
    db.commit()

    headers = {"Authorization": f"Bearer {_token(admin.id)}"}
    first = client.get("/v1/users?limit=2", headers=headers)
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["data"]) == 2
    assert first_body["meta"]["has_next"] is True
    assert first_body["meta"]["next_cursor"]

    second = client.get(
        "/v1/users",
        params={"limit": 2, "cursor": first_body["meta"]["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200
    second_body = second.json()
    assert len(second_body["data"]) == 2
    assert second_body["meta"] == {"limit": 2, "next_cursor": None, "has_next": False}
    assert {user["id"] for user in first_body["data"]}.isdisjoint(
        {user["id"] for user in second_body["data"]}
    )


def test_admin_user_listing_rejects_cursor_with_different_filters(listing_client) -> None:
    client, db = listing_client
    admin = _seed_admin(db)
    for index in range(2):
        db.add(
            _user(
                name=f"Customer {index}",
                email=f"customer-{index}@example.com",
                created_at=datetime(2024, 1, 2 + index, tzinfo=UTC),
            )
        )
    db.commit()

    headers = {"Authorization": f"Bearer {_token(admin.id)}"}
    first = client.get("/v1/users?limit=1", headers=headers)
    cursor = first.json()["meta"]["next_cursor"]
    response = client.get(
        "/v1/users",
        params={"cursor": cursor, "status": "inactive"},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_customer_cannot_list_users(listing_client) -> None:
    client, db = listing_client
    customer = _user(
        name="Customer",
        email="customer@example.com",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    db.add(customer)
    db.commit()

    response = client.get(
        "/v1/users",
        headers={"Authorization": f"Bearer {_token(customer.id)}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
