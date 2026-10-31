"""Strict request and response schemas for orders."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.common.responses import PageMeta
from app.common.types import OrderStatusEnum


class _OrderSchema(BaseModel):
    """Shared strict configuration for order schemas."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class CreateOrderItemRequest(_OrderSchema):
    """One product and quantity requested for a new order."""

    product_id: UUID
    quantity: StrictInt = Field(ge=1, le=100)


class CreateOrderRequest(_OrderSchema):
    """Validated order creation payload.

    Product identity is unique within one request.  Checking it here gives
    callers a field-level validation error before a repository is touched.
    """

    items: list[CreateOrderItemRequest] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_duplicate_products(self) -> "CreateOrderRequest":
        product_ids = [item.product_id for item in self.items]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("each product_id may appear only once in an order")
        return self


class OrderItemResponse(_OrderSchema):
    """Public order-item representation containing the purchase-time price."""

    id: UUID
    product_id: UUID
    quantity: StrictInt = Field(ge=1, le=100)
    unit_price_cents: StrictInt = Field(ge=1, le=1_000_000)
    subtotal_cents: StrictInt = Field(ge=1)


class OrderResponse(_OrderSchema):
    """Public order representation with price snapshots and totals."""

    id: UUID
    user_id: UUID
    status: OrderStatusEnum
    total_cents: StrictInt = Field(ge=1)
    items: list[OrderItemResponse] = Field(min_length=1, max_length=50)
    created_at: datetime
    updated_at: datetime


class OrderPage(_OrderSchema):
    """Cursor-pagination envelope for order responses."""

    data: list[OrderResponse]
    meta: PageMeta


__all__ = [
    "CreateOrderItemRequest",
    "CreateOrderRequest",
    "OrderItemResponse",
    "OrderPage",
    "OrderResponse",
    "PageMeta",
]
