"""Property-based tests for user listing, visibility and profile updates."""

from __future__ import annotations

from uuid import uuid4

from hypothesis import given, strategies as st

from app.common.types import UserRoleEnum, UserStatusEnum
from app.users.model import UserModel
from tests.support import (
    ApiHarness,
    api_harness,
    assert_no_credentials,
    assert_problem,
    bearer,
    utc,
)

USER_FIELDS = {"id", "name", "email", "role", "status", "created_at", "updated_at"}
NAME_ALPHABET = "abcAB áÁçÇ"

roles = st.sampled_from(list(UserRoleEnum))
statuses = st.sampled_from(list(UserStatusEnum))
names = st.text(alphabet=NAME_ALPHABET, min_size=1, max_size=12).filter(str.strip)


def _random_case(draw: st.DrawFn, text: str) -> str:
    flips = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(char.upper() if flip else char for char, flip in zip(text, flips))


@st.composite
def user_specs(draw: st.DrawFn) -> list[dict]:
    count = draw(st.integers(min_value=0, max_value=12))
    return [
        {
            "name": draw(names).strip(),
            "email": f"user{index}@example.com",
            "role": draw(roles),
            "status": draw(statuses),
        }
        for index in range(count)
    ]


def _seed_users(api: ApiHarness, specs: list[dict]) -> list[UserModel]:
    return [
        api.create_user(created_at=utc(minutes=index + 1), **spec)
        for index, spec in enumerate(specs)
    ]


@given(data=st.data(), specs=user_specs())
def test_user_listing_matches_view_and_filters(data: st.DataObject, specs: list[dict]) -> None:
    # Feature: api-chocomarket, Property 4: Listagem de usuários corresponde à visão e aos filtros
    # **Validates: Requirements 2.1, 2.2**
    with api_harness() as api:
        admin = api.create_admin(name="Root", email="root@example.com", created_at=utc())
        users = [admin, *_seed_users(api, specs)]

        params: dict[str, str] = {"limit": "100"}
        name_filter = data.draw(st.none() | names)
        if name_filter is not None:
            params["name"] = name_filter
        email_filter = data.draw(
            st.none() | st.sampled_from([user.email for user in users] + ["nobody@example.com"])
        )
        if email_filter is not None:
            params["email"] = _random_case(data.draw, email_filter)
        role_filter = data.draw(st.none() | roles)
        if role_filter is not None:
            params["role"] = role_filter.value
        status_filter = data.draw(st.none() | statuses)
        if status_filter is not None:
            params["status"] = status_filter.value

        response = api.client.get("/v1/users", params=params, headers=bearer(admin))

        assert response.status_code == 200, response.text
        expected = [
            str(user.id)
            for user in sorted(users, key=lambda user: (user.created_at, str(user.id)))
            if (name_filter is None or name_filter.strip().casefold() in user.name.casefold())
            and (email_filter is None or user.email == email_filter.lower())
            and (role_filter is None or user.role is role_filter)
            and user.status is (status_filter or UserStatusEnum.ACTIVE)
        ]
        body = response.json()
        assert [user["id"] for user in body["data"]] == expected
        assert body["meta"] == {"limit": 100, "next_cursor": None, "has_next": False}
        assert_no_credentials(body)


@given(
    requester_role=roles,
    target=st.sampled_from(["self", "other", "missing"]),
    target_status=statuses,
)
def test_profile_lookup_respects_owner_status_and_projection(
    requester_role: UserRoleEnum, target: str, target_status: UserStatusEnum
) -> None:
    # Feature: api-chocomarket, Property 5: Perfil respeita proprietário, status e projeção pública
    # **Validates: Requirements 2.3, 2.4, 2.5, 8.5**
    with api_harness() as api:
        requester = api.create_user(role=requester_role)
        other = api.create_user(status=target_status)
        target_id = {"self": requester.id, "other": other.id, "missing": uuid4()}[target]

        response = api.client.get(f"/v1/users/{target_id}", headers=bearer(requester))

        visible = target == "self" or (
            target == "other" and requester_role is UserRoleEnum.ADMIN
        )
        if visible:
            assert response.status_code == 200, response.text
            body = response.json()
            assert set(body) == USER_FIELDS
            assert body["id"] == str(target_id)
            assert_no_credentials(body)
        else:
            body = assert_problem(response, 404)
            assert str(target_id) not in response.text
            assert other.email not in response.text


@st.composite
def user_patches(draw: st.DrawFn) -> dict:
    fields = draw(
        st.sets(st.sampled_from(["name", "email", "role", "status"]), min_size=1)
    )
    patch: dict = {}
    if "name" in fields:
        patch["name"] = draw(names)
    if "email" in fields:
        patch["email"] = _random_case(draw, f"{uuid4().hex[:10]}@example.com")
    if "role" in fields:
        patch["role"] = draw(roles).value
    if "status" in fields:
        patch["status"] = draw(statuses).value
    return patch


@given(requester_role=roles, patch=user_patches())
def test_user_patch_changes_only_allowed_present_fields(
    requester_role: UserRoleEnum, patch: dict
) -> None:
    # Feature: api-chocomarket, Property 6: Patch de usuário altera somente campos permitidos e presentes
    # **Validates: Requirements 2.6, 2.7**
    with api_harness() as api:
        requester = api.create_user(role=requester_role)
        target = (
            api.create_user(name="Alvo Original")
            if requester_role is UserRoleEnum.ADMIN
            else requester
        )
        before = api.snapshot()

        response = api.client.patch(
            f"/v1/users/{target.id}", json=patch, headers=bearer(requester)
        )

        forbidden = requester_role is UserRoleEnum.CUSTOMER and bool(
            set(patch) & {"role", "status"}
        )
        if forbidden:
            assert_problem(response, 403)
            assert api.snapshot() == before
            return

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == USER_FIELDS
        assert_no_credentials(body)
        stored = api.get(UserModel, target.id)
        expected = {
            "name": patch.get("name", target.name),
            "email": patch["email"].lower() if "email" in patch else target.email,
            "role": UserRoleEnum(patch.get("role", target.role)),
            "status": UserStatusEnum(patch.get("status", target.status)),
        }
        assert {field: getattr(stored, field) for field in expected} == expected
        assert body["email"] == expected["email"]
        assert stored.password == target.password
        assert stored.created_at == target.created_at


@given(
    data=st.data(),
    requester_is_admin=st.booleans(),
)
def test_normalized_email_remains_unique(data: st.DataObject, requester_is_admin: bool) -> None:
    # Feature: api-chocomarket, Property 7: E-mail normalizado permanece único
    # **Validates: Requirements 2.8**
    with api_harness() as api:
        local_part = data.draw(st.text(alphabet="abcdefxyz019", min_size=1, max_size=12))
        taken = api.create_user(email=f"{local_part}@example.com")
        target = api.create_user(name="Alvo")
        requester = api.create_admin() if requester_is_admin else target
        before = api.snapshot()
        colliding = _random_case(data.draw, taken.email)
        extra = data.draw(st.sampled_from([{}, {"name": "Nome Novo"}]))

        response = api.client.patch(
            f"/v1/users/{target.id}",
            json={"email": colliding, **extra},
            headers=bearer(requester),
        )

        body = assert_problem(response, 409)
        assert body["code"] == "CONFLICT"
        assert api.snapshot() == before
