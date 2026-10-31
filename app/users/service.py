from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException, status

from app.users.model import UserModel
from app.users.schemas import CreateCustomerRequest, UserResponse, UserRoleEnum, UserStatusEnum
from app.core.security import hash_user_password

users_by_email: dict[str, UserModel] = {}



def create_customer(payload: CreateCustomerRequest) -> UserResponse:
    normalized_email = str(payload.email).lower()

    if normalized_email in users_by_email:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists.",
        )

    now = datetime.now(UTC)
    user = UserModel(
        id=uuid4(),
        name=payload.name.strip(),
        email=normalized_email,
        password=hash_user_password(payload.password),
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    user_response = UserResponse(
        id=user.id,
        name=payload.name.strip(),
        email=normalized_email,
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    
    users_by_email[normalized_email] = user
    return user_response