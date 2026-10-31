"""SQLAlchemy persistence models for orders and order items."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import CheckConstraint, Enum as SAEnum, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.models import CommonModelMixin
from app.common.types import OrderStatusEnum, UUIDType
from app.database import Base

if TYPE_CHECKING:
    from app.products.model import ProductModel
    from app.users.model import UserModel


class OrderModel(CommonModelMixin, Base):
    """A confirmed or canceled order owned by one persisted user."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "total_cents >= 1",
            name="ck_orders_total_cents_positive",
        ),
        Index("ix_orders_user_id_created_at_id", "user_id", "created_at", "id"),
        Index("ix_orders_status_created_at_id", "status", "created_at", "id"),
        Index("ix_orders_created_at_id", "created_at", "id"),
    )

    user_id: Mapped[UUID] = mapped_column(
        UUIDType(),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[OrderStatusEnum] = mapped_column(
        SAEnum(
            OrderStatusEnum,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
            length=20,
            name="order_status",
        ),
        nullable=False,
        default=OrderStatusEnum.CONFIRMED,
    )
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    user: Mapped[UserModel] = relationship(back_populates="orders")
    items: Mapped[list[OrderItemModel]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItemModel.id",
    )


class OrderItemModel(CommonModelMixin, Base):
    """A price snapshot and quantity for one product in an order."""

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint(
            "quantity BETWEEN 1 AND 100",
            name="ck_order_items_quantity_range",
        ),
        CheckConstraint(
            "unit_price_cents BETWEEN 1 AND 1000000",
            name="ck_order_items_unit_price_cents_range",
        ),
        CheckConstraint(
            "subtotal_cents >= 1",
            name="ck_order_items_subtotal_cents_positive",
        ),
        CheckConstraint(
            "subtotal_cents = quantity * unit_price_cents",
            name="ck_order_items_subtotal_matches_quantity_price",
        ),
        UniqueConstraint(
            "order_id",
            "product_id",
            name="uq_order_items_order_product",
        ),
        Index("ix_order_items_order_id", "order_id"),
        Index("ix_order_items_product_id", "product_id"),
    )

    order_id: Mapped[UUID] = mapped_column(
        UUIDType(),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[UUID] = mapped_column(
        UUIDType(),
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped[OrderModel] = relationship(back_populates="items")
    product: Mapped[ProductModel] = relationship(back_populates="order_items")


# Register related models when this module is imported directly.  The imports
# occur after class declarations so the string relationships resolve safely.
from app.products.model import ProductModel  # noqa: E402,F401
from app.users.model import UserModel  # noqa: E402,F401
