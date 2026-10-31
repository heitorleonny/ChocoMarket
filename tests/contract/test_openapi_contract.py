"""Contract tests: routes and real responses must match ``openapi.yaml``."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.openapi.utils import get_openapi

from app.common.types import OrderStatusEnum, ProductStatusEnum, UserStatusEnum
from app.main import app, load_contract
from tests.contract.openapi import (
    assert_response_matches,
    contract_operations,
    normalize_path,
)
from tests.support import DEFAULT_PASSWORD, ApiHarness, assert_no_credentials, bearer

NON_CONTRACT_PATHS = {"/health"}


def _implemented_operations() -> dict[tuple[str, str], dict]:
    generated = get_openapi(title=app.title, version=app.version, routes=app.routes)
    return {
        (method.upper(), normalize_path(path)): operation
        for path, item in generated["paths"].items()
        if path not in NON_CONTRACT_PATHS
        for method, operation in item.items()
    }


def test_registered_routes_are_exactly_the_contract_operations() -> None:
    assert set(_implemented_operations()) == set(contract_operations())


def test_path_parameter_names_match_the_contract() -> None:
    generated = get_openapi(title=app.title, version=app.version, routes=app.routes)
    implemented_paths = set(generated["paths"]) - NON_CONTRACT_PATHS
    assert implemented_paths == set(load_contract()["paths"])


def test_success_status_of_each_route_matches_the_contract() -> None:
    implemented = _implemented_operations()
    for key, operation in contract_operations().items():
        documented = {code for code in operation["responses"] if code.startswith("2")}
        generated = {code for code in implemented[key]["responses"] if code.startswith("2")}
        assert generated == documented, key


def test_openapi_document_served_is_the_manual_contract(api: ApiHarness) -> None:
    response = api.client.get("/openapi.json")

    assert response.status_code == 200
    served = response.json()
    contract = load_contract()
    # Only the server list differs: "/" comes first so /docs calls this instance.
    assert served["servers"] == [{"url": "/", "description": "Este servidor"}, *contract["servers"]]
    assert {**served, "servers": contract["servers"]} == contract


def test_user_operations_match_contract(api: ApiHarness) -> None:
    client = api.client
    admin = api.create_admin()
    customer = api.create_user()

    created = client.post(
        "/v1/users",
        json={"name": "Ana", "email": "ana@example.com", "password": DEFAULT_PASSWORD},
        headers={"Idempotency-Key": "contract-user-key-0001"},
    )
    body = assert_response_matches(created, "POST", "/v1/users")
    assert created.status_code == 201
    assert_no_credentials(body)
    new_user_id = body["id"]

    replay = client.post(
        "/v1/users",
        json={"name": "Ana", "email": "ana@example.com", "password": DEFAULT_PASSWORD},
        headers={"Idempotency-Key": "contract-user-key-0001"},
    )
    assert replay.status_code == 201
    assert assert_response_matches(replay, "POST", "/v1/users") == body

    cases = [
        ("POST", "/v1/users", {"json": {"name": "Ana", "email": "ANA@example.com", "password": DEFAULT_PASSWORD}}, 409),
        ("POST", "/v1/users", {"json": {"name": "Ana", "email": "x@example.com", "password": DEFAULT_PASSWORD, "role": "admin"}}, 422),
        ("POST", "/v1/users", {"json": {"name": "A", "email": "y@example.com", "password": DEFAULT_PASSWORD}, "headers": {"Idempotency-Key": "short"}}, 422),
        ("GET", "/v1/users", {"headers": bearer(admin)}, 200),
        ("GET", "/v1/users", {"headers": bearer(admin), "params": {"status": "inactive", "limit": 1}}, 200),
        ("GET", "/v1/users", {}, 401),
        ("GET", "/v1/users", {"headers": bearer(customer)}, 403),
        ("GET", "/v1/users", {"headers": bearer(admin), "params": {"limit": 0}}, 422),
        ("GET", "/v1/users", {"headers": bearer(admin), "params": {"cursor": "not-a-cursor"}}, 422),
        ("GET", f"/v1/users/{customer.id}", {"headers": bearer(customer)}, 200),
        ("GET", f"/v1/users/{admin.id}", {"headers": bearer(customer)}, 404),
        ("GET", "/v1/users/not-a-uuid", {"headers": bearer(admin)}, 422),
        ("PATCH", f"/v1/users/{customer.id}", {"headers": bearer(customer), "json": {"name": "Novo Nome"}}, 200),
        ("PATCH", f"/v1/users/{customer.id}", {"headers": bearer(customer), "json": {"role": "admin"}}, 403),
        ("PATCH", f"/v1/users/{customer.id}", {"headers": bearer(customer), "json": {"email": "ana@example.com"}}, 409),
        ("PATCH", f"/v1/users/{customer.id}", {"headers": bearer(customer), "json": {}}, 422),
        ("PATCH", f"/v1/users/{customer.id}/password", {"headers": bearer(customer), "json": {"current_password": "SenhaErrada12345", "new_password": "NovaSenhaSegura1"}}, 401),
        ("PATCH", f"/v1/users/{customer.id}/password", {"headers": bearer(admin), "json": {"current_password": DEFAULT_PASSWORD, "new_password": "NovaSenhaSegura1"}}, 403),
        ("PATCH", f"/v1/users/{customer.id}/password", {"headers": bearer(customer), "json": {"current_password": DEFAULT_PASSWORD}}, 422),
        ("PATCH", f"/v1/users/{customer.id}/password", {"headers": bearer(customer), "json": {"current_password": DEFAULT_PASSWORD, "new_password": "NovaSenhaSegura1"}}, 204),
        ("DELETE", f"/v1/users/{uuid4()}", {"headers": bearer(admin)}, 404),
        ("DELETE", f"/v1/users/{new_user_id}", {"headers": bearer(admin)}, 204),
        ("DELETE", f"/v1/users/{new_user_id}", {"headers": bearer(admin)}, 204),
        ("GET", f"/v1/users/{new_user_id}", {"headers": bearer(admin)}, 200),
    ]
    _run_cases(api, cases)


def test_product_operations_match_contract(api: ApiHarness) -> None:
    admin = api.create_admin()
    customer = api.create_user()
    hidden = api.create_product(name="Inativo", status=ProductStatusEnum.INACTIVE)

    created = api.client.post(
        "/v1/products",
        json={"name": "Trufa", "price_cents": 450, "stock_quantity": 0},
        headers=bearer(admin),
    )
    body = assert_response_matches(created, "POST", "/v1/products")
    assert created.status_code == 201
    assert body["status"] == "out_of_stock"
    product_id = body["id"]

    cases = [
        ("POST", "/v1/products", {"headers": bearer(customer), "json": {"name": "X", "price_cents": 1, "stock_quantity": 1}}, 403),
        ("POST", "/v1/products", {"json": {"name": "X", "price_cents": 1, "stock_quantity": 1}}, 401),
        ("POST", "/v1/products", {"headers": bearer(admin), "json": {"name": "X", "price_cents": 0, "stock_quantity": 1}}, 422),
        ("POST", "/v1/products", {"headers": bearer(admin), "json": {"name": "X", "price_cents": "10", "stock_quantity": 1}}, 422),
        ("GET", "/v1/products", {}, 200),
        ("GET", "/v1/products", {"headers": bearer(admin), "params": {"status": "inactive"}}, 200),
        ("GET", "/v1/products", {"headers": bearer(customer), "params": {"status": "inactive"}}, 422),
        ("GET", "/v1/products", {"headers": {"Authorization": "Bearer invalid"}}, 401),
        ("GET", f"/v1/products/{hidden.id}", {"headers": bearer(admin)}, 200),
        ("GET", f"/v1/products/{hidden.id}", {}, 404),
        ("PATCH", f"/v1/products/{product_id}", {"headers": bearer(admin), "json": {"stock_quantity": 3}}, 200),
        ("PATCH", f"/v1/products/{product_id}", {"headers": bearer(customer), "json": {"stock_quantity": 3}}, 403),
        ("PATCH", f"/v1/products/{uuid4()}", {"headers": bearer(admin), "json": {"stock_quantity": 3}}, 404),
        ("PATCH", f"/v1/products/{product_id}", {"headers": bearer(admin), "json": {"name": None}}, 422),
        ("DELETE", f"/v1/products/{product_id}", {"headers": bearer(admin)}, 204),
        ("DELETE", f"/v1/products/{uuid4()}", {"headers": bearer(admin)}, 404),
        ("DELETE", f"/v1/products/{product_id}", {}, 401),
    ]
    _run_cases(api, cases)


def test_order_operations_match_contract(api: ApiHarness) -> None:
    admin = api.create_admin()
    customer = api.create_user()
    other = api.create_user()
    product = api.create_product(price_cents=1_590, stock_quantity=3)

    created = api.client.post(
        "/v1/orders",
        json={"items": [{"product_id": str(product.id), "quantity": 2}]},
        headers={**bearer(customer), "Idempotency-Key": "contract-order-key-01"},
    )
    body = assert_response_matches(created, "POST", "/v1/orders")
    assert created.status_code == 201
    assert body["total_cents"] == 3_180
    order_id = body["id"]

    cases = [
        ("POST", "/v1/orders", {"headers": {**bearer(customer), "Idempotency-Key": "contract-order-key-02"}, "json": {"items": [{"product_id": str(product.id), "quantity": 5}]}}, 409),
        ("POST", "/v1/orders", {"headers": {**bearer(customer), "Idempotency-Key": "contract-order-key-01"}, "json": {"items": [{"product_id": str(product.id), "quantity": 1}]}}, 409),
        ("POST", "/v1/orders", {"headers": bearer(customer), "json": {"items": [{"product_id": str(product.id), "quantity": 1}]}}, 422),
        ("POST", "/v1/orders", {"headers": {**bearer(customer), "Idempotency-Key": "contract-order-key-03"}, "json": {"items": []}}, 422),
        ("POST", "/v1/orders", {"headers": {"Idempotency-Key": "contract-order-key-04"}, "json": {"items": [{"product_id": str(product.id), "quantity": 1}]}}, 401),
        ("POST", "/v1/orders", {"headers": {**bearer(admin), "Idempotency-Key": "contract-order-key-05"}, "json": {"items": [{"product_id": str(product.id), "quantity": 1}]}}, 403),
        ("GET", "/v1/orders", {"headers": bearer(customer)}, 200),
        ("GET", "/v1/orders", {"headers": bearer(admin), "params": {"user_id": str(customer.id), "status": "confirmed"}}, 200),
        ("GET", "/v1/orders", {}, 401),
        ("GET", "/v1/orders", {"headers": bearer(customer), "params": {"created_after": "2026-02-01T00:00:00Z", "created_before": "2026-01-01T00:00:00Z"}}, 422),
        ("GET", f"/v1/orders/{order_id}", {"headers": bearer(customer)}, 200),
        ("GET", f"/v1/orders/{order_id}", {"headers": bearer(other)}, 404),
        ("GET", f"/v1/orders/{order_id}", {"headers": bearer(admin)}, 200),
        ("POST", f"/v1/orders/{order_id}/cancel", {"headers": bearer(other)}, 404),
        ("POST", f"/v1/orders/{order_id}/cancel", {"headers": bearer(customer)}, 200),
        ("POST", f"/v1/orders/{order_id}/cancel", {"headers": bearer(admin)}, 200),
        ("POST", f"/v1/orders/{uuid4()}/cancel", {"headers": bearer(admin)}, 404),
    ]
    _run_cases(api, cases)
    canceled = api.client.get(f"/v1/orders/{order_id}", headers=bearer(admin)).json()
    assert canceled["status"] == OrderStatusEnum.CANCELED.value


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/users"),
        ("GET", "/v1/users/{id}"),
        ("PATCH", "/v1/users/{id}"),
        ("DELETE", "/v1/users/{id}"),
        ("PATCH", "/v1/users/{id}/password"),
        ("POST", "/v1/products"),
        ("PATCH", "/v1/products/{id}"),
        ("DELETE", "/v1/products/{id}"),
        ("GET", "/v1/orders"),
        ("POST", "/v1/orders"),
        ("GET", "/v1/orders/{id}"),
        ("POST", "/v1/orders/{id}/cancel"),
    ],
)
def test_inactive_user_gets_contract_401_on_every_protected_route(
    api: ApiHarness, method: str, path: str
) -> None:
    inactive = api.create_user(status=UserStatusEnum.INACTIVE)
    url = path.replace("{id}", str(inactive.id))

    response = api.client.request(method, url, headers=bearer(inactive), json={})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert_response_matches(response, method, path)


def _run_cases(api: ApiHarness, cases: list[tuple[str, str, dict, int]]) -> None:
    for method, url, kwargs, expected in cases:
        response = api.client.request(method, url, **kwargs)
        assert response.status_code == expected, (method, url, response.text)
        template = "/".join(
            "{id}" if _looks_like_id(segment) else segment for segment in url.split("/")
        )
        body = assert_response_matches(response, method, template)
        if body is not None:
            assert_no_credentials(body)
            if response.status_code >= 400:
                assert body["request_id"] == response.headers["X-Request-ID"]


def _looks_like_id(segment: str) -> bool:
    return len(segment) == 36 or segment == "not-a-uuid"
