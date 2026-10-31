"""End-to-end HTTP flows for the users module on an isolated database."""

from __future__ import annotations

from uuid import uuid4

from app.common.types import UserRoleEnum, UserStatusEnum
from app.core.security import verify_user_password
from app.users.model import UserModel
from tests.support import (
    DEFAULT_PASSWORD,
    ApiHarness,
    assert_no_credentials,
    assert_problem,
    bearer,
    utc,
)

NEW_PASSWORD = "OutraSenhaSegura9"


def test_registration_is_idempotent_and_returns_location(api: ApiHarness) -> None:
    payload = {"name": "Bia", "email": "Bia@Example.com", "password": DEFAULT_PASSWORD}
    headers = {"Idempotency-Key": "users-integration-0001"}

    first = api.client.post("/v1/users", json=payload, headers=headers)
    replay = api.client.post("/v1/users", json=payload, headers=headers)
    conflict = api.client.post(
        "/v1/users", json={**payload, "name": "Outra"}, headers=headers
    )

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert first.headers["Location"] == f"/v1/users/{first.json()['id']}"
    assert replay.headers["Location"] == first.headers["Location"]
    assert first.json()["email"] == "bia@example.com"
    assert_problem(conflict, 409)
    assert api.count(UserModel) == 1


def test_admin_lists_users_with_filters_and_default_active_view(api: ApiHarness) -> None:
    admin = api.create_admin(name="Admin", created_at=utc())
    ana = api.create_user(name="Ana Lima", email="ana@example.com", created_at=utc(minutes=1))
    api.create_user(name="Mariana", status=UserStatusEnum.INACTIVE, created_at=utc(minutes=2))
    api.create_user(name="Bruno", created_at=utc(minutes=3))

    default = api.client.get("/v1/users", headers=bearer(admin)).json()
    by_name = api.client.get("/v1/users", params={"name": "ANA"}, headers=bearer(admin)).json()
    inactive = api.client.get(
        "/v1/users", params={"name": "ana", "status": "inactive"}, headers=bearer(admin)
    ).json()
    by_email_role = api.client.get(
        "/v1/users",
        params={"email": "ANA@example.com", "role": "customer"},
        headers=bearer(admin),
    ).json()

    assert [user["name"] for user in default["data"]] == ["Admin", "Ana Lima", "Bruno"]
    assert default["meta"] == {"limit": 20, "next_cursor": None, "has_next": False}
    assert [user["name"] for user in by_name["data"]] == ["Ana Lima"]
    assert [user["name"] for user in inactive["data"]] == ["Mariana"]
    assert [user["id"] for user in by_email_role["data"]] == [str(ana.id)]
    assert_no_credentials(default)


def test_customer_profile_visibility_patch_and_conflict(api: ApiHarness) -> None:
    customer = api.create_user(name="Caio", email="caio@example.com")
    other = api.create_user(email="outra@example.com")
    admin = api.create_admin()

    own = api.client.get(f"/v1/users/{customer.id}", headers=bearer(customer))
    foreign = api.client.get(f"/v1/users/{other.id}", headers=bearer(customer))
    patched = api.client.patch(
        f"/v1/users/{customer.id}", json={"name": "Caio Souza"}, headers=bearer(customer)
    )
    escalation = api.client.patch(
        f"/v1/users/{customer.id}", json={"role": "admin"}, headers=bearer(customer)
    )
    conflict = api.client.patch(
        f"/v1/users/{customer.id}", json={"email": "OUTRA@example.com"}, headers=bearer(customer)
    )
    promoted = api.client.patch(
        f"/v1/users/{other.id}", json={"role": "admin"}, headers=bearer(admin)
    )

    assert own.status_code == 200
    assert_no_credentials(own.json())
    assert_problem(foreign, 404)
    assert other.email not in foreign.text
    assert patched.status_code == 200
    assert patched.json()["name"] == "Caio Souza"
    assert patched.json()["email"] == "caio@example.com"
    assert patched.json()["role"] == "customer"
    assert_problem(escalation, 403)
    assert_problem(conflict, 409)
    assert api.get(UserModel, customer.id).email == "caio@example.com"
    assert promoted.json()["role"] == "admin"
    assert api.get(UserModel, other.id).role is UserRoleEnum.ADMIN


def test_self_deactivation_is_logical_and_revokes_access(api: ApiHarness) -> None:
    customer = api.create_user()
    admin = api.create_admin()

    deleted = api.client.delete(f"/v1/users/{customer.id}", headers=bearer(customer))
    after = api.client.get(f"/v1/users/{customer.id}", headers=bearer(customer))
    repeated = api.client.delete(f"/v1/users/{customer.id}", headers=bearer(admin))
    seen_by_admin = api.client.get(f"/v1/users/{customer.id}", headers=bearer(admin))

    assert deleted.status_code == 204 and deleted.content == b""
    assert_problem(after, 401)
    assert repeated.status_code == 204
    assert seen_by_admin.json()["status"] == "inactive"
    assert api.count(UserModel) == 2


def test_password_change_replaces_the_stored_hash(api: ApiHarness) -> None:
    # Requirement 2.11
    customer = api.create_user()

    response = api.client.patch(
        f"/v1/users/{customer.id}/password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
        headers=bearer(customer),
    )

    assert response.status_code == 204 and response.content == b""
    stored = api.get(UserModel, customer.id)
    assert stored.password.startswith("$argon2")
    assert verify_user_password(stored.password, NEW_PASSWORD)
    assert not verify_user_password(stored.password, DEFAULT_PASSWORD)


def test_wrong_current_password_is_401_and_preserves_credentials(api: ApiHarness) -> None:
    # Requirement 2.12
    customer = api.create_user()
    before = api.snapshot()

    response = api.client.patch(
        f"/v1/users/{customer.id}/password",
        json={"current_password": "SenhaIncorreta99", "new_password": NEW_PASSWORD},
        headers=bearer(customer),
    )

    assert_problem(response, 401)
    assert api.snapshot() == before


def test_password_change_authorization_uses_403_or_404(api: ApiHarness) -> None:
    # Requirement 2.13
    customer = api.create_user()
    other = api.create_user()
    admin = api.create_admin()
    body = {"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD}
    before = api.snapshot()

    by_admin = api.client.patch(f"/v1/users/{customer.id}/password", json=body, headers=bearer(admin))
    by_other = api.client.patch(f"/v1/users/{customer.id}/password", json=body, headers=bearer(other))
    missing = api.client.patch(f"/v1/users/{uuid4()}/password", json=body, headers=bearer(customer))

    assert_problem(by_admin, 403)
    assert_problem(by_other, 404)
    assert_problem(missing, 404)
    assert api.snapshot() == before


def test_admin_operations_on_missing_user_return_404_without_effect(api: ApiHarness) -> None:
    # Requirement 2.14
    admin = api.create_admin()
    missing = uuid4()
    before = api.snapshot()

    responses = [
        api.client.get(f"/v1/users/{missing}", headers=bearer(admin)),
        api.client.patch(f"/v1/users/{missing}", json={"name": "X"}, headers=bearer(admin)),
        api.client.delete(f"/v1/users/{missing}", headers=bearer(admin)),
    ]

    for response in responses:
        assert_problem(response, 404)
    assert api.snapshot() == before
