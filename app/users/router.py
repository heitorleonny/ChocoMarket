from fastapi import APIRouter, status

from app.users.schemas import CreateCustomerRequest, UserResponse
from app.users.service import create_customer as create_customer_service


router = APIRouter(prefix="/v1/users", tags=["Users"])


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_customer(payload: CreateCustomerRequest) -> UserResponse:
    return create_customer_service(payload)