from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Enum as SAEnum, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.models import TimestampMixin, UUIDPrimaryKeyMixin
from app.common.types import UserRoleEnum, UserStatusEnum
from app.database import Base

if TYPE_CHECKING:
    from app.orders.model import OrderModel


class UserModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_status_created_at_id", "status", "created_at", "id"),
        Index("ix_users_role", "role"),
        Index("ix_users_name", "name"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        unique=True,
        index=True,
    )
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRoleEnum] = mapped_column(
        SAEnum(
            UserRoleEnum,
            native_enum=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=20,
        ),
        nullable=False,
    )
    status: Mapped[UserStatusEnum] = mapped_column(
        SAEnum(
            UserStatusEnum,
            native_enum=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=20,
        ),
        nullable=False,
    )

    orders: Mapped[list[OrderModel]] = relationship(back_populates="user")


# Register order mappings when UserModel is imported on its own.
from app.orders.model import OrderModel  # noqa: E402,F401
from app.core.idempotency import IdempotencyRecordModel  # noqa: E402,F401
