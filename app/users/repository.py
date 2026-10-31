"""Persistence operations for users.

Repositories contain database queries and persistence mechanics only.  They do
not decide whether a requester may access a user; that belongs to the service
and authentication layers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.common.types import UserRoleEnum, UserStatusEnum
from app.core.pagination import OrderingKey
from app.database import casefolded
from app.users.model import UserModel


_USER_UPDATE_FIELDS = frozenset({"name", "email", "role", "status"})


def normalize_user_email(email: str) -> str:
    """Return the canonical form used for user lookup and persistence."""

    return str(email).strip().lower()


def get_by_id(db: Session, user_id: UUID | str) -> UserModel | None:
    """Return a user by UUID, or ``None`` when it does not exist."""

    return db.get(UserModel, user_id)


def get_by_email(db: Session, email: str) -> UserModel | None:
    """Return a user by normalized e-mail address."""

    normalized_email = normalize_user_email(email)
    return db.scalar(select(UserModel).where(UserModel.email == normalized_email))


def list_users(
    db: Session,
    *,
    name: str | None = None,
    email: str | None = None,
    role: UserRoleEnum | str | None = None,
    status: UserStatusEnum | str | None = UserStatusEnum.ACTIVE,
) -> list[UserModel]:
    """Return users matching all supplied filters in stable order.

    The default active-status filter matches the public contract.  Passing
    ``status=None`` is reserved for administrative callers that need all
    statuses; cursor slicing belongs to the pagination layer.
    """

    statement = select(UserModel)
    if name is not None:
        statement = statement.where(
            casefolded(UserModel.name).contains(
                name.strip().casefold(), autoescape=True
            )
        )
    if email is not None:
        statement = statement.where(
            UserModel.email == normalize_user_email(email)
        )
    if role is not None:
        statement = statement.where(UserModel.role == role)
    if status is not None:
        statement = statement.where(UserModel.status == status)

    statement = statement.order_by(UserModel.created_at, UserModel.id)
    return list(db.scalars(statement).all())


def create(db: Session, user: UserModel) -> UserModel:
    """Add a user and flush it without committing the caller's transaction."""

    user.email = normalize_user_email(user.email)
    db.add(user)
    db.flush()
    return user


def update(
    db: Session,
    user: UserModel,
    changes: Mapping[str, Any],
) -> UserModel:
    """Apply validated profile changes and flush without committing.

    Restricting fields here prevents an accidental profile update from writing
    the password hash or other internal columns, even if a caller bypasses the
    Pydantic request model.
    """

    unknown_fields = set(changes) - _USER_UPDATE_FIELDS
    if unknown_fields:
        names = ", ".join(sorted(unknown_fields))
        raise ValueError(f"Unsupported user update field(s): {names}")

    for field, value in changes.items():
        if field == "email":
            value = normalize_user_email(value)
        setattr(user, field, value)

    db.flush()
    return user


def list_users_page(
    db: Session,
    *,
    name: str | None = None,
    email: str | None = None,
    role: UserRoleEnum | str | None = None,
    status: UserStatusEnum | str | None = UserStatusEnum.ACTIVE,
    after: OrderingKey | None = None,
    limit: int = 20,
) -> list[UserModel]:
    """Return one keyset-paginated page of users in stable order.

    ``after`` is the last ordering key from the previous page.  Filtering is
    applied before the keyset predicate so cursors cannot skip across a
    different result set.
    """

    statement = select(UserModel)
    if name is not None:
        statement = statement.where(
            casefolded(UserModel.name).contains(
                name.strip().casefold(), autoescape=True
            )
        )
    if email is not None:
        statement = statement.where(
            UserModel.email == normalize_user_email(email)
        )
    if role is not None:
        statement = statement.where(UserModel.role == role)
    if status is not None:
        statement = statement.where(UserModel.status == status)
    if after is not None:
        statement = statement.where(
            or_(
                UserModel.created_at > after.created_at,
                and_(
                    UserModel.created_at == after.created_at,
                    UserModel.id > after.id,
                ),
            )
        )

    statement = (
        statement.order_by(UserModel.created_at, UserModel.id)
        .limit(limit + 1)
    )
    return list(db.scalars(statement).all())


# Descriptive aliases make repository call sites self-documenting while the
# short names above remain compatible with the design's conceptual interface.
get_user_by_id = get_by_id
get_user_by_email = get_by_email
create_user = create
update_user = update
list_user_models = list_users
