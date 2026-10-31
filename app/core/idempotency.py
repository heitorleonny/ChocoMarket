"""Persistence and canonicalization helpers for idempotent mutations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from enum import Enum
import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import CheckConstraint, Index, Integer, JSON, String, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.common.models import UUIDPrimaryKeyMixin
from app.common.types import UTCDateTime, utc_now
from app.core.errors import ConflictError, ValidationDomainError
from app.database import Base

IDEMPOTENCY_KEY_MIN_LENGTH = 16
IDEMPOTENCY_KEY_MAX_LENGTH = 128
DEFAULT_IDEMPOTENCY_TTL = timedelta(hours=24)


class IdempotencyRaceError(RuntimeError):
    """Signal that another transaction completed the same key first.

    ``store_record`` rolls the current transaction back before raising this
    exception.  Callers can therefore safely return the already committed
    response without leaving resource mutations from the losing attempt.
    """

    def __init__(self, record: "IdempotencyRecordModel") -> None:
        self.record = record
        super().__init__("another transaction completed this idempotency key")


def validate_idempotency_key(key: str | None) -> str | None:
    """Validate an optional header value and return it unchanged."""

    if key is None:
        return None
    if not isinstance(key, str) or not IDEMPOTENCY_KEY_MIN_LENGTH <= len(key) <= IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ValidationDomainError(
            detail=(
                "Idempotency-Key must contain between 16 and 128 characters."
            ),
            code="VALIDATION_ERROR",
            errors=[
                {
                    "field": "header.Idempotency-Key",
                    "message": "String should have between 16 and 128 characters",
                    "code": "string_too_short_or_long",
                }
            ],
        )
    return key


def _canonical_value(value: Any) -> Any:
    """Convert supported request values into deterministic JSON values."""

    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        canonical_items = [_canonical_value(item) for item in value]
        return sorted(
            canonical_items,
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        )
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported value for idempotency fingerprint: {type(value)!r}")


def canonical_json(*, method: str, path: str, body: Any) -> str:
    """Return the canonical operation representation used for fingerprints."""

    canonical = {
        "body": _canonical_value(body),
        "method": method.upper(),
        "path": path,
    }
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def request_fingerprint(*, method: str, path: str, body: Any) -> str:
    """Hash a deterministic operation representation without storing its body."""

    return hashlib.sha256(
        canonical_json(method=method, path=path, body=body).encode("utf-8")
    ).hexdigest()


def mutation_scope(
    *, actor_id: UUID | str | None, method: str, path: str
) -> str:
    """Build the stable scope used to isolate a mutation's idempotency key."""

    principal = "public" if actor_id is None else f"user:{actor_id}"
    return f"{principal}:{method.upper()}:{path}"


def get_record(
    db: Session, *, scope: str, key: str
) -> "IdempotencyRecordModel | None":
    """Return an unexpired record for a scope/key pair, if present.

    Expired records are removed in the caller's transaction so the key can be
    reused after the retention window without violating the unique constraint.
    """

    record = db.scalar(
        select(IdempotencyRecordModel).where(
            IdempotencyRecordModel.scope == scope,
            IdempotencyRecordModel.key == key,
        )
    )
    if record is None:
        return None
    if record.expires_at <= utc_now():
        db.delete(record)
        db.flush()
        return None
    return record


def replay_or_conflict(
    db: Session,
    *,
    scope: str,
    key: str,
    request_fingerprint_value: str,
) -> "IdempotencyRecordModel | None":
    """Return a replay record, or raise for conflicting key reuse."""

    validate_idempotency_key(key)
    record = get_record(db, scope=scope, key=key)
    if record is None:
        return None
    if record.request_fingerprint != request_fingerprint_value:
        raise ConflictError(
            detail="Idempotency-Key was already used with different content.",
            code="IDEMPOTENCY_KEY_REUSE",
        )
    return record


def store_record(
    db: Session,
    *,
    scope: str,
    key: str,
    request_fingerprint_value: str,
    status_code: int,
    response_body: dict[str, Any],
    ttl: timedelta = DEFAULT_IDEMPOTENCY_TTL,
) -> "IdempotencyRecordModel":
    """Flush a completed response as part of the caller's transaction.

    The resource mutation and this record must be committed by the caller in
    one transaction.  A unique-key collision rolls back the entire losing
    transaction and exposes the winner through ``IdempotencyRaceError``.
    """

    validate_idempotency_key(key)
    record = IdempotencyRecordModel(
        scope=scope,
        key=key,
        request_fingerprint=request_fingerprint_value,
        status_code=status_code,
        response_body=response_body,
        expires_at=datetime.now(UTC) + ttl,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        winner = db.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.scope == scope,
                IdempotencyRecordModel.key == key,
            )
        )
        if winner is not None and winner.expires_at > utc_now():
            raise IdempotencyRaceError(winner) from exc
        raise
    return record


class IdempotencyRecordModel(UUIDPrimaryKeyMixin, Base):
    """Store a completed response for one scoped idempotency key."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint(
            "length(scope) > 0",
            name="ck_idempotency_records_scope_nonempty",
        ),
        CheckConstraint(
            "length(key) BETWEEN 16 AND 128",
            name="ck_idempotency_records_key_length",
        ),
        CheckConstraint(
            "length(request_fingerprint) > 0",
            name="ck_idempotency_records_fingerprint_nonempty",
        ),
        CheckConstraint(
            "status_code BETWEEN 100 AND 599",
            name="ck_idempotency_records_status_code_range",
        ),
        UniqueConstraint("scope", "key", name="uq_idempotency_records_scope_key"),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    scope: Mapped[str] = mapped_column(String(512), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
