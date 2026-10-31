"""HTTP routes for public and administrative product reads."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Response, status
from sqlalchemy.orm import Session

from app.common.types import ProductStatusEnum
from app.core.auth import get_optional_current_user, require_admin
from app.database import get_db
from app.products.schemas import (
    CreateProductRequest,
    ProductPage,
    ProductResponse,
    UpdateProductRequest,
)
from app.products.service import (
    create_product_admin as create_product_service,
    deactivate_product_admin as deactivate_product_service,
    get_product as get_product_service,
    list_products as list_products_service,
    update_product_admin as update_product_service,
)
from app.users.model import UserModel

DbSession = Annotated[Session, Depends(get_db)]
OptionalUser = Annotated[UserModel | None, Depends(get_optional_current_user)]
AdminUser = Annotated[UserModel, Depends(require_admin)]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]

router = APIRouter(prefix="/v1/products", tags=["Products"])


@router.get(
    "",
    response_model=ProductPage,
    status_code=200,
)
def list_products(
    db: DbSession,
    requester: OptionalUser,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    name: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
    min_price_cents: Annotated[int | None, Query(ge=1, le=1_000_000)] = None,
    max_price_cents: Annotated[int | None, Query(ge=1, le=1_000_000)] = None,
    product_status: Annotated[
        ProductStatusEnum | None,
        Query(alias="status"),
    ] = None,
) -> ProductPage:
    """List visible products with combinable filters and cursor pagination."""

    return list_products_service(
        db,
        requester,
        name=name,
        min_price_cents=min_price_cents,
        max_price_cents=max_price_cents,
        status=product_status,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_product(
    payload: CreateProductRequest,
    db: DbSession,
    _admin: AdminUser,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
) -> ProductResponse:
    """Create a product using backend-controlled stock status."""

    result = create_product_service(
        db,
        _admin,
        payload,
        idempotency_key=idempotency_key,
    )
    response.headers["Location"] = f"/v1/products/{result.id}"
    return result


@router.get(
    "/{productId}",
    response_model=ProductResponse,
    status_code=200,
)
def get_product(
    product_id: Annotated[UUID, Path(alias="productId")],
    db: DbSession,
    requester: OptionalUser,
) -> ProductResponse:
    """Return an active product publicly or any product to an administrator."""

    return get_product_service(db, requester, product_id)


@router.patch(
    "/{productId}",
    response_model=ProductResponse,
    status_code=status.HTTP_200_OK,
)
def update_product(
    product_id: Annotated[UUID, Path(alias="productId")],
    payload: UpdateProductRequest,
    db: DbSession,
    _admin: AdminUser,
) -> ProductResponse:
    """Apply an administrator-only partial product update."""

    return update_product_service(db, product_id, payload)


@router.delete(
    "/{productId}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def deactivate_product(
    product_id: Annotated[UUID, Path(alias="productId")],
    db: DbSession,
    _admin: AdminUser,
) -> Response:
    """Logically deactivate a product, safely allowing repeated deletion."""

    deactivate_product_service(db, product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
