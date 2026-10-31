"""Pure product-domain rules and patch application helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationDomainError
from app.core.pagination import PaginationService
from app.products.model import ProductModel
from app.products.schemas import (
    CreateProductRequest,
    PageMeta,
    ProductPage,
    ProductResponse,
    UpdateProductRequest,
)
from app.common.types import ProductStatusEnum, UserRoleEnum, utc_now
from app.core.idempotency import (
    IdempotencyRaceError,
    mutation_scope,
    replay_or_conflict,
    request_fingerprint,
    store_record,
    validate_idempotency_key,
)
from app.users.model import UserModel

_UNSET = object()


def validate_product_values(
    *,
    name: str,
    description: str,
    price_cents: int,
    stock_quantity: int,
) -> None:
    """Validate product values for callers that do not use Pydantic schemas.

    HTTP requests are validated by ``CreateProductRequest`` and
    ``UpdateProductRequest``.  This small domain boundary keeps repository and
    service callers subject to the same limits before a session is flushed.
    """

    if not isinstance(name, str) or not 1 <= len(name) <= 160:
        raise ValueError("name must contain between 1 and 160 characters")
    if not isinstance(description, str) or len(description) > 2_000:
        raise ValueError("description must contain at most 2000 characters")
    if isinstance(price_cents, bool) or not isinstance(price_cents, int):
        raise ValueError("price_cents must be an integer")
    if not 1 <= price_cents <= 1_000_000:
        raise ValueError("price_cents must be between 1 and 1000000")
    if isinstance(stock_quantity, bool) or not isinstance(stock_quantity, int):
        raise ValueError("stock_quantity must be an integer")
    if not 0 <= stock_quantity <= 1_000_000:
        raise ValueError("stock_quantity must be between 0 and 1000000")


def status_for_product_creation(stock_quantity: int) -> ProductStatusEnum:
    """Return the backend-controlled status for a newly created product."""

    if isinstance(stock_quantity, bool) or not isinstance(stock_quantity, int):
        raise ValueError("stock_quantity must be an integer")
    if not 0 <= stock_quantity <= 1_000_000:
        raise ValueError("stock_quantity must be between 0 and 1000000")
    return (
        ProductStatusEnum.OUT_OF_STOCK
        if stock_quantity == 0
        else ProductStatusEnum.ACTIVE
    )


def transition_product_status(
    *,
    current_status: ProductStatusEnum | None,
    stock_quantity: int,
    requested_status: ProductStatusEnum | object = _UNSET,
) -> ProductStatusEnum:
    """Apply the product status precedence rules for an update.

    Stock zero always wins and produces ``out_of_stock``.  Positive stock
    reactivates products currently ``active`` or ``out_of_stock``.  An explicit
    ``inactive`` status is preserved so logical deletion remains authoritative;
    an explicit ``active`` status is the administrator's reactivation action.
    A positive-stock product already marked ``inactive`` therefore stays
    inactive until that explicit reactivation is supplied.
    """

    if isinstance(stock_quantity, bool) or not isinstance(stock_quantity, int):
        raise ValueError("stock_quantity must be an integer")
    if not 0 <= stock_quantity <= 1_000_000:
        raise ValueError("stock_quantity must be between 0 and 1000000")

    if stock_quantity == 0:
        return ProductStatusEnum.OUT_OF_STOCK

    if requested_status is ProductStatusEnum.INACTIVE:
        return ProductStatusEnum.INACTIVE
    if requested_status is ProductStatusEnum.ACTIVE:
        return ProductStatusEnum.ACTIVE

    if current_status in {
        ProductStatusEnum.ACTIVE,
        ProductStatusEnum.OUT_OF_STOCK,
    }:
        return ProductStatusEnum.ACTIVE
    if current_status is ProductStatusEnum.INACTIVE:
        return ProductStatusEnum.INACTIVE

    # This is useful for callers constructing a new state without using the
    # create-specific helper, while preserving the positive-stock invariant.
    return ProductStatusEnum.ACTIVE


def _provided_changes(
    patch: UpdateProductRequest | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(patch, UpdateProductRequest):
        return {
            field: getattr(patch, field)
            for field in patch.model_fields_set
        }
    changes = dict(patch)
    if not changes:
        raise ValueError("at least one product field must be provided")
    return changes


def apply_product_patch(
    product: ProductModel,
    patch: UpdateProductRequest | Mapping[str, Any],
) -> ProductModel:
    """Apply a validated merge patch and derive the resulting status."""

    changes = _provided_changes(patch)
    allowed_fields = {"name", "description", "price_cents", "stock_quantity", "status"}
    unknown_fields = set(changes) - allowed_fields
    if unknown_fields:
        raise ValueError(f"unknown product fields: {sorted(unknown_fields)!r}")
    if any(value is None for value in changes.values()):
        raise ValueError("null is not allowed for product patch fields")

    name = changes.get("name", product.name)
    description = changes.get("description", product.description)
    price_cents = changes.get("price_cents", product.price_cents)
    stock_quantity = changes.get("stock_quantity", product.stock_quantity)
    validate_product_values(
        name=name,
        description=description,
        price_cents=price_cents,
        stock_quantity=stock_quantity,
    )

    requested_status = changes.get("status", _UNSET)
    product.name = name
    product.description = description
    product.price_cents = price_cents
    product.stock_quantity = stock_quantity
    product.status = transition_product_status(
        current_status=product.status,
        stock_quantity=stock_quantity,
        requested_status=requested_status,
    )
    product.updated_at = utc_now()
    return product


def product_from_create(payload: CreateProductRequest) -> ProductModel:
    """Build a product model from a validated creation payload."""

    validate_product_values(
        name=payload.name,
        description=payload.description,
        price_cents=payload.price_cents,
        stock_quantity=payload.stock_quantity,
    )
    now = utc_now()
    return ProductModel(
        name=payload.name,
        description=payload.description,
        price_cents=payload.price_cents,
        stock_quantity=payload.stock_quantity,
        status=status_for_product_creation(payload.stock_quantity),
        created_at=now,
        updated_at=now,
    )

PRODUCT_LIST_RESOURCE = "products"


def _product_view(requester: UserModel | None) -> dict[str, str | None]:
    """Return the identity of the authorized product view for cursor binding."""

    if requester is None:
        return {"role": "public", "user_id": None}
    return {"role": requester.role.value, "user_id": str(requester.id)}


def _product_filters(
    *,
    requester: UserModel | None,
    name: str | None,
    min_price_cents: int | None,
    max_price_cents: int | None,
    status: ProductStatusEnum | str | None,
) -> tuple[dict[str, object], str | None]:
    """Normalize filters and apply the public/customer active-only rule."""

    normalized_name = name.strip() if name is not None else None
    normalized_status = status.value if isinstance(status, ProductStatusEnum) else status
    is_admin = requester is not None and requester.role is UserRoleEnum.ADMIN
    if not is_admin:
        if normalized_status is not None and normalized_status != ProductStatusEnum.ACTIVE.value:
            raise ValidationDomainError(
                detail="Customers can only filter products with status active.",
                errors=[
                    {
                        "field": "status",
                        "message": "Only status active is available to this requester.",
                        "code": "visibility_status",
                    }
                ],
            )
        normalized_status = ProductStatusEnum.ACTIVE.value

    filters = {
        "name": normalized_name,
        "min_price_cents": min_price_cents,
        "max_price_cents": max_price_cents,
        "status": normalized_status,
    }
    return (
        {key: value for key, value in filters.items() if value is not None},
        normalized_status,
    )


def list_products(
    db: Session,
    requester: UserModel | None = None,
    *,
    name: str | None = None,
    min_price_cents: int | None = None,
    max_price_cents: int | None = None,
    status: ProductStatusEnum | str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    pagination: PaginationService | None = None,
) -> ProductPage:
    """List products with visibility-aware filters and signed cursors."""

    from app.products.repository import ProductRepository

    pagination_service = pagination or PaginationService()
    filters, effective_status = _product_filters(
        requester=requester,
        name=name,
        min_price_cents=min_price_cents,
        max_price_cents=max_price_cents,
        status=status,
    )
    view = _product_view(requester)
    validated_limit = pagination_service.validate_limit(limit)
    after = None
    if cursor is not None:
        after = pagination_service.decode_cursor(
            cursor,
            resource=PRODUCT_LIST_RESOURCE,
            filters=filters,
            view=view,
        ).position

    products = ProductRepository.list_page(
        db,
        name=filters.get("name"),
        min_price_cents=min_price_cents,
        max_price_cents=max_price_cents,
        status=effective_status,
        after=after,
        limit=validated_limit,
    )
    page = pagination_service.build_page(
        products,
        limit=validated_limit,
        resource=PRODUCT_LIST_RESOURCE,
        filters=filters,
        view=view,
    )
    return ProductPage(
        data=[ProductResponse.model_validate(product) for product in page.data],
        meta=PageMeta(
            limit=page.meta.limit,
            next_cursor=page.meta.next_cursor,
            has_next=page.meta.has_next,
        ),
    )


def create_product_admin(
    db: Session,
    requester: UserModel,
    payload: CreateProductRequest,
    *,
    idempotency_key: str | None = None,
) -> ProductResponse:
    """Create a product for an administrator, optionally replaying a result."""

    key = validate_idempotency_key(idempotency_key)
    scope = mutation_scope(
        actor_id=requester.id,
        method="POST",
        path="/v1/products",
    )
    fingerprint = (
        request_fingerprint(
            method="POST",
            path="/v1/products",
            body=payload.model_dump(mode="json"),
        )
        if key is not None
        else None
    )

    if key is not None and fingerprint is not None:
        stored = replay_or_conflict(
            db,
            scope=scope,
            key=key,
            request_fingerprint_value=fingerprint,
        )
        if stored is not None:
            return ProductResponse.model_validate(stored.response_body)

    from app.products.repository import ProductRepository

    product = ProductRepository.create(db, payload)
    response = ProductResponse.model_validate(product)

    if key is not None and fingerprint is not None:
        try:
            store_record(
                db,
                scope=scope,
                key=key,
                request_fingerprint_value=fingerprint,
                status_code=201,
                response_body=response.model_dump(mode="json"),
            )
        except IdempotencyRaceError:
            stored = replay_or_conflict(
                db,
                scope=scope,
                key=key,
                request_fingerprint_value=fingerprint,
            )
            assert stored is not None
            return ProductResponse.model_validate(stored.response_body)

    db.commit()
    db.refresh(product)
    return ProductResponse.model_validate(product)


def update_product_admin(
    db: Session,
    product_id: UUID,
    payload: UpdateProductRequest,
) -> ProductResponse:
    """Apply an administrator-only product merge patch."""

    from app.products.repository import ProductRepository

    product = ProductRepository.get_by_id(db, product_id)
    if product is None:
        raise NotFoundError()
    ProductRepository.update(db, product, payload)
    db.commit()
    db.refresh(product)
    return ProductResponse.model_validate(product)


def deactivate_product_admin(db: Session, product_id: UUID) -> None:
    """Logically delete a product; repeating the operation is harmless."""

    from app.products.repository import ProductRepository

    product = ProductRepository.get_by_id(db, product_id)
    if product is None:
        raise NotFoundError()
    ProductRepository.deactivate(db, product)
    db.commit()


def get_product(
    db: Session,
    requester: UserModel | None,
    product_id: UUID,
) -> ProductResponse:
    """Return a product visible to a public, customer, or admin requester."""

    from app.products.repository import ProductRepository

    product = ProductRepository.get_by_id(db, product_id)
    if product is None:
        raise NotFoundError()
    if requester is None or requester.role is not UserRoleEnum.ADMIN:
        if product.status is not ProductStatusEnum.ACTIVE:
            raise NotFoundError()
    return ProductResponse.model_validate(product)
