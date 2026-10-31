"""HTTP routes for customer order creation and authenticated listing."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Response, status
from sqlalchemy.orm import Session

from app.common.types import OrderStatusEnum
from app.core.auth import require_active_user
from app.database import get_db
from app.orders.schemas import CreateOrderRequest, OrderPage, OrderResponse
from app.orders.service import (
    create_order_customer,
    cancel_order as cancel_order_service,
    get_order as get_order_service,
    list_orders as list_orders_service,
)
from app.users.model import UserModel

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[UserModel, Depends(require_active_user)]
RequiredIdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key")]

router = APIRouter(prefix="/v1/orders", tags=["Orders"])


@router.get(
    "",
    response_model=OrderPage,
    status_code=status.HTTP_200_OK,
)
def list_orders(
    db: DbSession,
    current_user: CurrentUser,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    order_status: Annotated[
        OrderStatusEnum | None,
        Query(alias="status"),
    ] = None,
    created_after: Annotated[datetime | None, Query()] = None,
    created_before: Annotated[datetime | None, Query()] = None,
    user_id: Annotated[UUID | None, Query()] = None,
) -> OrderPage:
    """List orders within the requester's authorized view."""

    return list_orders_service(
        db,
        current_user,
        status=order_status,
        created_after=created_after,
        created_before=created_before,
        user_id=user_id,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_order(
    payload: CreateOrderRequest,
    db: DbSession,
    current_user: CurrentUser,
    response: Response,
    idempotency_key: RequiredIdempotencyHeader,
) -> OrderResponse:
    """Create one confirmed order and decrement its products atomically."""

    result = create_order_customer(
        db,
        current_user,
        payload,
        idempotency_key=idempotency_key,
    )
    response.headers["Location"] = f"/v1/orders/{result.id}"
    return result


@router.get(
    "/{orderId}",
    response_model=OrderResponse,
    status_code=status.HTTP_200_OK,
)
def get_order(
    order_id: Annotated[UUID, Path(alias="orderId")],
    db: DbSession,
    current_user: CurrentUser,
) -> OrderResponse:
    """Return an order visible to the authenticated requester."""

    return get_order_service(db, current_user, order_id)


@router.post(
    "/{orderId}/cancel",
    response_model=OrderResponse,
    status_code=status.HTTP_200_OK,
)
def cancel_order(
    order_id: Annotated[UUID, Path(alias="orderId")],
    db: DbSession,
    current_user: CurrentUser,
) -> OrderResponse:
    """Cancel an order and restore its stock in one transaction."""

    return cancel_order_service(db, current_user, order_id)
