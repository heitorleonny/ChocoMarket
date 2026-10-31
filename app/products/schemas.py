"""Pydantic schemas for product requests and public responses."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.common.responses import PageMeta
from app.common.types import ProductStatusEnum


class CreateProductRequest(BaseModel):
    """Validated payload used to create a product."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    price_cents: StrictInt = Field(ge=1, le=1_000_000)
    stock_quantity: StrictInt = Field(ge=0, le=1_000_000)


class UpdateProductRequest(BaseModel):
    """Non-empty merge-patch payload for an existing product.

    ``model_fields_set`` is retained by Pydantic, so callers can distinguish an
    omitted field from a field supplied with a value when applying the patch.
    Explicit ``null`` is rejected because the OpenAPI contract does not make
    any product field nullable.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    price_cents: StrictInt | None = Field(default=None, ge=1, le=1_000_000)
    stock_quantity: StrictInt | None = Field(default=None, ge=0, le=1_000_000)
    status: ProductStatusEnum | None = None

    @field_validator(
        "name",
        "description",
        "price_cents",
        "stock_quantity",
        "status",
        mode="before",
    )
    @classmethod
    def reject_null_values(cls, value: object) -> object:
        if value is None:
            raise ValueError("null is not allowed for product patch fields")
        return value

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "UpdateProductRequest":
        if not self.model_fields_set:
            raise ValueError("at least one product field must be provided")
        return self


class ProductResponse(BaseModel):
    """Public product representation with no persistence-only fields."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    name: str
    description: str
    price_cents: int = Field(ge=1, le=1_000_000)
    stock_quantity: int = Field(ge=0, le=1_000_000)
    status: ProductStatusEnum
    created_at: datetime
    updated_at: datetime


class ProductPage(BaseModel):
    """Public product list envelope."""

    model_config = ConfigDict(extra="forbid")

    data: list[ProductResponse]
    meta: PageMeta
