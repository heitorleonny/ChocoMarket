"""Reusable SQLAlchemy model mixins."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Mapped, mapped_column

from app.common.types import UTCDateTime, UUIDType, utc_now


class UUIDPrimaryKeyMixin:
    """Provide a UUID primary key compatible with SQLite and API schemas."""

    id: Mapped[UUID] = mapped_column(
        UUIDType(),
        primary_key=True,
        default=uuid4,
    )


class TimestampMixin:
    """Provide required UTC creation and modification timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class CommonModelMixin(UUIDPrimaryKeyMixin, TimestampMixin):
    """Combine the standard identifier and timestamp conventions."""

    __abstract__ = True
