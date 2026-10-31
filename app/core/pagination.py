"""Signed cursor pagination primitives shared by list services.

The pagination service is deliberately independent from FastAPI and SQLAlchemy.
Repositories can use the decoded :class:`CursorData.position` to add a keyset
predicate, then pass ``limit + 1`` ordered rows to :meth:`PaginationService.build_page`.
A cursor binds the resource, filter set, and authorized view so it cannot be
replayed against a different listing.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
import hashlib
import hmac
import json
import math
import os
import time
from typing import Any, Generic, TypeVar
from uuid import UUID

from app.core.errors import ValidationDomainError

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
DEFAULT_CURSOR_TTL_SECONDS = 300
CURSOR_VERSION = 1
ORDERING_FIELDS = ("created_at", "id")
ORDERING_DIRECTIONS = ("asc", "asc")

T = TypeVar("T")


class CursorConfigurationError(ValueError):
    """Raised when no safe signing key is available for cursor tokens."""


class InvalidCursorError(ValidationDomainError):
    """A cursor is malformed, expired, tampered with, or bound elsewhere."""

    def __init__(self, detail: str = "The cursor is invalid or incompatible with this listing.") -> None:
        super().__init__(detail=detail, errors=[{"field": "cursor", "message": detail, "code": "invalid_cursor"}])


@dataclass(frozen=True, slots=True)
class OrderingKey:
    """The stable key used by every cursor-paginated listing."""

    created_at: datetime
    id: UUID

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        object.__setattr__(self, "id", _coerce_uuid(self.id))


@dataclass(frozen=True, slots=True)
class CursorData:
    """Decoded and validated cursor contents."""

    resource: str
    filters: Any
    view: Any
    position: OrderingKey
    issued_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class PageMetadata:
    """Transport-neutral pagination metadata matching the public page schema."""

    limit: int
    next_cursor: str | None
    has_next: bool


@dataclass(frozen=True, slots=True)
class PaginationPage(Generic[T]):
    """A page envelope that routers can map to a resource-specific schema."""

    data: list[T]
    meta: PageMetadata

    @property
    def limit(self) -> int:
        return self.meta.limit

    @property
    def next_cursor(self) -> str | None:
        return self.meta.next_cursor

    @property
    def has_next(self) -> bool:
        return self.meta.has_next


def _invalid(message: str = "The cursor is invalid or incompatible with this listing.") -> InvalidCursorError:
    return InvalidCursorError(message)


def _coerce_uuid(value: UUID | str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("ordering id must be a valid UUID") from exc


def _coerce_now(value: float | int | datetime | None, clock: Callable[[], float]) -> float:
    if value is None:
        return float(clock())
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("now must be a timestamp or timezone-aware datetime")
    if not math.isfinite(float(value)):
        raise ValueError("now must be finite")
    return float(value)


def _canonical_value(value: Any) -> Any:
    """Convert supported cursor inputs into deterministic JSON values."""

    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("cursor datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("cursor mapping keys must be strings")
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("cursor values must be finite")
        return value
    raise ValueError(f"unsupported cursor value type: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_segment(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("empty cursor segment")
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise ValueError("invalid cursor encoding")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid cursor encoding") from exc


def _position_from_value(value: Any) -> OrderingKey:
    if isinstance(value, OrderingKey):
        return value
    if isinstance(value, Mapping):
        try:
            created_at = value["created_at"]
            identifier = value["id"]
        except (KeyError, TypeError) as exc:
            raise ValueError("ordering position must contain created_at and id") from exc
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) and len(value) == 2:
        created_at, identifier = value
    else:
        raise ValueError("ordering position must be an OrderingKey, mapping, or pair")
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise ValueError("ordering created_at must be ISO date-time") from exc
    if not isinstance(created_at, datetime):
        raise ValueError("ordering created_at must be a datetime")
    return OrderingKey(created_at=created_at, id=_coerce_uuid(identifier))


def default_ordering_key(item: Any) -> OrderingKey:
    """Extract ``(created_at, id)`` from an ORM object or mapping."""

    if isinstance(item, Mapping):
        return _position_from_value(item)
    try:
        return OrderingKey(created_at=item.created_at, id=item.id)
    except AttributeError as exc:
        raise ValueError("page items must expose created_at and id") from exc


class PaginationService:
    """Create, validate, and construct signed cursor pages.

    The signing key is required explicitly or must be supplied by
    ``CHOCOMARKET_CURSOR_SECRET``.  ``CHOCOMARKET_JWT_SECRET`` and
    ``JWT_SECRET_KEY`` are accepted as deliberate deployment fallbacks so
    applications can use one configured application secret without embedding a
    secret in source code.
    """

    def __init__(
        self,
        secret: str | bytes | None = None,
        *,
        ttl_seconds: int = DEFAULT_CURSOR_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        configured_secret = secret
        if configured_secret is None:
            configured_secret = (
                os.getenv("CHOCOMARKET_CURSOR_SECRET")
                or os.getenv("CHOCOMARKET_JWT_SECRET")
                or os.getenv("JWT_SECRET_KEY")
            )
        if isinstance(configured_secret, str):
            configured_secret = configured_secret.encode("utf-8")
        if not isinstance(configured_secret, bytes) or not configured_secret:
            raise CursorConfigurationError("cursor signing secret must be configured")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise CursorConfigurationError("cursor TTL must be a positive integer")
        self._secret = configured_secret
        self.ttl_seconds = ttl_seconds
        self._clock = clock

    @staticmethod
    def validate_limit(limit: int | None) -> int:
        """Return a valid page size, defaulting to the contract value of 20."""

        if limit is None:
            return DEFAULT_LIMIT
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ValidationDomainError(
                detail=f"limit must be an integer between 1 and {MAX_LIMIT}.",
                errors=[{"field": "limit", "message": f"limit must be between 1 and {MAX_LIMIT}.", "code": "limit_range"}],
            )
        return limit

    def encode_cursor(
        self,
        *,
        resource: str,
        filters: Mapping[str, Any] | None = None,
        view: Any = None,
        position: OrderingKey | Mapping[str, Any] | Sequence[Any],
        now: float | int | datetime | None = None,
    ) -> str:
        """Encode a cursor bound to one resource, filter set, and authorized view."""

        if not isinstance(resource, str) or not resource.strip():
            raise ValueError("cursor resource must be a non-empty string")
        canonical_filters = _canonical_value(filters or {})
        if not isinstance(canonical_filters, dict):
            raise ValueError("cursor filters must be a mapping")
        canonical_view = _canonical_value(view)
        ordering_position = _position_from_value(position)
        issued_at = _coerce_now(now, self._clock)
        payload = {
            "v": CURSOR_VERSION,
            "resource": resource,
            "filters": canonical_filters,
            "view": canonical_view,
            "ordering": {"fields": list(ORDERING_FIELDS), "directions": list(ORDERING_DIRECTIONS)},
            "position": {
                "created_at": ordering_position.created_at.isoformat(),
                "id": str(ordering_position.id),
            },
            "iat": issued_at,
            "exp": issued_at + self.ttl_seconds,
        }
        encoded_payload = _encode_segment(_canonical_json(payload).encode("utf-8"))
        signature = hmac.new(self._secret, encoded_payload.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded_payload}.{_encode_segment(signature)}"

    def decode_cursor(
        self,
        cursor: str,
        *,
        resource: str,
        filters: Mapping[str, Any] | None = None,
        view: Any = None,
        now: float | int | datetime | None = None,
    ) -> CursorData:
        """Verify and decode a cursor, including all request-context bindings."""

        try:
            if not isinstance(cursor, str) or cursor.count(".") != 1:
                raise ValueError("invalid cursor shape")
            encoded_payload, encoded_signature = cursor.split(".")
            payload_bytes = _decode_segment(encoded_payload)
            signature = _decode_segment(encoded_signature)
            expected_signature = hmac.new(self._secret, encoded_payload.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected_signature):
                raise ValueError("invalid cursor signature")
            payload = json.loads(payload_bytes.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("cursor payload must be an object")
            if payload.get("v") != CURSOR_VERSION:
                raise ValueError("unsupported cursor version")
            if payload.get("resource") != resource:
                raise ValueError("cursor resource mismatch")
            expected_filters = _canonical_value(filters or {})
            if payload.get("filters") != expected_filters:
                raise ValueError("cursor filter mismatch")
            if payload.get("view") != _canonical_value(view):
                raise ValueError("cursor view mismatch")
            ordering = payload.get("ordering")
            if ordering != {"fields": list(ORDERING_FIELDS), "directions": list(ORDERING_DIRECTIONS)}:
                raise ValueError("cursor ordering mismatch")
            issued_at = payload.get("iat")
            expires_at = payload.get("exp")
            if isinstance(issued_at, bool) or not isinstance(issued_at, (int, float)):
                raise ValueError("invalid cursor issue time")
            if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
                raise ValueError("invalid cursor expiration")
            issued_at = float(issued_at)
            expires_at = float(expires_at)
            if not math.isfinite(issued_at) or not math.isfinite(expires_at) or expires_at <= issued_at:
                raise ValueError("invalid cursor lifetime")
            current_time = _coerce_now(now, self._clock)
            if current_time >= expires_at:
                raise ValueError("cursor expired")
            position = _position_from_value(payload.get("position"))
            return CursorData(
                resource=resource,
                filters=expected_filters,
                view=_canonical_value(view),
                position=position,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        except InvalidCursorError:
            raise
        except (binascii.Error, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise _invalid() from exc

    def build_page(
        self,
        items: Sequence[T],
        *,
        limit: int | None,
        resource: str,
        filters: Mapping[str, Any] | None = None,
        view: Any = None,
        ordering_key: Callable[[T], OrderingKey] = default_ordering_key,
        now: float | int | datetime | None = None,
    ) -> PaginationPage[T]:
        """Build page metadata from rows fetched in deterministic order.

        Callers should fetch at most ``limit + 1`` rows.  The extra row is used
        only to decide ``has_next`` and is never returned in ``data``.
        """

        validated_limit = self.validate_limit(limit)
        page_items = list(items)
        has_next = len(page_items) > validated_limit
        data = page_items[:validated_limit]
        next_cursor = None
        if has_next:
            if not data:
                raise ValueError("a page with a next row must contain a data row")
            next_cursor = self.encode_cursor(
                resource=resource,
                filters=filters,
                view=view,
                position=ordering_key(data[-1]),
                now=now,
            )
        return PaginationPage(
            data=data,
            meta=PageMetadata(limit=validated_limit, next_cursor=next_cursor, has_next=has_next),
        )


# Functional aliases keep repository integrations concise while retaining one
# implementation and one source of cursor validation rules.
def validate_limit(limit: int | None) -> int:
    return PaginationService.validate_limit(limit)


def build_page(
    items: Sequence[T],
    *,
    service: PaginationService,
    limit: int | None,
    resource: str,
    filters: Mapping[str, Any] | None = None,
    view: Any = None,
    ordering_key: Callable[[T], OrderingKey] = default_ordering_key,
    now: float | int | datetime | None = None,
) -> PaginationPage[T]:
    return service.build_page(
        items,
        limit=limit,
        resource=resource,
        filters=filters,
        view=view,
        ordering_key=ordering_key,
        now=now,
    )


__all__ = [
    "CURSOR_VERSION",
    "DEFAULT_CURSOR_TTL_SECONDS",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "ORDERING_DIRECTIONS",
    "ORDERING_FIELDS",
    "CursorConfigurationError",
    "CursorData",
    "InvalidCursorError",
    "OrderingKey",
    "PageMetadata",
    "PaginationPage",
    "PaginationService",
    "build_page",
    "default_ordering_key",
    "validate_limit",
]
