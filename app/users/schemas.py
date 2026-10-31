"""Pydantic schemas and safe public projections for users."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.common.responses import PageMeta
from app.common.types import UserRoleEnum, UserStatusEnum


class _UserSchema(BaseModel):
    """Shared strict configuration for user request and response schemas."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class CreateCustomerRequest(_UserSchema):
    """Public customer registration payload.

    Role and status are intentionally absent.  Registration always creates an
    active customer, so callers cannot elevate privileges through this model.
    """

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class UpdateUserRequest(_UserSchema):
    """Merge-patch fields accepted by user profile management.

    Optional values represent omitted fields.  Explicit ``null`` is rejected
    because the OpenAPI contract does not make any of these fields nullable.
    ``model_fields_set`` remains available to callers to distinguish omitted
    fields from fields supplied in the request.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    email: EmailStr | None = None
    role: UserRoleEnum | None = None
    status: UserStatusEnum | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> "UpdateUserRequest":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        if any(
            field in self.model_fields_set and getattr(self, field) is None
            for field in ("name", "email", "role", "status")
        ):
            raise ValueError("User update fields cannot be null")
        return self

    def changes(self) -> dict[str, Any]:
        """Return only supplied, validated fields for repository updates."""

        return self.model_dump(exclude_unset=True)


class ChangePasswordRequest(_UserSchema):
    """Payload for changing a user's password."""

    current_password: str = Field(min_length=12, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class UserResponse(_UserSchema):
    """Credential-free representation of a persisted user."""

    id: UUID
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    role: UserRoleEnum
    status: UserStatusEnum
    created_at: datetime
    updated_at: datetime


class UserPage(_UserSchema):
    """Cursor-pagination envelope for user projections."""

    data: list[UserResponse]
    meta: PageMeta


def user_response_from_model(user: Any) -> UserResponse:
    """Project a user model explicitly, excluding its password hash.

    Explicit field selection is deliberate: adding a sensitive column to
    ``UserModel`` cannot make it appear in an HTTP response accidentally.
    """

    return UserResponse(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


# Keep the enum names available from this module for existing callers.
__all__ = [
    "ChangePasswordRequest",
    "CreateCustomerRequest",
    "PageMeta",
    "UpdateUserRequest",
    "UserPage",
    "UserResponse",
    "UserRoleEnum",
    "UserStatusEnum",
    "user_response_from_model",
]
