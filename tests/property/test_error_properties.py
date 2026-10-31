"""Property-based tests: every predicted error uses the Problem envelope."""

from __future__ import annotations

from uuid import uuid4

from hypothesis import given, strategies as st

from tests.contract.openapi import assert_response_matches
from tests.support import ApiHarness, api_harness, assert_problem, bearer, make_token

_REQUEST_IDS = st.one_of(
    st.none(),
    st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}", fullmatch=True),
    # Unsafe IDs must be replaced, never echoed (HTTP headers are ASCII only).
    st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), max_size=20),
)


def _scenarios(api: ApiHarness) -> dict[str, tuple[int, str, str, dict]]:
    """Requests that each trigger one error predicted by the contract.

    Values are ``(status, method, url, request kwargs)``; the URL template
    for contract lookup is derived from the URL.
    """

    admin = api.create_admin()
    customer = api.create_user(email="dono@example.com")
    other = api.create_user()
    product = api.create_product(stock_quantity=1)
    order = api.create_order(customer, product)
    expired = {"Authorization": f"Bearer {make_token(customer.id, expires_in=-60)}"}
    return {
        "missing_token": (401, "GET", "/v1/orders", {}),
        "expired_token": (401, "GET", f"/v1/users/{customer.id}", {"headers": expired}),
        "malformed_token": (401, "GET", "/v1/products", {"headers": {"Authorization": "Bearer a.b.c"}}),
        "customer_lists_users": (403, "GET", "/v1/users", {"headers": bearer(customer)}),
        "customer_creates_product": (
            403, "POST", "/v1/products",
            {"headers": bearer(customer), "json": {"name": "X", "price_cents": 1, "stock_quantity": 1}},
        ),
        "foreign_order": (404, "GET", f"/v1/orders/{order.id}", {"headers": bearer(other)}),
        "missing_product": (404, "GET", f"/v1/products/{uuid4()}", {}),
        "missing_user": (404, "DELETE", f"/v1/users/{uuid4()}", {"headers": bearer(admin)}),
        "duplicate_email": (
            409, "POST", "/v1/users",
            {"json": {"name": "D", "email": "DONO@example.com", "password": "SenhaSegura123"}},
        ),
        "insufficient_stock": (
            409, "POST", "/v1/orders",
            {
                "headers": {**bearer(customer), "Idempotency-Key": "error-property-key-1"},
                "json": {"items": [{"product_id": str(product.id), "quantity": 2}]},
            },
        ),
        "invalid_limit": (422, "GET", "/v1/products", {"params": {"limit": 101}}),
        "invalid_path_uuid": (422, "GET", "/v1/orders/123", {"headers": bearer(customer)}),
        "invalid_body": (422, "PATCH", f"/v1/products/{product.id}", {"headers": bearer(admin), "json": {}}),
        "missing_idempotency_key": (
            422, "POST", "/v1/orders",
            {"headers": bearer(customer), "json": {"items": [{"product_id": str(product.id), "quantity": 1}]}},
        ),
    }


@given(
    scenario=st.sampled_from(
        [
            "missing_token", "expired_token", "malformed_token", "customer_lists_users",
            "customer_creates_product", "foreign_order", "missing_product", "missing_user",
            "duplicate_email", "insufficient_stock", "invalid_limit", "invalid_path_uuid",
            "invalid_body", "missing_idempotency_key",
        ]
    ),
    request_id=_REQUEST_IDS,
)
def test_every_predicted_error_has_a_consistent_envelope(scenario: str, request_id) -> None:
    # Feature: api-chocomarket, Property 26: Todo erro previsto tem envelope consistente
    # **Validates: Requirements 8.7**
    with api_harness() as api:
        status, method, url, kwargs = _scenarios(api)[scenario]
        headers = dict(kwargs.pop("headers", {}))
        if request_id is not None:
            headers["X-Request-ID"] = request_id

        response = api.client.request(method, url, headers=headers, **kwargs)

        body = assert_problem(response, status)
        assert body["type"].startswith("https://")
        if status == 422:
            assert body["errors"]
        if status == 401:
            assert response.headers["WWW-Authenticate"] == "Bearer"
        safe_id = request_id is not None and request_id == request_id.strip() and (
            request_id[:1].isalnum() and request_id.isascii()
            and all(char.isalnum() or char in "._-" for char in request_id)
            and len(request_id) <= 128
        )
        if safe_id:
            assert body["request_id"] == request_id
        template = "/".join(
            "{id}" if len(part) == 36 or part == "123" else part for part in url.split("/")
        )
        assert_response_matches(response, method, template)
