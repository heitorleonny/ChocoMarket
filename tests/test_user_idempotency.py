from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.idempotency import IdempotencyRecordModel
from app.database import Base, create_database_engine, get_db
from app.main import app
from app.users.model import UserModel


@pytest.fixture
def registration_database() -> Generator[sessionmaker[Session], None, None]:
    database_engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=database_engine)
    session_factory = sessionmaker(
        bind=database_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    try:
        yield session_factory
    finally:
        database_engine.dispose()


@pytest.fixture
def registration_client(
    registration_database: sessionmaker[Session],
) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        with registration_database() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def registration_payload(name: str = "Ana Souza") -> dict[str, str]:
    return {
        "name": name,
        "email": "ana@example.com",
        "password": "UmaSenhaSeguraCom12Caracteres",
    }


def test_idempotent_registration_replays_persisted_result(
    registration_client: TestClient,
    registration_database: sessionmaker[Session],
) -> None:
    payload = registration_payload()
    key = "registration-key-001"

    first = registration_client.post(
        "/v1/users", json=payload, headers={"Idempotency-Key": key}
    )
    retry = registration_client.post(
        "/v1/users", json=payload, headers={"Idempotency-Key": key}
    )

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json() == first.json()
    assert retry.headers["location"] == first.headers["location"]

    with registration_database() as db:
        assert db.scalar(select(func.count()).select_from(UserModel)) == 1
        record = db.scalar(select(IdempotencyRecordModel))
        assert record is not None
        assert record.status_code == 201
        assert "password" not in record.response_body
        assert "UmaSenhaSegura" not in record.request_fingerprint


def test_idempotency_key_reuse_with_different_content_returns_conflict(
    registration_client: TestClient,
    registration_database: sessionmaker[Session],
) -> None:
    key = "registration-key-002"

    first = registration_client.post(
        "/v1/users",
        json=registration_payload(),
        headers={"Idempotency-Key": key},
    )
    conflict = registration_client.post(
        "/v1/users",
        json=registration_payload(name="Beatriz Lima"),
        headers={"Idempotency-Key": key},
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSE"

    with registration_database() as db:
        assert db.scalar(select(func.count()).select_from(UserModel)) == 1
        assert db.scalar(select(func.count()).select_from(IdempotencyRecordModel)) == 1


@pytest.mark.parametrize("key", ["too-short", "k" * 129])
def test_invalid_optional_idempotency_key_fails_before_persistence(
    registration_client: TestClient,
    registration_database: sessionmaker[Session],
    key: str,
) -> None:
    response = registration_client.post(
        "/v1/users",
        json=registration_payload(),
        headers={"Idempotency-Key": key},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")

    with registration_database() as db:
        assert db.scalar(select(func.count()).select_from(UserModel)) == 0
        assert db.scalar(select(func.count()).select_from(IdempotencyRecordModel)) == 0


def test_registration_without_idempotency_key_preserves_existing_behavior(
    registration_client: TestClient,
) -> None:
    response = registration_client.post("/v1/users", json=registration_payload())

    assert response.status_code == 201
    assert response.headers["location"].endswith(f"/v1/users/{response.json()['id']}")
    assert response.json()["role"] == "customer"
    assert response.json()["status"] == "active"
    assert "password" not in response.json()
