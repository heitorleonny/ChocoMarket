"""Property-based tests: invalid input fails before any persistence."""

from __future__ import annotations

from hypothesis import given, strategies as st

from tests.support import DEFAULT_PASSWORD, ApiHarness, api_harness, assert_problem, bearer

# For every mutation target: a valid body, the fields required by the
# contract, and values that violate each field's contract constraints.
_ENDPOINTS = {
    "create_user": {
        "body": {"name": "Ana", "email": "nova@example.com", "password": DEFAULT_PASSWORD},
        "required": ["name", "email", "password"],
        "invalid": {
            "name": ["", "n" * 121, 7, None],
            "email": ["sem-arroba", "", 1, None],
            "password": ["curta", "s" * 129, 123456789012, None],
        },
    },
    "update_user": {
        "body": {"name": "Novo Nome"},
        "required": [],
        "invalid": {
            "name": ["", "n" * 121, None],
            "email": ["invalido", None],
            "role": ["root", "", None],
            "status": ["deleted", None],
        },
    },
    "change_password": {
        "body": {"current_password": DEFAULT_PASSWORD, "new_password": "NovaSenhaSegura1"},
        "required": ["current_password", "new_password"],
        "invalid": {
            "current_password": ["curta", "s" * 129, None],
            "new_password": ["curta", "s" * 129, 1, None],
        },
    },
    "create_product": {
        "body": {"name": "Trufa", "description": "Doce", "price_cents": 450, "stock_quantity": 3},
        "required": ["name", "price_cents", "stock_quantity"],
        "invalid": {
            "name": ["", "n" * 161, None],
            "description": ["d" * 2001, None, 5],
            "price_cents": [0, -1, 1_000_001, 4.5, "450", True, None],
            "stock_quantity": [-1, 1_000_001, 2.0, "3", None],
        },
    },
    "update_product": {
        "body": {"price_cents": 700},
        "required": [],
        "invalid": {
            "name": ["", "n" * 161, None],
            "description": ["d" * 2001, None],
            "price_cents": [0, 1_000_001, 1.5, "7", None],
            "stock_quantity": [-1, 1_000_001, None],
            "status": ["sold", None],
        },
    },
    "create_order": {
        "body": None,  # built per example; needs a product ID
        "required": ["items"],
        "invalid": {
            "items": [
                [],
                None,
                "items",
                [{"product_id": "not-a-uuid", "quantity": 1}],
                [{"product_id": "PRODUCT", "quantity": 0}],
                [{"product_id": "PRODUCT", "quantity": 101}],
                [{"product_id": "PRODUCT", "quantity": 1.5}],
                [{"product_id": "PRODUCT"}],
                [{"product_id": "PRODUCT", "quantity": 1, "price_cents": 1}],
                [{"product_id": "PRODUCT", "quantity": 1}, {"product_id": "PRODUCT", "quantity": 2}],
            ]
        },
    },
}


@st.composite
def invalid_requests(draw: st.DrawFn) -> tuple[str, str, object]:
    endpoint = draw(st.sampled_from(sorted(_ENDPOINTS)))
    spec = _ENDPOINTS[endpoint]
    kinds = ["extra", "invalid_value", "not_object"] + (["missing"] if spec["required"] else [])
    if endpoint in ("update_user", "update_product"):
        kinds.append("empty_patch")
    kind = draw(st.sampled_from(kinds))
    if kind == "extra":
        field = draw(st.sampled_from(["role", "status", "id", "password_hash", "unknown", "created_at"]))
        if field in spec["invalid"]:
            field = "unknown"
        return endpoint, kind, (field, draw(st.sampled_from(["admin", 1, None, True])))
    if kind == "missing":
        return endpoint, kind, draw(st.sampled_from(spec["required"]))
    if kind == "invalid_value":
        field = draw(st.sampled_from(sorted(spec["invalid"])))
        return endpoint, kind, (field, draw(st.sampled_from(spec["invalid"][field])))
    if kind == "not_object":
        return endpoint, kind, draw(st.sampled_from([[], "texto", 1, None]))
    return endpoint, kind, None


def _replace_product(value: object, product_id: str) -> object:
    if isinstance(value, list):
        return [_replace_product(item, product_id) for item in value]
    if isinstance(value, dict):
        return {key: _replace_product(item, product_id) for key, item in value.items()}
    return product_id if value == "PRODUCT" else value


def _request(api: ApiHarness, endpoint: str, body: object):
    admin = api.create_admin()
    customer = api.create_user(email="cliente@example.com")
    product = api.create_product(stock_quantity=10)
    routes = {
        "create_user": ("POST", "/v1/users", {}),
        "update_user": ("PATCH", f"/v1/users/{customer.id}", bearer(admin)),
        "change_password": ("PATCH", f"/v1/users/{customer.id}/password", bearer(customer)),
        "create_product": ("POST", "/v1/products", bearer(admin)),
        "update_product": ("PATCH", f"/v1/products/{product.id}", bearer(admin)),
        "create_order": (
            "POST",
            "/v1/orders",
            {**bearer(customer), "Idempotency-Key": "validation-property-key"},
        ),
    }
    method, url, headers = routes[endpoint]
    return api, method, url, headers, _replace_product(body, str(product.id))


@given(request=invalid_requests())
def test_invalid_input_fails_before_persistence(request) -> None:
    # Feature: api-chocomarket, Property 25: Entrada inválida falha antes da persistência
    # **Validates: Requirements 8.6**
    endpoint, kind, detail = request
    spec = _ENDPOINTS[endpoint]
    base = spec["body"] or {"items": [{"product_id": "PRODUCT", "quantity": 1}]}
    if kind == "extra":
        field, value = detail
        body: object = {**base, field: value}
    elif kind == "missing":
        body = {key: value for key, value in base.items() if key != detail}
    elif kind == "invalid_value":
        field, value = detail
        body = {**base, field: value}
    elif kind == "not_object":
        body = detail
    else:
        body = {}

    with api_harness() as api:
        api, method, url, headers, body = _request(api, endpoint, body)
        before = api.snapshot()

        response = api.client.request(method, url, json=body, headers=headers)

        problem = assert_problem(response, 422)
        assert problem["code"] == "VALIDATION_ERROR"
        assert isinstance(problem["errors"], list) and problem["errors"]
        assert api.snapshot() == before
