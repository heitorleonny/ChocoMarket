import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.common.types import UserRoleEnum, UserStatusEnum
from app.core.auth import (
    get_current_user,
    get_optional_current_user,
    require_active_user,
    require_admin,
)
from app.core.errors import ForbiddenError, UnauthorizedError
from app.database import Base, create_database_engine, create_session_factory
from app.users.model import UserModel


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token(user_id: str, *, role: str = "customer") -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "sub": user_id,
        "role": role,
        "iss": "test-issuer",
        "aud": "test-audience",
        "exp": time.time() + 300,
    }
    encoded_header = _base64url(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _base64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = hmac.new(b"test-secret", signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_claims}.{_base64url(signature)}"


@pytest.fixture(autouse=True)
def jwt_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHOCOMARKET_JWT_SECRET", "test-secret")
    monkeypatch.setenv("CHOCOMARKET_JWT_ISSUER", "test-issuer")
    monkeypatch.setenv("CHOCOMARKET_JWT_AUDIENCE", "test-audience")


@pytest.fixture
def db() -> Session:
    database_engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    session_factory = create_session_factory(database_engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        database_engine.dispose()


def _create_user(
    db: Session,
    *,
    role: UserRoleEnum = UserRoleEnum.CUSTOMER,
    status: UserStatusEnum = UserStatusEnum.ACTIVE,
) -> UserModel:
    now = datetime.now(UTC)
    user = UserModel(
        id=uuid4(),
        name="Test User",
        email=f"{uuid4()}@example.com",
        password="argon2-hash",
        role=role,
        status=status,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_get_current_user_loads_active_user_from_database(db: Session) -> None:
    user = _create_user(db, role=UserRoleEnum.ADMIN)

    authenticated = get_current_user(
        authorization=f"Bearer {_token(str(user.id), role='customer')}",
        db=db,
    )

    assert authenticated.id == user.id
    assert authenticated.role is UserRoleEnum.ADMIN


def test_get_current_user_rejects_inactive_persisted_user(db: Session) -> None:
    user = _create_user(db, status=UserStatusEnum.INACTIVE)

    with pytest.raises(UnauthorizedError) as error:
        get_current_user(
            authorization=f"Bearer {_token(str(user.id))}",
            db=db,
        )

    assert error.value.status_code == 401


def test_require_active_user_rechecks_status() -> None:
    user = UserModel(
        id=uuid4(),
        name="Inactive User",
        email="inactive@example.com",
        password="argon2-hash",
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.INACTIVE,
    )

    with pytest.raises(UnauthorizedError):
        require_active_user(current_user=user)


def test_require_admin_rejects_customer_without_database_mutation(db: Session) -> None:
    user = _create_user(db, role=UserRoleEnum.CUSTOMER)

    with pytest.raises(ForbiddenError) as error:
        require_admin(current_user=user)

    assert error.value.status_code == 403
    assert db.get(UserModel, user.id).role is UserRoleEnum.CUSTOMER


def test_require_admin_accepts_active_admin(db: Session) -> None:
    user = _create_user(db, role=UserRoleEnum.ADMIN)

    assert require_admin(current_user=user) is user


def test_missing_or_malformed_credentials_are_rejected(db: Session) -> None:
    with pytest.raises(UnauthorizedError):
        get_current_user(authorization=None, db=db)

    with pytest.raises(UnauthorizedError):
        get_current_user(authorization="Basic credentials", db=db)


def test_optional_auth_allows_anonymous_and_resolves_active_user(db: Session) -> None:
    assert get_optional_current_user(authorization=None, db=db) is None

    user = _create_user(db)
    authenticated = get_optional_current_user(
        authorization=f"Bearer {_token(str(user.id))}",
        db=db,
    )

    assert authenticated is not None
    assert authenticated.id == user.id


def test_optional_auth_rejects_supplied_invalid_credentials(db: Session) -> None:
    with pytest.raises(UnauthorizedError):
        get_optional_current_user(authorization="Bearer malformed", db=db)
