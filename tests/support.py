"""Shared helpers for HTTP, property and contract tests.

Every harness owns a private SQLite database and overrides ``get_db`` on the
real application, so tests exercise the production routers, dependencies,
error handlers and middleware without touching ``chocomarket.db``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.common.types import (
    OrderStatusEnum,
    ProductStatusEnum,
    UserRoleEnum,
    UserStatusEnum,
)
from app.core.idempotency import IdempotencyRecordModel
from app.core.security import hash_user_password
from app.database import (
    Base,
    create_database_engine,
    create_session_factory,
    get_db,
    make_session_dependency,
)
from app.main import app
from app.orders.model import OrderItemModel, OrderModel
from app.products.model import ProductModel
from app.users.model import UserModel

JWT_SECRET = "chocomarket-test-secret"
JWT_ISSUER = "https://issuer.chocomarket.test"
JWT_AUDIENCE = "chocomarket-api"
DEFAULT_PASSWORD = "SenhaDeTeste123"

# Fields that must never cross the HTTP boundary in any response body.
CREDENTIAL_FIELDS = frozenset(
    {"password", "password_hash", "hashed_password", "token", "access_token", "secret"}
)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def make_token(
    user_id: UUID | str,
    *,
    secret: str = JWT_SECRET,
    issuer: str = JWT_ISSUER,
    audience: str = JWT_AUDIENCE,
    expires_in: float = 300,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Sign an HS256 fixture token accepted by the configured test issuer."""

    header = {"alg": "HS256", "typ": "JWT"}
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "iss": issuer,
        "aud": audience,
        "exp": time.time() + expires_in,
    }
    claims.update(extra_claims or {})
    encoded_header = _base64url(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _base64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_claims}.{_base64url(signature)}"


def bearer(user: UserModel | UUID | str) -> dict[str, str]:
    """Return an ``Authorization`` header for a user or user ID."""

    user_id = user.id if isinstance(user, UserModel) else user
    return {"Authorization": f"Bearer {make_token(user_id)}"}


@cache
def default_password_hash() -> str:
    """Hash the default password once; Argon2 is deliberately slow."""

    return hash_user_password(DEFAULT_PASSWORD)


def assert_no_credentials(value: Any) -> None:
    """Recursively assert that no credential-like key appears in a JSON body."""

    if isinstance(value, dict):
        leaked = CREDENTIAL_FIELDS.intersection(value)
        assert not leaked, f"credential fields exposed: {sorted(leaked)}"
        for item in value.values():
            assert_no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_credentials(item)


def assert_problem(response: Any, status_code: int) -> dict[str, Any]:
    """Assert the common Problem_Response envelope and return its body."""

    assert response.status_code == status_code, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    for field in ("type", "title", "status", "detail", "code", "request_id"):
        assert field in body, f"missing problem field {field!r}"
    assert body["status"] == status_code
    assert body["request_id"]
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body


@dataclass
class ApiHarness:
    """A TestClient bound to the real app and one isolated database."""

    client: TestClient
    engine: Engine
    session_factory: sessionmaker[Session]

    @contextmanager
    def session(self) -> Iterator[Session]:
        db = self.session_factory()
        try:
            yield db
        finally:
            db.close()

    def create_user(
        self,
        *,
        name: str = "Usuário de Teste",
        email: str | None = None,
        role: UserRoleEnum = UserRoleEnum.CUSTOMER,
        status: UserStatusEnum = UserStatusEnum.ACTIVE,
        created_at: datetime | None = None,
    ) -> UserModel:
        timestamp = created_at or datetime.now(UTC)
        user = UserModel(
            id=uuid4(),
            name=name,
            email=email or f"{uuid4().hex}@example.com",
            password=default_password_hash(),
            role=role,
            status=status,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self.session() as db:
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    def create_admin(self, **kwargs: Any) -> UserModel:
        return self.create_user(role=UserRoleEnum.ADMIN, **kwargs)

    def create_product(
        self,
        *,
        name: str = "Chocolate amargo 70%",
        description: str = "",
        price_cents: int = 1_590,
        stock_quantity: int = 10,
        status: ProductStatusEnum | None = None,
        created_at: datetime | None = None,
    ) -> ProductModel:
        timestamp = created_at or datetime.now(UTC)
        product = ProductModel(
            id=uuid4(),
            name=name,
            description=description,
            price_cents=price_cents,
            stock_quantity=stock_quantity,
            status=status
            or (
                ProductStatusEnum.OUT_OF_STOCK
                if stock_quantity == 0
                else ProductStatusEnum.ACTIVE
            ),
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self.session() as db:
            db.add(product)
            db.commit()
            db.refresh(product)
        return product

    def create_order(
        self,
        user: UserModel,
        product: ProductModel,
        *,
        quantity: int = 1,
        status: OrderStatusEnum = OrderStatusEnum.CONFIRMED,
        created_at: datetime | None = None,
    ) -> OrderModel:
        """Persist an order directly, bypassing stock checks, for query tests."""

        timestamp = created_at or datetime.now(UTC)
        subtotal = quantity * product.price_cents
        order = OrderModel(
            id=uuid4(),
            user_id=user.id,
            status=status,
            total_cents=subtotal,
            created_at=timestamp,
            updated_at=timestamp,
        )
        order.items = [
            OrderItemModel(
                id=uuid4(),
                product_id=product.id,
                quantity=quantity,
                unit_price_cents=product.price_cents,
                subtotal_cents=subtotal,
                created_at=timestamp,
                updated_at=timestamp,
            )
        ]
        with self.session() as db:
            db.add(order)
            db.commit()
            db.refresh(order)
        return order

    def stock(self, product_id: UUID) -> int:
        return self.get(ProductModel, product_id).stock_quantity

    def get(self, model: type[Base], entity_id: UUID) -> Any:
        with self.session() as db:
            return db.get(model, entity_id)

    def count(self, model: type[Base]) -> int:
        with self.session() as db:
            return db.scalar(select(func.count()).select_from(model)) or 0

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        """Capture every persisted row so tests can assert ``no mutation``."""

        tables = {
            "users": UserModel,
            "products": ProductModel,
            "orders": OrderModel,
            "order_items": OrderItemModel,
            "idempotency_records": IdempotencyRecordModel,
        }
        result: dict[str, list[tuple[Any, ...]]] = {}
        with self.session() as db:
            for name, model in tables.items():
                columns = list(model.__table__.columns)
                rows = db.execute(select(*columns).order_by(*model.__table__.primary_key)).all()
                result[name] = [tuple(row) for row in rows]
        return result


@contextmanager
def api_harness(database_url: str = "sqlite:///:memory:") -> Iterator[ApiHarness]:
    """Open a harness on a fresh database, in memory by default.

    Usable both as a pytest fixture body and inside a Hypothesis example,
    where function-scoped fixtures would otherwise leak state across inputs.
    Concurrency tests pass a file URL so each thread gets its own connection.
    """

    engine = create_database_engine(database_url)
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = make_session_dependency(session_factory)
    try:
        yield ApiHarness(
            client=TestClient(app),
            engine=engine,
            session_factory=session_factory,
        )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        engine.dispose()


def utc(year: int = 2026, month: int = 1, day: int = 1, **offset: float) -> datetime:
    """Build an aware UTC datetime, optionally shifted by ``timedelta`` kwargs."""

    return datetime(year, month, day, tzinfo=UTC) + timedelta(**offset)
