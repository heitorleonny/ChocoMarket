"""Pure order pricing and model-construction helpers.

This module deliberately does not inspect stock, change products, commit, or
rollback a session.  Those concerns belong to the atomic creation workflow in
task 5.3.  The helpers here only validate pricing inputs, capture current
product prices, and construct a complete order graph for a caller's
transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.types import OrderStatusEnum, ProductStatusEnum, UserRoleEnum, utc_now
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationDomainError,
)
from app.core.pagination import PaginationService
from app.core.idempotency import (
    IdempotencyRaceError,
    mutation_scope,
    replay_or_conflict,
    request_fingerprint,
    store_record,
    validate_idempotency_key,
)
from app.orders.model import OrderItemModel, OrderModel
from app.orders.schemas import CreateOrderRequest, OrderPage, OrderResponse, PageMeta


@dataclass(frozen=True, slots=True)
class OrderItemPriceSnapshot:
    """Immutable purchase-time values for one order item."""

    product_id: UUID
    quantity: int
    unit_price_cents: int

    @property
    def subtotal_cents(self) -> int:
        """Return the exact integer subtotal for this snapshot."""

        return calculate_subtotal_cents(self.quantity, self.unit_price_cents)


def _require_integer(value: Any, field: str) -> int:
    """Require a real integer, rejecting booleans and numeric coercion."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def validate_order_quantity(quantity: int) -> int:
    """Validate the domain quantity range used by order items."""

    quantity = _require_integer(quantity, "quantity")
    if not 1 <= quantity <= 100:
        raise ValueError("quantity must be between 1 and 100")
    return quantity


def validate_unit_price_cents(unit_price_cents: int) -> int:
    """Validate a product price before it becomes a snapshot."""

    unit_price_cents = _require_integer(unit_price_cents, "unit_price_cents")
    if not 1 <= unit_price_cents <= 1_000_000:
        raise ValueError("unit_price_cents must be between 1 and 1000000")
    return unit_price_cents


def calculate_subtotal_cents(quantity: int, unit_price_cents: int) -> int:
    """Calculate a line subtotal using integer cents only."""

    quantity = validate_order_quantity(quantity)
    unit_price_cents = validate_unit_price_cents(unit_price_cents)
    return quantity * unit_price_cents


def calculate_total_cents(subtotals: Iterable[int]) -> int:
    """Sum validated integer subtotals without decimal conversion."""

    values = list(subtotals)
    if not values:
        raise ValueError("an order must contain at least one item")
    validated: list[int] = []
    for subtotal in values:
        subtotal = _require_integer(subtotal, "subtotal_cents")
        if subtotal < 1:
            raise ValueError("subtotal_cents must be positive")
        validated.append(subtotal)
    return sum(validated)


def _uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("product_id must be a valid UUID") from exc


def _product_values(
    products: Mapping[UUID | str, Any] | Iterable[Any],
) -> dict[UUID, Any]:
    """Normalize either a product mapping or product model iterable."""

    if isinstance(products, Mapping):
        return {_uuid(product_id): product for product_id, product in products.items()}
    normalized: dict[UUID, Any] = {}
    for product in products:
        if not hasattr(product, "id"):
            raise ValueError("each product must expose id and price_cents")
        normalized[_uuid(product.id)] = product
    return normalized


def snapshot_order_items(
    payload: CreateOrderRequest,
    products: Mapping[UUID | str, Any] | Iterable[Any],
) -> list[OrderItemPriceSnapshot]:
    """Capture current product prices for every requested item.

    Product existence is required because a price snapshot cannot be built
    without a product.  Availability and stock checks are intentionally left to
    the atomic workflow that consumes these snapshots.
    """

    product_values = _product_values(products)
    snapshots: list[OrderItemPriceSnapshot] = []
    seen: set[UUID] = set()
    for item in payload.items:
        product_id = _uuid(item.product_id)
        if product_id in seen:
            # The Pydantic request normally catches this.  Keep the domain
            # boundary defensive for callers that construct equivalent objects.
            raise ValueError("each product_id may appear only once in an order")
        seen.add(product_id)
        product = product_values.get(product_id)
        if product is None or not hasattr(product, "price_cents"):
            raise ValueError(f"product {product_id} is required for price snapshot")
        snapshots.append(
            OrderItemPriceSnapshot(
                product_id=product_id,
                quantity=validate_order_quantity(item.quantity),
                unit_price_cents=validate_unit_price_cents(product.price_cents),
            )
        )
    return snapshots


def build_order_model(
    *,
    user_id: UUID | str,
    payload: CreateOrderRequest,
    products: Mapping[UUID | str, Any] | Iterable[Any],
    now: datetime | None = None,
) -> OrderModel:
    """Build an order and its price-snapshot items without database effects."""

    owner_id = _uuid(user_id)
    snapshots = snapshot_order_items(payload, products)
    timestamp = now or utc_now()
    items = [
        OrderItemModel(
            product_id=snapshot.product_id,
            quantity=snapshot.quantity,
            unit_price_cents=snapshot.unit_price_cents,
            subtotal_cents=snapshot.subtotal_cents,
            created_at=timestamp,
            updated_at=timestamp,
        )
        for snapshot in snapshots
    ]
    order = OrderModel(
        user_id=owner_id,
        status=OrderStatusEnum.CONFIRMED,
        total_cents=calculate_total_cents(
            item.subtotal_cents for item in items
        ),
        created_at=timestamp,
        updated_at=timestamp,
    )
    order.items = items
    return order


def order_response_from_model(order: OrderModel) -> OrderResponse:
    """Project a persisted order through the strict public response schema."""

    return OrderResponse.model_validate(order)


# Descriptive aliases keep service call sites readable and provide a stable
# functional API for the task-5.3 transaction service.
calculate_item_subtotal = calculate_subtotal_cents
calculate_order_total = calculate_total_cents
order_from_create = build_order_model
create_order_model = build_order_model


__all__ = [
    "OrderItemPriceSnapshot",
    "build_order_model",
    "calculate_item_subtotal",
    "calculate_order_total",
    "calculate_subtotal_cents",
    "calculate_total_cents",
    "cancel_order",
    "create_order_customer",
    "create_order_model",
    "get_order",
    "list_orders",
    "order_from_create",
    "order_response_from_model",
    "snapshot_order_items",
    "validate_order_quantity",
    "validate_unit_price_cents",
]


ORDER_CREATE_PATH = "/v1/orders"


def create_order_customer(
    db: Session,
    requester: Any,
    payload: CreateOrderRequest,
    *,
    idempotency_key: str | None = None,
) -> OrderResponse:
    """Create one confirmed order and reserve all requested stock atomically.

    Every product is loaded and validated before any stock update. Stock is
    then decremented with conditional SQL updates, so a concurrent request
    cannot reserve a stale quantity. The order graph and idempotency record are
    flushed and committed together; every failure rolls the session back.
    """

    if requester.role is not UserRoleEnum.CUSTOMER:
        raise ForbiddenError()

    key = validate_idempotency_key(idempotency_key)
    if key is None:
        raise ValidationDomainError(
            detail="Idempotency-Key is required for order creation.",
            errors=[
                {
                    "field": "header.Idempotency-Key",
                    "message": "Field required",
                    "code": "missing",
                }
            ],
        )

    fingerprint = request_fingerprint(
        method="POST",
        path=ORDER_CREATE_PATH,
        body=payload.model_dump(mode="json"),
    )
    scope = mutation_scope(
        actor_id=requester.id,
        method="POST",
        path=ORDER_CREATE_PATH,
    )

    try:
        stored = replay_or_conflict(
            db,
            scope=scope,
            key=key,
            request_fingerprint_value=fingerprint,
        )
        if stored is not None:
            return OrderResponse.model_validate(stored.response_body)

        from app.orders.repository import OrderRepository
        from app.products.model import ProductModel
        from app.products.repository import ProductRepository

        requested_ids = [item.product_id for item in payload.items]
        products = list(
            db.scalars(
                select(ProductModel).where(ProductModel.id.in_(requested_ids))
            ).all()
        )
        products_by_id = {product.id: product for product in products}

        # This complete validation pass happens before any UPDATE/INSERT.
        for item in payload.items:
            product = products_by_id.get(item.product_id)
            if (
                product is None
                or product.status is not ProductStatusEnum.ACTIVE
                or product.stock_quantity < item.quantity
            ):
                raise ConflictError(
                    detail="One or more requested products are unavailable or lack sufficient stock.",
                    code="PRODUCT_UNAVAILABLE",
                )

        order = build_order_model(
            user_id=requester.id,
            payload=payload,
            products=products_by_id,
        )

        # Conditional updates protect the decision against a concurrent order
        # even when the earlier validation read used a stale ORM snapshot.
        for item in payload.items:
            reserved = ProductRepository.decrement_stock(
                db,
                item.product_id,
                item.quantity,
            )
            if not reserved:
                raise ConflictError(
                    detail="One or more requested products are unavailable or lack sufficient stock.",
                    code="PRODUCT_UNAVAILABLE",
                )

        OrderRepository.create(db, order)
        response = order_response_from_model(order)
        store_record(
            db,
            scope=scope,
            key=key,
            request_fingerprint_value=fingerprint,
            status_code=201,
            response_body=response.model_dump(mode="json"),
        )
        db.commit()
        return response
    except IdempotencyRaceError:
        # store_record already rolled back the losing resource mutation. Read
        # the committed winner and return its exact public response.
        db.rollback()
        stored = replay_or_conflict(
            db,
            scope=scope,
            key=key,
            request_fingerprint_value=fingerprint,
        )
        if stored is None:
            raise ConflictError(
                detail="The idempotent order result is not available.",
                code="IDEMPOTENCY_RACE",
            )
        return OrderResponse.model_validate(stored.response_body)
    except Exception:
        db.rollback()
        raise


ORDER_LIST_RESOURCE = "orders"


def get_order(
    db: Session,
    requester: Any,
    order_id: UUID,
) -> OrderResponse:
    """Return an order visible to the authenticated requester.

    Customers can only see orders they own. Administrators can see any
    persisted order, including canceled orders. Missing and customer-invisible
    orders intentionally share the same not-found response.
    """

    from app.orders.repository import get_order as get_order_model

    order = get_order_model(db, order_id)
    if order is None:
        raise NotFoundError()
    if requester.role is not UserRoleEnum.ADMIN and order.user_id != requester.id:
        raise NotFoundError()
    return order_response_from_model(order)


def _normalize_order_datetime(value: datetime | None) -> datetime | None:
    """Normalize a query datetime to the UTC representation used by models."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def list_orders(
    db: Session,
    requester: Any,
    *,
    status: OrderStatusEnum | str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    user_id: UUID | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    pagination: PaginationService | None = None,
) -> OrderPage:
    """List orders with ownership-aware visibility and signed cursors.

    Customers are always restricted to their own orders and any supplied
    ``user_id`` filter is intentionally ignored. Administrators see all
    orders unless they provide the optional owner filter.
    """

    from app.orders.repository import OrderRepository

    pagination_service = pagination or PaginationService()
    normalized_status = status.value if isinstance(status, OrderStatusEnum) else status
    normalized_after = _normalize_order_datetime(created_after)
    normalized_before = _normalize_order_datetime(created_before)
    if (
        normalized_after is not None
        and normalized_before is not None
        and normalized_after > normalized_before
    ):
        raise ValidationDomainError(
            detail="created_after must be earlier than or equal to created_before.",
            errors=[
                {
                    "field": "created_after",
                    "message": "created_after must be earlier than or equal to created_before.",
                    "code": "date_range",
                }
            ],
        )

    is_admin = requester.role is UserRoleEnum.ADMIN
    effective_user_id = user_id if is_admin else requester.id
    filters = {
        key: value
        for key, value in {
            "status": normalized_status,
            "created_after": normalized_after,
            "created_before": normalized_before,
            "user_id": effective_user_id,
        }.items()
        if value is not None
    }
    view = {"role": requester.role.value, "user_id": str(requester.id)}
    validated_limit = pagination_service.validate_limit(limit)
    after = None
    if cursor is not None:
        after = pagination_service.decode_cursor(
            cursor,
            resource=ORDER_LIST_RESOURCE,
            filters=filters,
            view=view,
        ).position

    orders = OrderRepository.list_page(
        db,
        user_id=effective_user_id,
        status=normalized_status,
        created_after=normalized_after,
        created_before=normalized_before,
        after=after,
        limit=validated_limit,
    )
    page = pagination_service.build_page(
        orders,
        limit=validated_limit,
        resource=ORDER_LIST_RESOURCE,
        filters=filters,
        view=view,
    )
    return OrderPage(
        data=[order_response_from_model(order) for order in page.data],
        meta=PageMeta(
            limit=page.meta.limit,
            next_cursor=page.meta.next_cursor,
            has_next=page.meta.has_next,
        ),
    )



def cancel_order(
    db: Session,
    requester: Any,
    order_id: UUID,
) -> OrderResponse:
    """Cancel an order and restore each item's stock exactly once.

    Authorization is resolved before state changes: administrators may cancel
    any order, customers may cancel only their own order, and an invisible
    order is indistinguishable from a missing order.  A canceled order is a
    safe replay and therefore does not touch product stock again.
    """

    from app.orders.repository import OrderRepository, get_order as get_order_model
    from app.products.repository import ProductRepository

    order = get_order_model(db, order_id)
    if order is None:
        raise NotFoundError()

    if requester.role is UserRoleEnum.ADMIN:
        pass
    elif order.user_id != requester.id:
        raise NotFoundError()
    elif requester.role is not UserRoleEnum.CUSTOMER:
        raise ForbiddenError()

    if order.status is OrderStatusEnum.CANCELED:
        return order_response_from_model(order)
    if order.status is not OrderStatusEnum.CONFIRMED:
        raise ConflictError(
            detail="The order cannot be canceled in its current state.",
            code="ORDER_NOT_CANCELLABLE",
        )

    # Claim the transition atomically.  A concurrent cancellation that read
    # the same confirmed state will lose this conditional update and replay
    # the winner's canceled response without touching stock.
    if not OrderRepository.cancel_if_confirmed(db, order.id):
        db.rollback()
        current = get_order_model(db, order.id)
        if current is None:
            raise NotFoundError()
        if current.status is OrderStatusEnum.CANCELED:
            return order_response_from_model(current)
        raise ConflictError(
            detail="The order cannot be canceled in its current state.",
            code="ORDER_NOT_CANCELLABLE",
        )

    order.status = OrderStatusEnum.CANCELED
    order.updated_at = utc_now()
    try:
        for item in order.items:
            restored = ProductRepository.restore_stock(
                db,
                item.product_id,
                item.quantity,
            )
            if not restored:
                raise ConflictError(
                    detail="The order stock could not be restored.",
                    code="STOCK_RESTORE_CONFLICT",
                )

        db.commit()
        return order_response_from_model(order)
    except Exception:
        db.rollback()
        raise
