from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Response, status
from pydantic import EmailStr
from sqlalchemy.orm import Session

from app.core.auth import require_active_user, require_admin
from app.database import get_db

from app.common.types import UserRoleEnum, UserStatusEnum
from app.users.model import UserModel
from app.users.schemas import (
    ChangePasswordRequest,
    CreateCustomerRequest,
    UpdateUserRequest,
    UserPage,
    UserResponse,
)
from app.users.service import (
    change_user_password as change_user_password_service,
    create_customer as create_customer_service,
    deactivate_user as deactivate_user_service,
    get_user as get_user_service,
    list_users_admin as list_users_service,
    update_user as update_user_service,
)

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[UserModel, Depends(require_active_user)]
AdminUser = Annotated[UserModel, Depends(require_admin)]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]

router = APIRouter(prefix="/v1/users", tags=["Users"])


@router.get(
    "",
    response_model=UserPage,
    status_code=status.HTTP_200_OK,
)
def list_users(
    db: DbSession,
    _admin: AdminUser,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    name: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    email: Annotated[EmailStr | None, Query()] = None,
    role: Annotated[UserRoleEnum | None, Query()] = None,
    user_status: Annotated[
        UserStatusEnum | None,
        Query(alias="status"),
    ] = None,
) -> UserPage:
    """Return the administrator's filterable, cursor-paginated user view."""

    return list_users_service(
        db,
        name=name,
        email=str(email) if email is not None else None,
        role=role,
        status=user_status,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_customer(
    payload: CreateCustomerRequest,
    db: DbSession,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
) -> UserResponse:
    result = create_customer_service(
        payload,
        db,
        idempotency_key=idempotency_key,
    )
    response.headers["Location"] = f"/v1/users/{result.id}"
    return result


@router.get(
    "/{userId}",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
)
def get_user(
    user_id: Annotated[UUID, Path(alias="userId")],
    db: DbSession,
    current_user: CurrentUser,
) -> UserResponse:
    return get_user_service(db, current_user, user_id)


@router.patch(
    "/{userId}",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
)
def update_user(
    user_id: Annotated[UUID, Path(alias="userId")],
    payload: UpdateUserRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> UserResponse:
    return update_user_service(db, current_user, user_id, payload)


@router.delete(
    "/{userId}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def deactivate_user(
    user_id: Annotated[UUID, Path(alias="userId")],
    db: DbSession,
    current_user: CurrentUser,
) -> Response:
    deactivate_user_service(db, current_user, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{userId}/password",
    status_code=status.HTTP_204_NO_CONTENT,
)
def change_user_password(
    user_id: Annotated[UUID, Path(alias="userId")],
    payload: ChangePasswordRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> Response:
    change_user_password_service(
        db,
        current_user,
        user_id,
        payload.current_password,
        payload.new_password,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
