from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Generator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.common.types import UserRoleEnum, UserStatusEnum
from app.core.security import hash_user_password, verify_user_password
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


@pytest.fixture
def management_client() -> Generator[tuple[TestClient, Session], None, None]:
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
    db: Session,
    *,
    email: str,
    name: str = "Test User",
    role: UserRoleEnum = UserRoleEnum.CUSTOMER,
    status: UserStatusEnum = UserStatusEnum.ACTIVE,
    password: str = "CurrentPassword12",
) -> UserModel:
    now = datetime.now(UTC)
    user = UserModel(
        id=uuid4(),
        name=name,
        email=email,
        password=hash_user_password(password),
        role=role,
        status=status,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user: UserModel) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(user.id)}"}


def test_customer_can_read_own_profile_but_not_another_user(
    management_client,
) -> None:
    client, db = management_client
    customer = _user(db, email="customer@example.com")
    other = _user(db, email="other@example.com")

    own = client.get(f"/v1/users/{customer.id}", headers=_headers(customer))
    hidden = client.get(f"/v1/users/{other.id}", headers=_headers(customer))

    assert own.status_code == 200
    assert own.json()["id"] == str(customer.id)
    assert "password" not in own.json()
    assert hidden.status_code == 404
    assert hidden.headers["content-type"].startswith("application/problem+json")


def test_admin_can_read_inactive_user_and_patch_fields(
    management_client,
) -> None:
    client, db = management_client
    admin = _user(
        db,
        email="admin@example.com",
        role=UserRoleEnum.ADMIN,
    )
    target = _user(
        db,
        email="target@example.com",
        status=UserStatusEnum.INACTIVE,
    )

    read = client.get(f"/v1/users/{target.id}", headers=_headers(admin))
    patched = client.patch(
        f"/v1/users/{target.id}",
        json={"name": "Reactivated", "status": "active"},
        headers=_headers(admin),
    )

    assert read.status_code == 200
    assert read.json()["status"] == "inactive"
    assert patched.status_code == 200
    assert patched.json()["name"] == "Reactivated"
    assert patched.json()["status"] == "active"
    assert patched.json()["email"] == "target@example.com"


def test_customer_patch_is_partial_and_rejects_role_changes(
    management_client,
) -> None:
    client, db = management_client
    customer = _user(db, email="customer@example.com")

    patched = client.patch(
        f"/v1/users/{customer.id}",
        json={"name": "Updated Name"},
        headers=_headers(customer),
    )
    forbidden = client.patch(
        f"/v1/users/{customer.id}",
        json={"role": "admin"},
        headers=_headers(customer),
    )

    assert patched.status_code == 200
    assert patched.json()["name"] == "Updated Name"
    assert patched.json()["email"] == "customer@example.com"
    assert forbidden.status_code == 403
    db.refresh(customer)
    assert customer.role is UserRoleEnum.CUSTOMER


def test_patch_normalized_email_conflict_preserves_target(
    management_client,
) -> None:
    client, db = management_client
    admin = _user(db, email="admin@example.com", role=UserRoleEnum.ADMIN)
    first = _user(db, email="first@example.com", name="Original")
    _user(db, email="taken@example.com")

    response = client.patch(
        f"/v1/users/{first.id}",
        json={"name": "Should Not Persist", "email": " TAKEN@EXAMPLE.COM "},
        headers=_headers(admin),
    )

    assert response.status_code == 409
    db.refresh(first)
    assert first.name == "Original"
    assert first.email == "first@example.com"


def test_admin_deactivation_is_logical_and_idempotent(
    management_client,
) -> None:
    client, db = management_client
    admin = _user(db, email="admin@example.com", role=UserRoleEnum.ADMIN)
    target = _user(db, email="target@example.com")
    original_name = target.name
    original_email = target.email

    first = client.delete(f"/v1/users/{target.id}", headers=_headers(admin))
    second = client.delete(f"/v1/users/{target.id}", headers=_headers(admin))

    assert first.status_code == 204
    assert first.content == b""
    assert second.status_code == 204
    db.refresh(target)
    assert target.status is UserStatusEnum.INACTIVE
    assert target.name == original_name
    assert target.email == original_email


def test_password_change_verifies_current_password_and_authorization(
    management_client,
) -> None:
    client, db = management_client
    customer = _user(db, email="customer@example.com")
    other = _user(db, email="other@example.com")
    admin = _user(db, email="admin@example.com", role=UserRoleEnum.ADMIN)

    wrong = client.patch(
        f"/v1/users/{customer.id}/password",
        json={
            "current_password": "WrongPassword12",
            "new_password": "NewPassword1234",
        },
        headers=_headers(customer),
    )
    changed = client.patch(
        f"/v1/users/{customer.id}/password",
        json={
            "current_password": "CurrentPassword12",
            "new_password": "NewPassword1234",
        },
        headers=_headers(customer),
    )
    other_target = client.patch(
        f"/v1/users/{other.id}/password",
        json={
            "current_password": "CurrentPassword12",
            "new_password": "NewPassword1234",
        },
        headers=_headers(customer),
    )
    admin_target = client.patch(
        f"/v1/users/{customer.id}/password",
        json={
            "current_password": "CurrentPassword12",
            "new_password": "NewPassword1234",
        },
        headers=_headers(admin),
    )

    assert wrong.status_code == 401
    assert changed.status_code == 204
    assert changed.content == b""
    assert other_target.status_code == 404
    assert admin_target.status_code == 403
    db.refresh(customer)
    assert verify_user_password(customer.password, "NewPassword1234")
    assert not verify_user_password(customer.password, "CurrentPassword12")
