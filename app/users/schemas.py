from datetime import datetime
from uuid import UUID
from enum import Enum
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserRoleEnum(str, Enum):
    CUSTOMER = "customer"
    ADMIN = "admin"

class UserStatusEnum(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"

class CreateCustomerRequest(BaseModel):
    model_config= ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)

class UserResponse(BaseModel):
    id: UUID
    name: str
    email: EmailStr
    role: UserRoleEnum
    status: UserStatusEnum
    created_at: datetime
    updated_at: datetime
