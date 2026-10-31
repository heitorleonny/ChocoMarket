import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.users.service import users_by_email


client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_users() -> None:
    users_by_email.clear()

def test_create_customer_returns_201_without_password() -> None:
    response = client.post(
        "/v1/users",
        json={
            "name": "Ana Souza",
            "email": "ana@example.com",
            "password": "UmaSenhaSeguraCom12Caracteres",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "customer"
    assert body["status"] == "active"
    assert "password" not in body
    assert "password_hash" not in body


def test_create_customer_rejects_admin_role() -> None:
    response = client.post(
        "/v1/users",
        json={
            "name": "Ana Souza",
            "email": "ana@example.com",
            "password": "UmaSenhaSeguraCom12Caracteres",
            "role": "admin",
        },
    )

    assert response.status_code == 422


def test_create_customer_rejects_duplicate_email() -> None:
    payload = {
        "name": "Ana Souza",
        "email": "ana@example.com",
        "password": "UmaSenhaSeguraCom12Caracteres",
    }

    first_response = client.post("/v1/users", json=payload)
    second_response = client.post("/v1/users", json=payload)

    assert first_response.status_code == 201
    assert second_response.status_code == 409