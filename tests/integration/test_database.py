from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.common.types import (
    OrderStatusEnum,
    ProductStatusEnum,
    UserRoleEnum,
    UserStatusEnum,
    utc_now,
)
from app.core.idempotency import IdempotencyRecordModel
from app.database import Base, create_database_engine, create_session_factory
from app.orders.model import OrderItemModel, OrderModel
from app.products.model import ProductModel
from app.users.model import UserModel
from tests.conftest import IsolatedDatabase


def _user(email: str = "customer@example.com") -> UserModel:
    return UserModel(
        name="Test Customer",
        email=email,
        password="argon2-hash",
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.ACTIVE,
    )


def _product() -> ProductModel:
    return ProductModel(
        name="Test Chocolate",
        description="",
        price_cents=250,
        stock_quantity=10,
        status=ProductStatusEnum.ACTIVE,
    )


def test_temporary_databases_are_isolated(
    isolated_database: IsolatedDatabase,
    tmp_path,
) -> None:
    first_session = isolated_database.session_factory()
    first_session.add(_user())
    first_session.commit()
    first_session.close()

    second_engine = create_database_engine(f"sqlite:///{tmp_path / 'second.db'}")
    Base.metadata.create_all(bind=second_engine)
    second_session = create_session_factory(second_engine)()
    try:
        assert second_session.scalar(select(UserModel)) is None
    finally:
        second_session.close()
        second_engine.dispose()

    check_session = isolated_database.session_factory()
    try:
        assert check_session.scalar(select(UserModel)) is not None
    finally:
        check_session.close()


def test_sqlite_foreign_keys_are_enabled_and_enforced(
    isolated_database: IsolatedDatabase,
) -> None:
    with isolated_database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1

    db = isolated_database.session_factory()
    try:
        db.add(OrderModel(user_id=uuid4(), total_cents=100))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        user = _user()
        product = _product()
        order = OrderModel(
            user=user,
            status=OrderStatusEnum.CONFIRMED,
            total_cents=250,
        )
        db.add_all([user, product, order])
        db.commit()

        db.add(
            OrderItemModel(
                order_id=uuid4(),
                product_id=product.id,
                quantity=1,
                unit_price_cents=250,
                subtotal_cents=250,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.add(
            OrderItemModel(
                order_id=order.id,
                product_id=uuid4(),
                quantity=1,
                unit_price_cents=250,
                subtotal_cents=250,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_expected_indexes_are_created(isolated_database: IsolatedDatabase) -> None:
    expected_indexes = {
        "users": {
            "ix_users_email",
            "ix_users_status_created_at_id",
            "ix_users_role",
            "ix_users_name",
        },
        "products": {
            "ix_products_status",
            "ix_products_status_created_at_id",
            "ix_products_price_cents",
        },
        "orders": {
            "ix_orders_user_id_created_at_id",
            "ix_orders_status_created_at_id",
            "ix_orders_created_at_id",
        },
        "order_items": {"ix_order_items_order_id", "ix_order_items_product_id"},
        "idempotency_records": {"ix_idempotency_records_expires_at"},
    }

    inspector = inspect(isolated_database.engine)
    for table_name, index_names in expected_indexes.items():
        actual_names = {index["name"] for index in inspector.get_indexes(table_name)}
        assert index_names <= actual_names

    email_index = next(
        index
        for index in inspector.get_indexes("users")
        if index["name"] == "ix_users_email"
    )
    assert email_index["unique"] == 1


def test_users_email_has_a_unique_constraint(isolated_database: IsolatedDatabase) -> None:
    db = isolated_database.session_factory()
    try:
        db.add(_user())
        db.commit()

        db.add(_user())
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        assert db.scalar(select(UserModel)) is not None
    finally:
        db.close()


def test_idempotency_scope_and_key_have_a_unique_constraint(
    isolated_database: IsolatedDatabase,
) -> None:
    expires_at = utc_now() + timedelta(hours=1)
    db = isolated_database.session_factory()
    try:
        db.add(
            IdempotencyRecordModel(
                scope="public:POST:/v1/users",
                key="unique-key-000001",
                request_fingerprint="fingerprint-1",
                status_code=201,
                response_body={"ok": True},
                expires_at=expires_at,
            )
        )
        db.commit()

        db.add(
            IdempotencyRecordModel(
                scope="public:POST:/v1/users",
                key="unique-key-000001",
                request_fingerprint="fingerprint-2",
                status_code=201,
                response_body={"ok": True},
                expires_at=expires_at,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        assert db.scalar(select(IdempotencyRecordModel)) is not None
    finally:
        db.close()


def test_order_items_have_a_unique_order_product_constraint(
    isolated_database: IsolatedDatabase,
) -> None:
    db = isolated_database.session_factory()
    try:
        user = _user()
        product = _product()
        order = OrderModel(user=user, total_cents=250)
        db.add_all([user, product, order])
        db.commit()

        db.add(
            OrderItemModel(
                order=order,
                product=product,
                quantity=1,
                unit_price_cents=250,
                subtotal_cents=250,
            )
        )
        db.commit()

        db.add(
            OrderItemModel(
                order=order,
                product=product,
                quantity=2,
                unit_price_cents=250,
                subtotal_cents=500,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        assert len(order.items) == 1
    finally:
        db.close()
