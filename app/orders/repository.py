"""Transaction-neutral persistence operations for orders."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.pagination import OrderingKey
from app.common.types import OrderStatusEnum, utc_now
from app.orders.model import OrderModel


class OrderRepository:
    """Persist and retrieve orders without owning commit or rollback."""

    @staticmethod
    def get_by_id(db: Session, order_id: UUID | str) -> OrderModel | None:
        """Return one order by UUID, or ``None`` for an invalid/missing ID."""

        try:
            normalized_id = (
                order_id if isinstance(order_id, UUID) else UUID(str(order_id))
            )
        except (AttributeError, TypeError, ValueError):
            return None
        return db.scalar(select(OrderModel).where(OrderModel.id == normalized_id))

    get = get_by_id

    @staticmethod
    def list_page(
        db: Session,
        *,
        user_id: UUID | str | None = None,
        status: Any | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        after: OrderingKey | None = None,
        limit: int = 20,
    ) -> list[OrderModel]:
        """Return one stable keyset-paginated page of visible orders.

        All visibility and filter predicates are applied before the cursor
        predicate. The extra row fetched beyond ``limit`` lets the service
        construct pagination metadata without a count query.
        """

        statement = select(OrderModel)
        if user_id is not None:
            statement = statement.where(OrderModel.user_id == user_id)
        if status is not None:
            statement = statement.where(OrderModel.status == status)
        if created_after is not None:
            statement = statement.where(OrderModel.created_at >= created_after)
        if created_before is not None:
            statement = statement.where(OrderModel.created_at <= created_before)
        if after is not None:
            statement = statement.where(
                or_(
                    OrderModel.created_at > after.created_at,
                    and_(
                        OrderModel.created_at == after.created_at,
                        OrderModel.id > after.id,
                    ),
                )
            )
        statement = (
            statement.order_by(OrderModel.created_at, OrderModel.id)
            .limit(limit + 1)
        )
        return list(db.scalars(statement).all())

    @staticmethod
    def cancel_if_confirmed(db: Session, order_id: UUID) -> bool:
        """Claim a confirmed order for cancellation within the caller's transaction."""

        from sqlalchemy import update

        result = db.execute(
            update(OrderModel)
            .where(
                OrderModel.id == order_id,
                OrderModel.status == OrderStatusEnum.CONFIRMED,
            )
            .values(
                status=OrderStatusEnum.CANCELED,
                updated_at=utc_now(),
            )
        )
        return result.rowcount == 1

    @staticmethod
    def create(db: Session, order: OrderModel) -> OrderModel:
        """Add and flush an order graph without committing the transaction."""

        db.add(order)
        db.flush()
        return order

    add = create


def get_order(db: Session, order_id: UUID | str) -> OrderModel | None:
    """Functional wrapper for repository lookup."""

    return OrderRepository.get_by_id(db, order_id)


def list_orders_page(db: Session, **filters: Any) -> list[OrderModel]:
    """Functional wrapper for one stable order-list page."""

    return OrderRepository.list_page(db, **filters)


def create_order(db: Session, order: OrderModel) -> OrderModel:
    """Functional wrapper for transaction-neutral order persistence."""

    return OrderRepository.create(db, order)


__all__ = ["OrderRepository", "create_order", "get_order", "list_orders_page"]
