"""Property-based tests for catalog visibility, stock status and patches."""

from __future__ import annotations

from hypothesis import given, strategies as st

from app.common.types import ProductStatusEnum, UserRoleEnum
from app.products.model import ProductModel
from tests.support import ApiHarness, api_harness, assert_no_credentials, bearer, utc

NAME_ALPHABET = "chocAMRGáÉ %_"
PRODUCT_FIELDS = ("name", "description", "price_cents", "stock_quantity")

names = st.text(alphabet=NAME_ALPHABET, min_size=1, max_size=16).filter(str.strip)
prices = st.integers(min_value=1, max_value=1_000_000)
stocks = st.integers(min_value=0, max_value=1_000_000)
product_statuses = st.sampled_from(list(ProductStatusEnum))
requesters = st.sampled_from(["public", "customer", "admin"])


@st.composite
def catalogs(draw: st.DrawFn) -> list[dict]:
    specs = []
    for _ in range(draw(st.integers(min_value=0, max_value=10))):
        status = draw(product_statuses)
        stock = 0 if status is ProductStatusEnum.OUT_OF_STOCK else draw(
            st.integers(min_value=1, max_value=50)
        )
        specs.append(
            {
                "name": draw(names).strip(),
                "price_cents": draw(st.sampled_from([1, 500, 1_590, 9_999, 1_000_000])),
                "stock_quantity": stock,
                "status": status,
            }
        )
    return specs


def _headers(api: ApiHarness, requester: str) -> dict[str, str]:
    if requester == "public":
        return {}
    role = UserRoleEnum.ADMIN if requester == "admin" else UserRoleEnum.CUSTOMER
    return bearer(api.create_user(role=role))


@given(data=st.data(), catalog=catalogs(), requester=requesters)
def test_catalog_respects_visibility_and_filters(
    data: st.DataObject, catalog: list[dict], requester: str
) -> None:
    # Feature: api-chocomarket, Property 9: Catálogo respeita visibilidade e filtros
    # **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
    with api_harness() as api:
        products = [
            api.create_product(created_at=utc(minutes=index), **spec)
            for index, spec in enumerate(catalog)
        ]
        headers = _headers(api, requester)
        name_filter = data.draw(st.none() | names)
        min_price = data.draw(st.none() | st.sampled_from([1, 500, 1_590, 10_000]))
        max_price = data.draw(st.none() | st.sampled_from([499, 1_590, 1_000_000]))
        allowed_statuses = (
            st.none() | product_statuses
            if requester == "admin"
            else st.sampled_from([None, ProductStatusEnum.ACTIVE])
        )
        status_filter = data.draw(allowed_statuses)
        params = {
            key: value
            for key, value in {
                "limit": 100,
                "name": name_filter,
                "min_price_cents": min_price,
                "max_price_cents": max_price,
                "status": status_filter.value if status_filter else None,
            }.items()
            if value is not None
        }

        response = api.client.get("/v1/products", params=params, headers=headers)

        assert response.status_code == 200, response.text
        effective_status = (
            status_filter if requester == "admin" else ProductStatusEnum.ACTIVE
        )
        expected = [
            str(product.id)
            for product in products
            if (name_filter is None or name_filter.strip().casefold() in product.name.casefold())
            and (min_price is None or product.price_cents >= min_price)
            and (max_price is None or product.price_cents <= max_price)
            and (effective_status is None or product.status is effective_status)
        ]
        body = response.json()
        assert [product["id"] for product in body["data"]] == expected
        assert body["meta"]["has_next"] is False
        assert body["meta"]["next_cursor"] is None
        assert_no_credentials(body)


def _expected_status(
    current: ProductStatusEnum, stock: int, requested: ProductStatusEnum | None
) -> ProductStatusEnum:
    """Reference rules from requirements 3.8/3.9 and the contract notes."""

    if stock == 0:
        return ProductStatusEnum.OUT_OF_STOCK
    if requested in (ProductStatusEnum.ACTIVE, ProductStatusEnum.INACTIVE):
        return requested
    if current is ProductStatusEnum.INACTIVE:
        return ProductStatusEnum.INACTIVE
    return ProductStatusEnum.ACTIVE


@given(
    initial_stock=stocks,
    updates=st.lists(
        st.tuples(stocks, st.none() | product_statuses), min_size=1, max_size=4
    ),
)
def test_product_status_follows_stock(
    initial_stock: int, updates: list[tuple[int, ProductStatusEnum | None]]
) -> None:
    # Feature: api-chocomarket, Property 10: Status do produto acompanha o estoque
    # **Validates: Requirements 3.5, 3.6, 3.8, 3.9**
    with api_harness() as api:
        headers = bearer(api.create_admin())
        created = api.client.post(
            "/v1/products",
            json={"name": "Barra", "price_cents": 990, "stock_quantity": initial_stock},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        current = ProductStatusEnum(created.json()["status"])
        assert current is (
            ProductStatusEnum.OUT_OF_STOCK if initial_stock == 0 else ProductStatusEnum.ACTIVE
        )

        for stock, requested in updates:
            patch: dict = {"stock_quantity": stock}
            if requested is not None:
                patch["status"] = requested.value
            response = api.client.patch(
                f"/v1/products/{created.json()['id']}", json=patch, headers=headers
            )
            assert response.status_code == 200, response.text
            expected = _expected_status(current, stock, requested)
            assert response.json()["status"] == expected.value
            assert response.json()["stock_quantity"] == stock
            current = expected


@st.composite
def product_patches(draw: st.DrawFn) -> dict:
    fields = draw(st.sets(st.sampled_from([*PRODUCT_FIELDS, "status"]), min_size=1))
    values = {
        "name": names,
        "description": st.text(alphabet="doce ", max_size=40),
        "price_cents": prices,
        "stock_quantity": stocks,
        "status": product_statuses.map(lambda status: status.value),
    }
    return {field: draw(values[field]) for field in fields}


@given(
    initial=st.fixed_dictionaries(
        {
            "name": names,
            "description": st.text(alphabet="cacau ", max_size=40),
            "price_cents": prices,
            "stock_quantity": stocks,
        }
    ),
    patch=product_patches(),
)
def test_product_patch_preserves_absent_fields(initial: dict, patch: dict) -> None:
    # Feature: api-chocomarket, Property 11: Atualização parcial de produto preserva campos ausentes
    # **Validates: Requirements 3.7**
    with api_harness() as api:
        product = api.create_product(**initial)

        response = api.client.patch(
            f"/v1/products/{product.id}", json=patch, headers=bearer(api.create_admin())
        )

        assert response.status_code == 200, response.text
        stored = api.get(ProductModel, product.id)
        for field in PRODUCT_FIELDS:
            assert getattr(stored, field) == patch.get(field, initial[field]), field
            assert response.json()[field] == getattr(stored, field)
        requested = ProductStatusEnum(patch["status"]) if "status" in patch else None
        assert stored.status is _expected_status(
            product.status, stored.stock_quantity, requested
        )
        assert stored.created_at == product.created_at
