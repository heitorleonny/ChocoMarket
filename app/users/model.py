from pydantic import BaseModel, EmailStr
from uuid import UUID
from datetime import datetime
from app.users.schemas import UserRoleEnum, UserStatusEnum

class UserModel(BaseModel):
    id: UUID
    name: str
    email: EmailStr
    password: str
    role: UserRoleEnum
    status: UserStatusEnum
    created_at: datetime
    updated_at: datetime