from __future__ import annotations

import string

from hypothesis import HealthCheck, given, settings, strategies as st

from app.common.types import UserStatusEnum
from tests.test_auth_http import _assert_problem, _create_user, _token, auth_client


_INVALID_TOKEN_ALPHABET = string.ascii_letters + string.digits + "-_"


@settings(max_examples=100, deadline=None)
@given(
    authorization=st.one_of(
        st.none(),
        st.sampled_from(
            [
                "",
                "Basic credentials",
                "Token credentials",
                "Bearer",
                "bearer",
            ]
        ),
        st.text(alphabet=_INVALID_TOKEN_ALPHABET, min_size=1, max_size=64).map(
            lambda token: f"Bearer {token}"
        ),
    )
)
def test_invalid_or_missing_bearer_credentials_stop_protected_handler(
    auth_client,
    authorization: str | None,
) -> None:
    # Feature: api-chocomarket, Property 1: Credenciais inválidas interrompem rotas protegidas
    # **Validates: Requirements 1.1**
    client, _db, handler_calls = auth_client
    headers = {} if authorization is None else {"Authorization": authorization}

    response = client.get("/protected", headers=headers)

    _assert_problem(response, 401)
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert handler_calls == []


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    role_claim=st.sampled_from(["customer", "admin", "auditor"]),
    token_nonce=st.uuids().map(str),
)
def test_inactive_user_token_is_rejected_before_protected_handler(
    auth_client,
    role_claim: str,
    token_nonce: str,
) -> None:
    # Feature: api-chocomarket, Property 2: Usuário inativo não acessa recursos protegidos
    # **Validates: Requirements 1.3**
    client, db, handler_calls = auth_client
    user = _create_user(db, status=UserStatusEnum.INACTIVE)

    response = client.get(
        "/protected",
        headers={
            "Authorization": f"Bearer {_token(str(user.id), extra_claims={'role': role_claim, 'nonce': token_nonce})}"
        },
    )

    _assert_problem(response, 401)
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert handler_calls == []


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    role_claim=st.sampled_from(["customer", "admin", "auditor"]),
    token_nonce=st.uuids().map(str),
)
def test_active_customer_with_insufficient_role_is_rejected_without_effect(
    auth_client,
    role_claim: str,
    token_nonce: str,
) -> None:
    # Feature: api-chocomarket, Property 3: Papel insuficiente é rejeitado sem efeito
    # **Validates: Requirements 1.2**
    client, db, handler_calls = auth_client
    user = _create_user(db)
    state_before = (
        user.name,
        user.email,
        user.role,
        user.status,
        user.password,
        user.created_at,
        user.updated_at,
    )

    response = client.get(
        "/admin",
        headers={
            "Authorization": f"Bearer {_token(str(user.id), extra_claims={'role': role_claim, 'nonce': token_nonce})}"
        },
    )

    _assert_problem(response, 403)
    assert response.json()["code"] == "FORBIDDEN"
    assert "WWW-Authenticate" not in response.headers
    assert handler_calls == []

    db.refresh(user)
    state_after = (
        user.name,
        user.email,
        user.role,
        user.status,
        user.password,
        user.created_at,
        user.updated_at,
    )
    assert state_after == state_before
