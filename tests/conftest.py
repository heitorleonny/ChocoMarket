from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Point the application engine at a throwaway file before any ``app`` import.
# The app lifespan creates tables on that engine, so without this a test that
# enters ``TestClient(app)`` would write to the shared development database.
os.environ.setdefault(
    "CHOCOMARKET_DATABASE_URL",
    f"sqlite:///{Path(tempfile.mkdtemp(prefix='chocomarket-tests-')) / 'app.db'}",
)

import pytest
from hypothesis import HealthCheck, settings
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

# Import every model module before creating metadata so the fixture includes all
# foreign-key targets and relationships, regardless of test import order.
from app.core.idempotency import IdempotencyRecordModel  # noqa: F401
from app.database import Base, create_database_engine, create_session_factory
from app.orders.model import OrderItemModel, OrderModel  # noqa: F401
from app.products.model import ProductModel  # noqa: F401
from app.users.model import UserModel  # noqa: F401
from tests.support import (
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_SECRET,
    ApiHarness,
    api_harness,
)

# Property tests open their own database per example through ``api_harness``,
# so the function-scoped fixture check does not apply to the fixtures they use
# (environment and settings only).
settings.register_profile(
    "chocomarket",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "chocomarket"))

_LAYER_BY_DIRECTORY = {
    "property": "property",
    "integration": "integration",
    "contract": "contract",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark each test with its layer so ``pytest -m <layer>`` selects it."""

    tests_root = Path(__file__).parent
    for item in items:
        relative = Path(item.fspath).relative_to(tests_root)
        layer = _LAYER_BY_DIRECTORY.get(relative.parts[0], "unit")
        item.add_marker(getattr(pytest.mark, layer))


@pytest.fixture(autouse=True)
def jwt_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the fixture token issuer; individual tests may override it."""

    monkeypatch.setenv("CHOCOMARKET_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("CHOCOMARKET_JWT_ISSUER", JWT_ISSUER)
    monkeypatch.setenv("CHOCOMARKET_JWT_AUDIENCE", JWT_AUDIENCE)


@pytest.fixture
def api() -> Iterator[ApiHarness]:
    """Real application client bound to a private in-memory database."""

    with api_harness() as harness:
        yield harness


@dataclass(frozen=True)
class IsolatedDatabase:
    engine: Engine
    session_factory: sessionmaker[Session]


@pytest.fixture
def isolated_database(tmp_path: Path) -> Iterator[IsolatedDatabase]:
    """Provide a fresh file-backed SQLite database for one test."""

    engine = create_database_engine(f"sqlite:///{tmp_path / 'chocomarket.db'}")
    Base.metadata.create_all(bind=engine)
    database = IsolatedDatabase(
        engine=engine,
        session_factory=create_session_factory(engine),
    )
    try:
        yield database
    finally:
        engine.dispose()
