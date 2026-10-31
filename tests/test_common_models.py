from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.common.types import (
    UTCDateTime,
    UUIDType,
    UserRoleEnum,
    UserStatusEnum,
    utc_now,
)
from app.database import Base, create_database_engine, create_session_factory
from app.users.model import UserModel


def test_uuid_type_round_trips_canonical_uuid_values() -> None:
    value = uuid4()
    uuid_type = UUIDType()

    stored = uuid_type.process_bind_param(value, None)
    loaded = uuid_type.process_result_value(stored, None)

    assert stored == str(value)
    assert loaded == value


def test_uuid_type_rejects_malformed_values() -> None:
    with pytest.raises(ValueError, match="valid UUID"):
        UUIDType().process_bind_param("not-a-uuid", None)


def test_utc_datetime_requires_aware_input_and_normalizes_to_utc() -> None:
    value = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    datetime_type = UTCDateTime()

    stored = datetime_type.process_bind_param(value, None)
    loaded = datetime_type.process_result_value(stored, None)

    assert stored == datetime(2025, 1, 1, 12, 0)
    assert loaded == value
    assert loaded.tzinfo is UTC

    with pytest.raises(ValueError, match="timezone-aware"):
        datetime_type.process_bind_param(datetime(2025, 1, 1, 12, 0), None)


def test_user_model_defaults_use_uuid_and_utc_timestamps() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)

    try:
        with session_factory() as db:
            user = UserModel(
                name="Ana Souza",
                email="ana@example.com",
                password="argon2-hash",
                role=UserRoleEnum.CUSTOMER,
                status=UserStatusEnum.ACTIVE,
            )
            db.add(user)
            db.commit()
            db.refresh(user)

            assert isinstance(user.id, UUID)
            assert user.created_at.tzinfo is UTC
            assert user.updated_at.tzinfo is UTC
    finally:
        engine.dispose()


def test_utc_now_returns_timezone_aware_utc() -> None:
    value = utc_now()

    assert value.tzinfo is UTC
