from __future__ import annotations

import os
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

from sqlalchemy import ColumnElement, Engine, create_engine, event, func
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = f"sqlite:///{BASE_DIR / 'chocomarket.db'}"
# CHOCOMARKET_DATABASE_URL lets tests and other environments use an isolated
# database without changing the development database or application imports.
DATABASE_URL = os.getenv("CHOCOMARKET_DATABASE_URL", DEFAULT_DATABASE_URL)


def _sqlite_connect_args(database_url: str) -> dict[str, Any]:
    """Return SQLite connection settings shared by application and test engines."""
    connect_args: dict[str, Any] = {
        "check_same_thread": False,
        # Start write transactions immediately so stock-changing transactions
        # do not silently compete after validation has already taken place.
        "isolation_level": "IMMEDIATE",
    }
    if database_url.endswith(":memory:"):
        connect_args["uri"] = False
    return connect_args


def create_database_engine(database_url: str = DATABASE_URL) -> Engine:
    """Create an engine with SQLite safety settings for the given database URL."""
    engine_kwargs: dict[str, Any] = {
        "future": True,
        "pool_pre_ping": True,
    }

    if database_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = _sqlite_connect_args(database_url)
        if database_url.endswith(":memory:"):
            # A temporary in-memory database must be shared by connections in
            # one test engine, while each engine remains isolated from others.
            engine_kwargs["poolclass"] = StaticPool

    database_engine = create_engine(database_url, **engine_kwargs)
    if database_engine.dialect.name == "sqlite":
        _register_sqlite_integrity_listener(database_engine)
    return database_engine


UNICODE_CASEFOLD_FUNCTION = "unicode_casefold"


def _casefold(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else value


def casefolded(expression: Any) -> ColumnElement[str]:
    """Return a SQL expression folding case for any Unicode text.

    SQLite's built-in ``lower()`` and ``LIKE`` only fold ASCII letters, so a
    case-insensitive search for "á" would not match "Á".
    """
    return getattr(func, UNICODE_CASEFOLD_FUNCTION)(expression)


def _register_sqlite_integrity_listener(database_engine: Engine) -> None:
    """Enable SQLite constraints and avoid indefinite writer contention."""
    @event.listens_for(database_engine, "connect")
    def _enable_sqlite_integrity(
        dbapi_connection: Any, _connection_record: Any
    ) -> None:
        dbapi_connection.create_function(
            UNICODE_CASEFOLD_FUNCTION, 1, _casefold, deterministic=True
        )
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


engine = create_database_engine()


def create_session_factory(database_engine: Engine) -> sessionmaker[Session]:
    """Build a session factory bound to an application or isolated test engine."""
    return sessionmaker(
        bind=database_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


SessionLocal = create_session_factory(engine)


class Base(DeclarativeBase):
    pass


def _yield_request_session(
    request: Request, session_factory: sessionmaker[Session]
) -> Generator[Session, None, None]:
    """Yield one session per request and always close it afterward."""
    db = session_factory()
    request.state.db = db
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        if getattr(request.state, "db", None) is db:
            del request.state.db


def get_db(request: Request) -> Generator[Session, None, None]:
    """Yield one application session per request."""
    yield from _yield_request_session(request, SessionLocal)


def make_session_dependency(
    session_factory: sessionmaker[Session],
) -> Callable[[Request], Generator[Session, None, None]]:
    """Build a ``get_db`` replacement bound to another engine, e.g. in tests.

    The replacement keeps the same per-request lifecycle and ``request.state``
    binding, so error handlers can still roll back the request session.
    """

    def dependency(request: Request) -> Generator[Session, None, None]:
        yield from _yield_request_session(request, session_factory)

    return dependency
