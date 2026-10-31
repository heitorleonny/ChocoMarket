from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.types import ProductStatusEnum, utc_now
from app.core.errors import ConflictError, ValidationDomainError
from app.core.idempotency import (
    IdempotencyRaceError,
    IdempotencyRecordModel,
    canonical_json,
    mutation_scope,
    replay_or_conflict,
    request_fingerprint,
    store_record,
    validate_idempotency_key,
)


from app.database import Base, create_database_engine, create_session_factory


@pytest.fixture
def db() -> Session:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _record_kwargs(
    key: str,
    body: dict[str, object],
    *,
    scope: str = "user:owner:POST:/v1/orders",
) -> dict[str, object]:
    return {
        "scope": scope,
        "key": key,
        "request_fingerprint_value": request_fingerprint(
            method="POST", path="/v1/orders", body=body
        ),
        "status_code": 201,
        "response_body": {"id": str(uuid4()), "status": "confirmed"},
    }


@pytest.mark.parametrize("key", ["", "short", "k" * 15, "k" * 129])
def test_idempotency_key_validation_rejects_values_outside_contract(key: str) -> None:
    with pytest.raises(ValidationDomainError):
        validate_idempotency_key(key)


@pytest.mark.parametrize("length", [16, 128])
def test_idempotency_key_validation_accepts_contract_boundaries(length: int) -> None:
    key = "k" * length
    assert validate_idempotency_key(key) == key


def test_fingerprint_is_canonical_and_does_not_store_request_body() -> None:
    first = {
        "name": "Ana",
        "product_id": uuid4(),
        "status": ProductStatusEnum.ACTIVE,
        "items": [{"quantity": 2, "price": 150}],
    }
    second = {
        "items": [{"quantity": 2, "price": 150}],
        "status": "active",
        "product_id": first["product_id"],
        "name": "Ana",
    }

    assert request_fingerprint(method="post", path="/v1/orders", body=first) == request_fingerprint(
        method="POST", path="/v1/orders", body=second
    )
    assert "Ana" not in request_fingerprint(
        method="POST", path="/v1/orders", body=first
    )
    assert canonical_json(method="POST", path="/v1/orders", body=first).startswith(
        '{"body":'
    )


def test_mutation_scope_isolated_by_actor_method_and_path() -> None:
    assert mutation_scope(actor_id=None, method="post", path="/v1/users") == (
        "public:POST:/v1/users"
    )
    assert mutation_scope(actor_id="abc", method="post", path="/v1/orders") == (
        "user:abc:POST:/v1/orders"
    )
    assert mutation_scope(actor_id="abc", method="patch", path="/v1/orders") != mutation_scope(
        actor_id="abc", method="post", path="/v1/orders"
    )


def test_replay_and_conflict_are_scoped_and_use_the_original_response(db: Session) -> None:
    key = "shared-key-000001"
    body = {"name": "Ana"}
    record = store_record(db, **_record_kwargs(key, body))
    other_scope_record = store_record(
        db,
        **_record_kwargs(
            key,
            body,
            scope="user:owner:POST:/v1/products",
        ),
    )
    db.commit()

    fingerprint = request_fingerprint(method="POST", path="/v1/orders", body=body)
    replay = replay_or_conflict(
        db,
        scope=record.scope,
        key=key,
        request_fingerprint_value=fingerprint,
    )
    assert replay is not None
    assert replay.response_body == record.response_body
    assert other_scope_record.scope != replay.scope

    with pytest.raises(ConflictError) as error:
        replay_or_conflict(
            db,
            scope=record.scope,
            key=key,
            request_fingerprint_value="different-fingerprint",
        )
    assert error.value.code == "IDEMPOTENCY_KEY_REUSE"


def test_record_and_resource_effect_are_in_one_caller_transaction(db: Session) -> None:
    key = "transaction-key-0001"
    store_record(db, **_record_kwargs(key, {"value": 1}))
    assert db.scalar(select(func.count()).select_from(IdempotencyRecordModel)) == 1

    db.rollback()
    assert db.scalar(select(func.count()).select_from(IdempotencyRecordModel)) == 0


def test_expired_record_can_be_reused_after_retention_window(db: Session) -> None:
    key = "expired-key-000001"
    store_record(
        db,
        **_record_kwargs(key, {"value": 1}),
        ttl=timedelta(seconds=-1),
    )
    db.commit()

    assert replay_or_conflict(
        db,
        scope="user:owner:POST:/v1/orders",
        key=key,
        request_fingerprint_value=request_fingerprint(
            method="POST", path="/v1/orders", body={"value": 1}
        ),
    ) is None
    replacement = store_record(db, **_record_kwargs(key, {"value": 2}))
    db.commit()
    assert replacement.response_body["status"] == "confirmed"
    assert db.scalar(select(func.count()).select_from(IdempotencyRecordModel)) == 1


def test_race_collision_rolls_back_losing_transaction(db: Session) -> None:
    key = "race-key-0000001"
    store_record(db, **_record_kwargs(key, {"value": 1}))
    db.commit()

    # A pending record represents a resource mutation made by the losing
    # request. The collision must roll back both that mutation and its record.
    pending = IdempotencyRecordModel(
        scope="user:owner:POST:/v1/other",
        key="pending-key-0001",
        request_fingerprint="pending",
        status_code=201,
        response_body={"ok": True},
        expires_at=utc_now() + timedelta(hours=24),
    )
    db.add(pending)
    with pytest.raises(IdempotencyRaceError) as error:
        store_record(db, **_record_kwargs(key, {"value": 1}))

    assert error.value.record.key == key
    assert db.scalar(select(func.count()).select_from(IdempotencyRecordModel)) == 1
    assert db.scalar(
        select(IdempotencyRecordModel).where(
            IdempotencyRecordModel.key == "pending-key-0001"
        )
    ) is None
