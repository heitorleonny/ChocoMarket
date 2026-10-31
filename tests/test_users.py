"""Regression tests for the original public customer registration."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

from app.common.types import UserRoleEnum, UserStatusEnum
from app.core.security import verify_user_password
from app.users.model import UserModel
from tests.support import ApiHarness, assert_no_credentials, assert_problem

PASSWORD = "UmaSenhaSeguraCom12Caracteres"
PAYLOAD = {"name": "Ana Souza", "email": "ana@example.com", "password": PASSWORD}


def _stored_user(api: ApiHarness, email: str) -> UserModel | None:
    with api.session() as db:
        return db.scalar(select(UserModel).where(UserModel.email == email))


def test_create_customer_returns_201_without_password(api: ApiHarness) -> None:
    response = api.client.post("/v1/users", json=PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "name", "email", "role", "status", "created_at", "updated_at"}
    assert body["role"] == "customer"
    assert body["status"] == "active"
    assert response.headers["Location"] == f"/v1/users/{body['id']}"
    UUID(body["id"])
    assert_no_credentials(body)
    assert PASSWORD not in response.text


def test_create_customer_persists_active_customer_with_argon2_hash(api: ApiHarness) -> None:
    response = api.client.post("/v1/users", json=PAYLOAD)

    assert response.status_code == 201
    user = _stored_user(api, "ana@example.com")
    assert user is not None
    assert str(user.id) == response.json()["id"]
    assert user.role is UserRoleEnum.CUSTOMER
    assert user.status is UserStatusEnum.ACTIVE
    assert user.password != PASSWORD
    assert user.password.startswith("$argon2")
    assert verify_user_password(user.password, PASSWORD)


def test_create_customer_normalizes_email(api: ApiHarness) -> None:
    response = api.client.post(
        "/v1/users",
        json={**PAYLOAD, "email": "  Ana.Souza@Example.COM "},
    )

    assert response.status_code == 201
    assert response.json()["email"] == "ana.souza@example.com"
    assert _stored_user(api, "ana.souza@example.com") is not None


@pytest.mark.parametrize("field", ["role", "status", "id", "password_hash"])
def test_create_customer_rejects_privileged_or_unknown_fields(
    api: ApiHarness, field: str
) -> None:
    response = api.client.post("/v1/users", json={**PAYLOAD, field: "admin"})

    body = assert_problem(response, 422)
    assert body["code"] == "VALIDATION_ERROR"
    assert any(field in error["field"] for error in body["errors"])
    assert api.count(UserModel) == 0


def test_create_customer_rejects_admin_role(api: ApiHarness) -> None:
    response = api.client.post("/v1/users", json={**PAYLOAD, "role": "admin"})

    assert response.status_code == 422
    assert api.count(UserModel) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"password": "a" * 11},
        {"password": "a" * 129},
        {"name": ""},
        {"name": "n" * 121},
        {"email": "not-an-email"},
    ],
)
def test_create_customer_rejects_contract_violations(
    api: ApiHarness, overrides: dict[str, str]
) -> None:
    response = api.client.post("/v1/users", json={**PAYLOAD, **overrides})

    assert_problem(response, 422)
    assert api.count(UserModel) == 0


@pytest.mark.parametrize("password", ["a" * 12, "a" * 128])
def test_create_customer_accepts_password_length_limits(
    api: ApiHarness, password: str
) -> None:
    response = api.client.post("/v1/users", json={**PAYLOAD, "password": password})

    assert response.status_code == 201


def test_create_customer_rejects_duplicate_email(api: ApiHarness) -> None:
    first_response = api.client.post("/v1/users", json=PAYLOAD)
    second_response = api.client.post(
        "/v1/users",
        json={**PAYLOAD, "name": "Outra Ana", "email": "ANA@example.com"},
    )

    assert first_response.status_code == 201
    body = assert_problem(second_response, 409)
    assert body["code"] == "CONFLICT"
    assert_no_credentials(body)
    assert api.count(UserModel) == 1
