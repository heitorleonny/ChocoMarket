from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.core.errors import ValidationDomainError
from app.core.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursorError,
    OrderingKey,
    PaginationService,
)


@pytest.fixture()
def clock():
    return {"now": 1_700_000_000.0}


@pytest.fixture()
def pagination(clock):
    return PaginationService(
        secret="test-pagination-secret",
        ttl_seconds=60,
        clock=lambda: clock["now"],
    )


def key(second: int = 0, identifier: UUID | None = None) -> OrderingKey:
    return OrderingKey(
        created_at=datetime(2024, 1, 1, 12, 0, second, tzinfo=UTC),
        id=identifier or UUID("00000000-0000-0000-0000-000000000001"),
    )


def test_validate_limit_uses_contract_default_and_bounds() -> None:
    assert PaginationService.validate_limit(None) == DEFAULT_LIMIT
    assert PaginationService.validate_limit(1) == 1
    assert PaginationService.validate_limit(MAX_LIMIT) == MAX_LIMIT

    for invalid in (True, False, 0, MAX_LIMIT + 1, "20", 1.5):
        with pytest.raises(ValidationDomainError):
            PaginationService.validate_limit(invalid)


def test_cursor_binds_resource_filters_view_and_ordering(pagination) -> None:
    user_id = uuid4()
    view = {"role": "admin", "user_id": user_id}
    cursor = pagination.encode_cursor(
        resource="products",
        filters={"status": "active", "min_price_cents": 100},
        view=view,
        position=key(),
        now=1_700_000_000.0,
    )

    decoded = pagination.decode_cursor(
        cursor,
        resource="products",
        filters={"min_price_cents": 100, "status": "active"},
        view={"user_id": user_id, "role": "admin"},
        now=1_700_000_001.0,
    )

    assert decoded.resource == "products"
    assert decoded.filters == {"min_price_cents": 100, "status": "active"}
    assert decoded.view == {"role": "admin", "user_id": str(user_id)}
    assert decoded.position == key()
    assert decoded.expires_at == 1_700_000_060.0

    for kwargs in (
        {"resource": "users"},
        {"resource": "products", "filters": {"status": "inactive"}},
        {"resource": "products", "view": {"role": "customer", "user_id": user_id}},
    ):
        with pytest.raises(InvalidCursorError):
            pagination.decode_cursor(cursor, now=1_700_000_001.0, **kwargs)


def test_cursor_rejects_tampering_and_expiration(pagination, clock) -> None:
    cursor = pagination.encode_cursor(
        resource="orders", filters={}, view="user:1", position=key(), now=clock["now"]
    )
    payload, signature = cursor.split(".")
    tampered = f"{payload[:-1]}{'A' if payload[-1] != 'A' else 'B'}.{signature}"

    with pytest.raises(InvalidCursorError):
        pagination.decode_cursor(tampered, resource="orders", filters={}, view="user:1")

    clock["now"] += 60
    with pytest.raises(InvalidCursorError):
        pagination.decode_cursor(cursor, resource="orders", filters={}, view="user:1")


def test_build_page_uses_limit_plus_one_and_emits_cursor(pagination) -> None:
    items = [
        SimpleNamespace(created_at=key(index).created_at, id=key(index).id)
        for index in range(3)
    ]
    page = pagination.build_page(
        items,
        limit=2,
        resource="products",
        filters={"status": "active"},
        view="public",
        now=1_700_000_000.0,
    )

    assert page.data == items[:2]
    assert page.meta.limit == 2
    assert page.meta.has_next is True
    assert page.meta.next_cursor is not None
    decoded = pagination.decode_cursor(
        page.meta.next_cursor,
        resource="products",
        filters={"status": "active"},
        view="public",
        now=1_700_000_001.0,
    )
    assert decoded.position == key(1)


def test_build_page_reports_terminal_and_empty_pages(pagination) -> None:
    item = SimpleNamespace(created_at=key().created_at, id=key().id)
    for items in ([], [item]):
        page = pagination.build_page(
            items,
            limit=2,
            resource="users",
            filters={},
            view="admin",
        )
        assert page.meta.has_next is False
        assert page.meta.next_cursor is None
        assert page.data == items


def test_cursor_requires_configured_secret(monkeypatch) -> None:
    monkeypatch.delenv("CHOCOMARKET_CURSOR_SECRET", raising=False)
    monkeypatch.delenv("CHOCOMARKET_JWT_SECRET", raising=False)

    with pytest.raises(ValueError, match="signing secret"):
        PaginationService()
