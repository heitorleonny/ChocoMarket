"""Persistence operations for products.

The repository deliberately does not commit transactions.  Services can group
product changes with order and idempotency writes, while tests and callers can
commit or roll back the session as one unit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.common.types import ProductStatusEnum, utc_now
from app.database import casefolded
from app.products.model import ProductModel
from app.products.schemas import CreateProductRequest, UpdateProductRequest
from app.products.service import apply_product_patch, product_from_create


class ProductRepository:
    """Query and mutate persisted products without owning the transaction."""

    @staticmethod
    def get_by_id(db: Session, product_id: UUID | str) -> ProductModel | None:
        """Return one product by UUID, or ``None`` when it does not exist."""

        try:
            normalized_id = product_id if isinstance(product_id, UUID) else UUID(str(product_id))
        except (AttributeError, TypeError, ValueError):
            return None
        return db.scalar(select(ProductModel).where(ProductModel.id == normalized_id))

    get = get_by_id

    @staticmethod
    def list(
        db: Session,
        *,
        name: str | None = None,
        min_price_cents: int | None = None,
        max_price_cents: int | None = None,
        status: Any | None = None,
    ) -> list[ProductModel]:
        """Return products matching all supplied filters in stable order."""

        statement = select(ProductModel)
        if name is not None:
            statement = statement.where(
                casefolded(ProductModel.name).contains(
                    name.strip().casefold(), autoescape=True
                )
            )
        if min_price_cents is not None:
            statement = statement.where(ProductModel.price_cents >= min_price_cents)
        if max_price_cents is not None:
            statement = statement.where(ProductModel.price_cents <= max_price_cents)
        if status is not None:
            statement = statement.where(ProductModel.status == status)
        statement = statement.order_by(ProductModel.created_at, ProductModel.id)
        return list(db.scalars(statement).all())

    @staticmethod
    def list_page(
        db: Session,
        *,
        name: str | None = None,
        min_price_cents: int | None = None,
        max_price_cents: int | None = None,
        status: Any | None = None,
        after: Any | None = None,
        limit: int = 20,
    ) -> list[ProductModel]:
        """Return one stable keyset-paginated product page.

        Visibility and all supplied filters are applied before the cursor
        predicate. The extra row requested by ``limit + 1`` lets the caller
        determine whether another page exists without a count query.
        """

        statement = select(ProductModel)
        if name is not None:
            statement = statement.where(
                casefolded(ProductModel.name).contains(
                    name.strip().casefold(), autoescape=True
                )
            )
        if min_price_cents is not None:
            statement = statement.where(ProductModel.price_cents >= min_price_cents)
        if max_price_cents is not None:
            statement = statement.where(ProductModel.price_cents <= max_price_cents)
        if status is not None:
            statement = statement.where(ProductModel.status == status)
        if after is not None:
            statement = statement.where(
                or_(
                    ProductModel.created_at > after.created_at,
                    and_(
                        ProductModel.created_at == after.created_at,
                        ProductModel.id > after.id,
                    ),
                )
            )
        statement = (
            statement.order_by(ProductModel.created_at, ProductModel.id)
            .limit(limit + 1)
        )
        return list(db.scalars(statement).all())

    @staticmethod
    def create(
        db: Session,
        payload: CreateProductRequest | None = None,
        *,
        name: str | None = None,
        description: str = "",
        price_cents: int | None = None,
        stock_quantity: int | None = None,
    ) -> ProductModel:
        """Add a product with status derived from its initial stock quantity."""

        if payload is not None:
            if any(value is not None for value in (name, price_cents, stock_quantity)):
                raise ValueError("use either payload or keyword product values, not both")
            product = product_from_create(payload)
        else:
            if name is None or price_cents is None or stock_quantity is None:
                raise ValueError(
                    "name, price_cents, and stock_quantity are required to create a product"
                )
            from app.products.schemas import CreateProductRequest

            product = product_from_create(
                CreateProductRequest(
                    name=name,
                    description=description,
                    price_cents=price_cents,
                    stock_quantity=stock_quantity,
                )
            )
        db.add(product)
        db.flush()
        return product

    @staticmethod
    def update(
        db: Session,
        product: ProductModel,
        patch: UpdateProductRequest | Mapping[str, Any],
    ) -> ProductModel:
        """Apply a partial update and leave commit/rollback to the caller."""

        updated = apply_product_patch(product, patch)
        db.flush()
        return updated

    @staticmethod
    def deactivate(db: Session, product: ProductModel) -> ProductModel:
        """Apply idempotent logical deletion to a product."""

        if product.status != ProductStatusEnum.INACTIVE:
            product.status = ProductStatusEnum.INACTIVE
            product.updated_at = utc_now()
        db.flush()
        return product

    @staticmethod
    def decrement_stock(
        db: Session,
        product_id: UUID,
        quantity: int,
    ) -> bool:
        """Atomically reserve stock when the active row has enough quantity.

        The conditional update is required even after a validation read: two
        concurrent order requests can otherwise validate the same stale stock
        value and both create orders. The caller owns the surrounding
        transaction and rolls it back if any item cannot be reserved.
        Reserving the last units moves the product to ``out_of_stock``, the
        counterpart of ``restore_stock`` reactivating it.
        """

        from sqlalchemy import case, update

        result = db.execute(
            update(ProductModel)
            .where(
                ProductModel.id == product_id,
                ProductModel.status == ProductStatusEnum.ACTIVE,
                ProductModel.stock_quantity >= quantity,
            )
            .values(
                stock_quantity=ProductModel.stock_quantity - quantity,
                # SET expressions read the pre-update row, so this compares
                # the current stock with the reserved quantity.
                status=case(
                    (
                        ProductModel.stock_quantity == quantity,
                        ProductStatusEnum.OUT_OF_STOCK,
                    ),
                    else_=ProductModel.status,
                ),
                updated_at=utc_now(),
            )
        )
        return result.rowcount == 1

    @staticmethod
    def restore_stock(
        db: Session,
        product_id: UUID,
        quantity: int,
    ) -> bool:
        """Atomically return one order item's stock to its product.

        The upper-bound predicate keeps the model constraint valid if a caller
        supplies an inconsistent historical order.  Restoring stock changes an
        ``out_of_stock`` product back to ``active`` but preserves an explicit
        logical ``inactive`` state.
        """

        from sqlalchemy import case, update

        result = db.execute(
            update(ProductModel)
            .where(
                ProductModel.id == product_id,
                ProductModel.stock_quantity + quantity <= 1_000_000,
            )
            .values(
                stock_quantity=ProductModel.stock_quantity + quantity,
                status=case(
                    (
                        ProductModel.status == ProductStatusEnum.OUT_OF_STOCK,
                        ProductStatusEnum.ACTIVE,
                    ),
                    else_=ProductModel.status,
                ),
                updated_at=utc_now(),
            )
        )
        return result.rowcount == 1

    soft_delete = deactivate


# Module-level functions provide a small functional API for services that do
# not need to instantiate a repository object.
def get_product(db: Session, product_id: UUID | str) -> ProductModel | None:
    return ProductRepository.get_by_id(db, product_id)


def list_products(db: Session, **filters: Any) -> list[ProductModel]:
    return ProductRepository.list(db, **filters)


def list_products_page(db: Session, **filters: Any) -> list[ProductModel]:
    return ProductRepository.list_page(db, **filters)


def create_product(
    db: Session,
    payload: CreateProductRequest | None = None,
    **values: Any,
) -> ProductModel:
    return ProductRepository.create(db, payload, **values)


def update_product(
    db: Session,
    product: ProductModel,
    patch: UpdateProductRequest | Mapping[str, Any],
) -> ProductModel:
    return ProductRepository.update(db, product, patch)


def deactivate_product(db: Session, product: ProductModel) -> ProductModel:
    return ProductRepository.deactivate(db, product)


def decrement_product_stock(db: Session, product_id: UUID, quantity: int) -> bool:
    return ProductRepository.decrement_stock(db, product_id, quantity)


def restore_product_stock(db: Session, product_id: UUID, quantity: int) -> bool:
    return ProductRepository.restore_stock(db, product_id, quantity)
