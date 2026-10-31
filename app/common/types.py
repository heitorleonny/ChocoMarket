"""Shared persistence types and domain enums.

SQLite does not have native UUID or timezone-aware datetime columns.  The
custom SQLAlchemy types below keep the database representation portable while
returning the canonical Python values used by the HTTP schemas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UUIDType(TypeDecorator[UUID]):
    """Store UUIDs as canonical strings and expose ``uuid.UUID`` values.

    The string representation is compatible with SQLite and existing ChocoMarket
    rows.  Both UUID instances and UUID-shaped strings are accepted on writes;
    malformed values fail at the persistence boundary instead of being stored
    as arbitrary text.
    """

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value: UUID | str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        try:
            return str(value if isinstance(value, UUID) else UUID(str(value)))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("UUID value must be a valid UUID") from exc

    def process_result_value(self, value: Any, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        try:
            return value if isinstance(value, UUID) else UUID(str(value))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Stored UUID value is invalid") from exc


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC datetimes portably and return timezone-aware UTC values."""

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Datetime value must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""

    return datetime.now(UTC)


class UserRoleEnum(str, Enum):
    CUSTOMER = "customer"
    ADMIN = "admin"


class UserStatusEnum(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class ProductStatusEnum(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    OUT_OF_STOCK = "out_of_stock"


class OrderStatusEnum(str, Enum):
    CONFIRMED = "confirmed"
    CANCELED = "canceled"
