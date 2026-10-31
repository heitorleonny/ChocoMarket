from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.auth import require_admin
from app.database import Base, create_database_engine, create_session_factory, get_db
from app.main import app
from app.products.model import ProductModel
from app.users.model import UserModel
from app.common.types import UserRoleEnum, UserStatusEnum


@pytest.fixture
def product_client():
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    setup = session_factory()
    now = datetime.now(UTC)
    admin = UserModel(
        id=uuid4(),
        name="Admin",
        email="admin@example.com",
        password="not-used-in-this-test",
        role=UserRoleEnum.ADMIN,
        status=UserStatusEnum.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    setup.add(admin)
    setup.commit()
    setup.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[require_admin] = lambda: admin
    try:
        with TestClient(app) as client:
            yield client, session_factory
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_admin, None)
        engine.dispose()


def test_admin_product_mutations_apply_status_patch_delete_and_idempotency(product_client) -> None:
    client, session_factory = product_client
    key = "product-create-key-001"
    payload = {
        "name": "Dark chocolate",
        "description": "70 percent cacao",
        "price_cents": 1590,
        "stock_quantity": 5,
    }

    created = client.post("/v1/products", json=payload, headers={"Idempotency-Key": key})
    replay = client.post("/v1/products", json=payload, headers={"Idempotency-Key": key})

    assert created.status_code == 201
    assert replay.status_code == 201
    assert created.headers["Location"] == f"/v1/products/{created.json()['id']}"
    assert replay.headers["Location"] == created.headers["Location"]
    assert created.json() == replay.json()
    assert created.json()["status"] == "active"

    db = session_factory()
    assert db.scalar(select(func.count()).select_from(ProductModel)) == 1
    db.close()

    product_id = created.json()["id"]
    patched = client.patch(
        f"/v1/products/{product_id}",
        json={"stock_quantity": 0},
        headers={"Content-Type": "application/merge-patch+json"},
    )
    assert patched.status_code == 200
    assert patched.json()["stock_quantity"] == 0
    assert patched.json()["status"] == "out_of_stock"
    assert patched.json()["name"] == payload["name"]

    deleted = client.delete(f"/v1/products/{product_id}")
    repeated_delete = client.delete(f"/v1/products/{product_id}")
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert repeated_delete.status_code == 204
    assert repeated_delete.content == b""

    db = session_factory()
    product = db.scalar(select(ProductModel).where(ProductModel.id == product_id))
    assert product is not None
    assert product.status.value == "inactive"
    db.close()


def test_product_mutations_reject_conflicting_idempotency_and_invalid_targets(product_client) -> None:
    client, _session_factory = product_client
    key = "product-create-key-002"
    first = client.post(
        "/v1/products",
        json={"name": "First", "price_cents": 100, "stock_quantity": 1},
        headers={"Idempotency-Key": key},
    )
    conflict = client.post(
        "/v1/products",
        json={"name": "Different", "price_cents": 100, "stock_quantity": 1},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")

    invalid_key = client.post(
        "/v1/products",
        json={"name": "Invalid key", "price_cents": 100, "stock_quantity": 1},
        headers={"Idempotency-Key": "too-short"},
    )
    assert invalid_key.status_code == 422

    missing = client.patch(
        f"/v1/products/{uuid4()}",
        json={"name": "No product"},
        headers={"Content-Type": "application/merge-patch+json"},
    )
    assert missing.status_code == 404

    invalid_patch = client.patch(
        f"/v1/products/{first.json()['id']}",
        json={},
        headers={"Content-Type": "application/merge-patch+json"},
    )
    assert invalid_patch.status_code == 422
