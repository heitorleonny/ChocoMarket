from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.common.types import UserRoleEnum, UserStatusEnum
from app.database import Base, create_database_engine, create_session_factory
from app.users.model import UserModel
from app.users.repository import (
    create,
    get_by_email,
    get_by_id,
    list_users,
    normalize_user_email,
    update,
)
from app.users.schemas import (
    CreateCustomerRequest,
    UpdateUserRequest,
    UserResponse,
    user_response_from_model,
)


@pytest.fixture()
def db():
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_user(
    *,
    email: str,
    name: str = "Ana Souza",
    status: UserStatusEnum = UserStatusEnum.ACTIVE,
    role: UserRoleEnum = UserRoleEnum.CUSTOMER,
) -> UserModel:
    now = datetime.now(UTC)
    return UserModel(
        id=uuid4(),
        name=name,
        email=email,
        password="argon2-hash",
        role=role,
        status=status,
        created_at=now,
        updated_at=now,
    )


def test_update_request_is_strict_and_distinguishes_omitted_fields() -> None:
    patch = UpdateUserRequest(name="Ana Atualizada")

    assert patch.model_fields_set == {"name"}
    assert patch.changes() == {"name": "Ana Atualizada"}

    with pytest.raises(ValidationError):
        UpdateUserRequest()
    with pytest.raises(ValidationError):
        UpdateUserRequest(email=None)
    with pytest.raises(ValidationError):
        CreateCustomerRequest(
            name="Ana",
            email="ana@example.com",
            password="UmaSenhaSeguraCom12Caracteres",
            role="admin",
        )


def test_user_projection_never_exposes_password() -> None:
    user = make_user(email="ana@example.com")

    projection = user_response_from_model(user)

    assert projection.id == user.id
    assert projection.email == "ana@example.com"
    assert "password" not in projection.model_dump()
    assert "password_hash" not in projection.model_dump()


def test_repository_uses_persisted_users_for_lookup_filters_and_updates(db) -> None:
    ana = create(db, make_user(email="ANA@example.com", name="Ana Souza"))
    bruno = create(
        db,
        make_user(
            email="bruno@example.com",
            name="Bruno Lima",
            status=UserStatusEnum.INACTIVE,
            role=UserRoleEnum.ADMIN,
        ),
    )
    db.commit()

    assert normalize_user_email(" ANA@EXAMPLE.COM ") == "ana@example.com"
    assert get_by_id(db, ana.id) is ana
    assert get_by_email(db, " ANA@example.com ") is ana
    assert list_users(db, name="ANA") == [ana]
    assert list_users(db, status=UserStatusEnum.INACTIVE) == [bruno]
    assert list_users(db, status=None, role=UserRoleEnum.ADMIN) == [bruno]

    update(db, ana, {"name": "Ana Nova", "email": "ANA.NOVA@example.com"})
    db.commit()
    db.refresh(ana)

    assert ana.name == "Ana Nova"
    assert ana.email == "ana.nova@example.com"
    assert get_by_email(db, "ana.nova@example.com") is ana

    with pytest.raises(ValueError, match="Unsupported user update field"):
        update(db, ana, {"password": "new-hash"})
