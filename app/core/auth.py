"""Bearer authentication and database-backed authorization dependencies.

JWT validation remains in :mod:`app.core.security`.  This module handles the
request boundary around it: extracting the bearer credential, loading the
current user from the database, enforcing the current status and role, and
supporting routes where authentication is optional.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.types import UserRoleEnum, UserStatusEnum
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import JWTSettings, _invalid_token, decode_jwt
from app.database import get_db
from app.users.model import UserModel


def extract_bearer_token(authorization: str | None) -> str:
    """Extract a single JWT from an RFC-style ``Authorization`` header."""

    if not authorization or not isinstance(authorization, str):
        raise _invalid_token()

    scheme, separator, credentials = authorization.partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not credentials
        or credentials != credentials.strip()
        or " " in credentials
    ):
        raise _invalid_token()
    return credentials


def validate_bearer_token(
    authorization: str | None,
    settings: JWTSettings | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate the Bearer scheme and its signed JWT claims."""

    return decode_jwt(extract_bearer_token(authorization), settings, now=now)


def _load_user_from_claims(claims: dict[str, Any], db: Session) -> UserModel:
    """Load the user named by a validated JWT subject.

    The subject is parsed as a UUID before it reaches SQLAlchemy.  A malformed,
    missing, or unknown subject has the same public response as any other
    invalid credential and never reveals whether a user exists.
    """

    subject = claims.get("sub")
    try:
        user_id = UUID(subject) if isinstance(subject, str) else None
    except (TypeError, ValueError, AttributeError):
        user_id = None

    if user_id is None:
        raise _invalid_token()

    user = db.scalar(select(UserModel).where(UserModel.id == user_id))
    if user is None:
        raise _invalid_token()
    return user


def _authenticate_user(
    authorization: str | None,
    db: Session,
    settings: JWTSettings | None = None,
    *,
    now: float | None = None,
) -> UserModel:
    """Validate a token and resolve its current persisted user."""

    claims = validate_bearer_token(authorization, settings, now=now)
    return _load_user_from_claims(claims, db)


def get_current_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> UserModel:
    """Authenticate the request and require the persisted user to be active.

    The role and status are deliberately read from the current database row,
    not trusted from optional JWT claims.  This makes deactivation effective
    immediately even when a previously issued token has not expired.
    """

    user = _authenticate_user(authorization, db)
    if user.status != UserStatusEnum.ACTIVE:
        raise UnauthorizedError(detail="Authentication is required or invalid.")
    return user


def require_active_user(
    current_user: UserModel = Depends(get_current_user),
) -> UserModel:
    """Provide an authenticated active user to protected route handlers."""

    # Keep this check even though get_current_user currently performs it.  It
    # makes the dependency safe if callers override get_current_user in tests
    # or replace the authentication dependency with another implementation.
    if current_user.status != UserStatusEnum.ACTIVE:
        raise UnauthorizedError(detail="Authentication is required or invalid.")
    return current_user


def require_admin(
    current_user: UserModel = Depends(require_active_user),
) -> UserModel:
    """Require an active authenticated user with the administrator role."""

    if current_user.role != UserRoleEnum.ADMIN:
        raise ForbiddenError()
    return current_user


def get_optional_current_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> UserModel | None:
    """Resolve an active user when credentials are supplied, else return None.

    Public product routes may omit ``Authorization``.  A supplied malformed,
    expired, unknown, or inactive credential is still rejected instead of being
    silently treated as anonymous access.
    """

    if authorization is None:
        return None
    return get_current_user(authorization=authorization, db=db)


# Short name for route declarations and compatibility with callers that use
# the task's ``optional user`` terminology.
get_optional_user = get_optional_current_user
