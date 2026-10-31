from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient

from app.core.errors import register_exception_handlers
from app.core.http import RequestIDMiddleware


def _test_app() -> FastAPI:
    test_app = FastAPI()
    test_app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(test_app)

    @test_app.get("/validation")
    def validation(value: int = Query(...)) -> dict[str, int]:
        return {"value": value}

    @test_app.get("/unauthorized")
    def unauthorized() -> None:
        raise HTTPException(
            status_code=401,
            detail="Authentication is required or invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @test_app.get("/boom")
    def boom() -> None:
        raise RuntimeError("password=must-not-be-returned")

    return test_app


def test_validation_errors_have_problem_envelope_and_propagate_request_id() -> None:
    client = TestClient(_test_app())

    response = client.get(
        "/validation?value=not-an-integer",
        headers={"X-Request-ID": "req_test-123"},
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["X-Request-ID"] == "req_test-123"
    body = response.json()
    assert body["type"] == "https://api.chocomarket.com/problems/validation-error"
    assert body["title"] == "Validation Error"
    assert body["status"] == 422
    assert body["code"] == "VALIDATION_ERROR"
    assert body["request_id"] == "req_test-123"
    assert body["errors"][0]["field"] == "query.value"
    assert "not-an-integer" not in response.text


def test_invalid_request_id_is_replaced_with_a_generated_id() -> None:
    client = TestClient(_test_app())

    response = client.get(
        "/validation?value=not-an-integer",
        headers={"X-Request-ID": "not safe/request id"},
    )

    request_id = response.headers["X-Request-ID"]
    assert response.status_code == 422
    assert request_id.startswith("req_")
    assert response.json()["request_id"] == request_id
    assert request_id != "not safe/request id"


def test_http_exception_preserves_authentication_challenge() -> None:
    client = TestClient(_test_app())

    response = client.get("/unauthorized")

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    body = response.json()
    assert body["status"] == 401
    assert body["code"] == "UNAUTHORIZED"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_unexpected_exception_returns_generic_problem_without_internal_detail() -> None:
    client = TestClient(_test_app(), raise_server_exceptions=False)

    response = client.get("/boom")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "https://api.chocomarket.com/problems/internal-error"
    assert body["status"] == 500
    assert body["code"] == "INTERNAL_ERROR"
    assert "must-not-be-returned" not in response.text
    assert body["request_id"] == response.headers["X-Request-ID"]
