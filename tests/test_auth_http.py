import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Generator
from uuid import uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.common.types import UserRoleEnum, UserStatusEnum
from app.core.auth import require_active_user, require_admin
from app.core.errors import register_exception_handlers
from app.core.http import RequestIDMiddleware
from app.database import Base, create_database_engine, create_session_factory, get_db
from app.users.model import UserModel


SECRET = "task-2-5-test-secret"
ISSUER = "https://issuer.example.test"
AUDIENCE = "chocomarket-api"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token(
    user_id: str,
    *,
    algorithm: str = "HS256",
    issuer: str = ISSUER,
    audience: str | list[str] = AUDIENCE,
    expiration: float | None = None,
    secret: str = SECRET,
    extra_claims: dict[str, object] | None = None,
) -> str:
    header = {"alg": algorithm, "typ": "JWT"}
    claims: dict[str, object] = {
        "sub": user_id,
        "iss": issuer,
        "aud": audience,
        "exp": time.time() + 300 if expiration is None else expiration,
    }
    if extra_claims:
        claims.update(extra_claims)
    encoded_header = _base64url(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _base64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    if algorithm == "none":
        signature = b""
    else:
        digest = getattr(
            hashlib,
            {"HS256": "sha256", "HS384": "sha384", "HS512": "sha512"}[algorithm],
        )
        signature = hmac.new(secret.encode(), signing_input, digest).digest()
    return f"{encoded_header}.{encoded_claims}.{_base64url(signature)}"


def _tampered_token(user_id: str) -> str:
    # Change the first signature character: the last one of a base64url
    # HS256 signature partly encodes padding bits, so changing it can leave
    # the decoded signature, and therefore the token, still valid.
    token = _token(user_id)
    signature_start = token.rindex(".") + 1
    replacement = "A" if token[signature_start] != "A" else "B"
    return token[:signature_start] + replacement + token[signature_start + 1 :]


def _create_user(
    db: Session,
    *,
    role: UserRoleEnum = UserRoleEnum.CUSTOMER,
    status: UserStatusEnum = UserStatusEnum.ACTIVE,
) -> UserModel:
    now = datetime.now(UTC)
    user = UserModel(
        id=uuid4(),
        name="Auth Test User",
        email=f"{uuid4()}@example.com",
        password="argon2-hash-not-a-credential-for-this-test",
        role=role,
        status=status,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def auth_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, Session, list[str]], None, None]:
    monkeypatch.setenv("CHOCOMARKET_JWT_SECRET", SECRET)
    monkeypatch.setenv("CHOCOMARKET_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("CHOCOMARKET_JWT_AUDIENCE", AUDIENCE)

    database_engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    session = create_session_factory(database_engine)()
    handler_calls: list[str] = []

    test_app = FastAPI()
    test_app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(test_app)
    router = APIRouter()

    @router.get("/protected")
    def protected(user: UserModel = Depends(require_active_user)) -> dict[str, str]:
        handler_calls.append("protected")
        return {"user_id": str(user.id)}

    @router.get("/admin")
    def admin(user: UserModel = Depends(require_admin)) -> dict[str, str]:
        handler_calls.append("admin")
        return {"user_id": str(user.id)}

    test_app.include_router(router)

    def override_get_db(request: Request) -> Generator[Session, None, None]:
        request.state.db = session
        yield session

    test_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(test_app) as client:
            yield client, session, handler_calls
    finally:
        test_app.dependency_overrides.pop(get_db, None)
        session.close()
        database_engine.dispose()


def _assert_problem(response, status: int) -> None:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert {
        "type",
        "title",
        "status",
        "detail",
        "code",
        "request_id",
    } <= body.keys()
    assert body["status"] == status
    assert body["request_id"]


def test_protected_route_rejects_missing_and_malformed_credentials(
    auth_client: tuple[TestClient, Session, list[str]],
) -> None:
    client, _db, handler_calls = auth_client

    for headers in ({}, {"Authorization": "Basic credentials"}, {"Authorization": "Bearer malformed"}):
        response = client.get("/protected", headers=headers)
        _assert_problem(response, 401)
        assert response.headers["WWW-Authenticate"] == "Bearer"

    assert handler_calls == []


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda user_id: _token(user_id, expiration=time.time() - 1),
        lambda user_id: _token(user_id, issuer="https://wrong-issuer.example.test"),
        lambda user_id: _token(user_id, audience="wrong-audience"),
        lambda user_id: _token(user_id, algorithm="HS384"),
        lambda user_id: _tampered_token(user_id),
    ],
    ids=["expired", "issuer-invalid", "audience-invalid", "algorithm-invalid", "tampered"],
)
def test_protected_route_rejects_invalid_token_variants(
    auth_client: tuple[TestClient, Session, list[str]],
    token_factory,
) -> None:
    client, db, handler_calls = auth_client
    user = _create_user(db)

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token_factory(str(user.id))}"},
    )

    _assert_problem(response, 401)
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert handler_calls == []


def test_invalid_algorithm_and_sensitive_token_data_are_not_exposed(
    auth_client: tuple[TestClient, Session, list[str]],
) -> None:
    client, db, handler_calls = auth_client
    user = _create_user(db)
    marker = "secret-value-must-not-leak"
    token = _token(
        str(user.id),
        algorithm="none",
        extra_claims={"diagnostic": marker},
    )

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    _assert_problem(response, 401)
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert marker not in response.text
    assert SECRET not in response.text
    assert token not in response.text
    assert handler_calls == []


def test_inactive_user_is_rejected_before_protected_handler(
    auth_client: tuple[TestClient, Session, list[str]],
) -> None:
    client, db, handler_calls = auth_client
    user = _create_user(db, status=UserStatusEnum.INACTIVE)

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {_token(str(user.id))}"},
    )

    _assert_problem(response, 401)
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert handler_calls == []


def test_active_customer_cannot_access_admin_route_and_handler_is_not_called(
    auth_client: tuple[TestClient, Session, list[str]],
) -> None:
    client, db, handler_calls = auth_client
    user = _create_user(db, role=UserRoleEnum.CUSTOMER)

    response = client.get(
        "/admin",
        headers={"Authorization": f"Bearer {_token(str(user.id))}"},
    )

    _assert_problem(response, 403)
    assert response.json()["code"] == "FORBIDDEN"
    assert "WWW-Authenticate" not in response.headers
    assert handler_calls == []


def test_admin_can_access_admin_route_using_current_database_role(
    auth_client: tuple[TestClient, Session, list[str]],
) -> None:
    client, db, handler_calls = auth_client
    user = _create_user(db, role=UserRoleEnum.ADMIN)

    response = client.get(
        "/admin",
        headers={"Authorization": f"Bearer {_token(str(user.id), extra_claims={'role': 'customer'})}"},
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": str(user.id)}
    assert handler_calls == ["admin"]


def test_auth_errors_propagate_safe_request_id_and_replace_unsafe_ids(
    auth_client: tuple[TestClient, Session, list[str]],
) -> None:
    client, _db, _handler_calls = auth_client

    supplied = client.get(
        "/protected",
        headers={"X-Request-ID": "req_auth-test-123"},
    )
    assert supplied.status_code == 401
    assert supplied.headers["X-Request-ID"] == "req_auth-test-123"
    assert supplied.json()["request_id"] == "req_auth-test-123"

    unsafe = client.get(
        "/protected",
        headers={"X-Request-ID": "unsafe/request id"},
    )
    assert unsafe.status_code == 401
    generated = unsafe.headers["X-Request-ID"]
    assert generated.startswith("req_")
    assert generated != "unsafe/request id"
    assert unsafe.json()["request_id"] == generated
