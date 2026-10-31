"""SQLAlchemy persistence model for catalog products."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Enum as SAEnum, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.models import CommonModelMixin
from app.common.types import ProductStatusEnum
from app.database import Base

if TYPE_CHECKING:
    from app.orders.model import OrderItemModel


class ProductModel(CommonModelMixin, Base):
    """A catalog product whose status is derived by the product service."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(
            "length(name) BETWEEN 1 AND 160",
            name="ck_products_name_length",
        ),
        CheckConstraint(
            "length(description) <= 2000",
            name="ck_products_description_length",
        ),
        CheckConstraint(
            "price_cents BETWEEN 1 AND 1000000",
            name="ck_products_price_cents_range",
        ),
        CheckConstraint(
            "stock_quantity BETWEEN 0 AND 1000000",
            name="ck_products_stock_quantity_range",
        ),
        Index("ix_products_status", "status"),
        Index("ix_products_status_created_at_id", "status", "created_at", "id"),
        Index("ix_products_price_cents", "price_cents"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(
        String(2000),
        nullable=False,
        default="",
        server_default="",
    )
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ProductStatusEnum] = mapped_column(
        SAEnum(
            ProductStatusEnum,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
            length=20,
            name="product_status",
        ),
        nullable=False,
    )

    order_items: Mapped[list[OrderItemModel]] = relationship(
        back_populates="product",
        passive_deletes=True,
    )


# Register the related model when this module is imported directly.  This keeps
# Base.metadata and SQLAlchemy mapper configuration complete for isolated tests.
from app.orders.model import OrderItemModel  # noqa: E402,F401
