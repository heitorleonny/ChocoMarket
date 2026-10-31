"""Property-based tests for cursor traversal and cursor rejection."""

from __future__ import annotations

from hypothesis import assume, given, strategies as st

from app.common.types import OrderStatusEnum, ProductStatusEnum, UserStatusEnum
from tests.support import ApiHarness, api_harness, assert_problem, bearer, utc

RESOURCES = ("users", "products", "orders")
_BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _seed(api: ApiHarness, resource: str, statuses: list[bool], minutes: list[int]):
    """Create one record per flag (True = matches the status filter).

    Returns the requesting admin and the IDs expected under the filter, in
    the API's ``(created_at, id)`` order.
    """

    admin = api.create_admin(created_at=utc(year=2025))
    product = api.create_product(stock_quantity=1, created_at=utc(year=2025))
    records = []
    for matches, minute in zip(statuses, minutes):
        created_at = utc(minutes=minute)
        if resource == "users":
            status = UserStatusEnum.ACTIVE if matches else UserStatusEnum.INACTIVE
            records.append((api.create_user(status=status, created_at=created_at), matches))
        elif resource == "products":
            status = ProductStatusEnum.ACTIVE if matches else ProductStatusEnum.INACTIVE
            records.append(
                (api.create_product(status=status, created_at=created_at), matches)
            )
        else:
            status = OrderStatusEnum.CONFIRMED if matches else OrderStatusEnum.CANCELED
            customer = api.create_user(created_at=utc(year=2025))
            records.append(
                (api.create_order(customer, product, status=status, created_at=created_at), matches)
            )
    expected = [
        str(record.id)
        for record, matches in sorted(records, key=lambda pair: (pair[0].created_at, str(pair[0].id)))
        if matches
    ]
    if resource == "users":
        expected.insert(0, str(admin.id))
    if resource == "products":
        expected.insert(0, str(product.id))
    return admin, expected


_FILTER = {"users": "active", "products": "active", "orders": "confirmed"}


@given(
    resource=st.sampled_from(RESOURCES),
    flags=st.lists(st.booleans(), max_size=25),
    data=st.data(),
    limit=st.integers(min_value=1, max_value=100),
)
def test_page_traversal_is_stable_and_complete(resource, flags, data, limit) -> None:
    # Feature: api-chocomarket, Property 22: Travessia de páginas é estável e completa
    # **Validates: Requirements 7.1, 7.2, 7.3, 7.5**
    # Few distinct minutes force timestamp ties, exercising the ID tie-breaker.
    minutes = data.draw(st.lists(st.integers(0, 3), min_size=len(flags), max_size=len(flags)))
    with api_harness() as api:
        admin, expected = _seed(api, resource, flags, minutes)
        params = {"limit": limit, "status": _FILTER[resource]}
        seen: list[str] = []
        cursor = None

        for _ in range(len(expected) + 2):
            page_params = {**params, **({"cursor": cursor} if cursor else {})}
            response = api.client.get(f"/v1/{resource}", params=page_params, headers=bearer(admin))
            assert response.status_code == 200, response.text
            body = response.json()
            meta = body["meta"]
            assert meta["limit"] == limit
            seen.extend(item["id"] for item in body["data"])
            if meta["has_next"]:
                assert len(body["data"]) == limit
                assert meta["next_cursor"]
                cursor = meta["next_cursor"]
            else:
                assert meta["next_cursor"] is None
                assert len(body["data"]) <= limit
                break
        else:
            raise AssertionError("pagination did not terminate")

        assert seen == expected


@st.composite
def corruptions(draw: st.DrawFn) -> tuple[str, int, str]:
    kind = draw(
        st.sampled_from(["flip", "truncate", "garbage", "other_resource", "other_filter", "other_view"])
    )
    return kind, draw(st.integers(min_value=0, max_value=10**6)), draw(
        st.text(alphabet=_BASE64URL + ".=", min_size=1, max_size=80)
    )


@given(resource=st.sampled_from(("products", "orders")), corruption=corruptions())
def test_invalid_cursor_returns_422_without_results(resource, corruption) -> None:
    # Feature: api-chocomarket, Property 23: Cursor inválido não produz resultados
    # **Validates: Requirements 7.4**
    kind, position, garbage = corruption
    with api_harness() as api:
        customer = api.create_user()
        product = api.create_product()
        for minute in range(3):
            api.create_product(created_at=utc(minutes=minute))
            api.create_order(customer, product, created_at=utc(minutes=minute))
        first = api.client.get(f"/v1/{resource}", params={"limit": 1}, headers=bearer(customer))
        cursor = first.json()["meta"]["next_cursor"]
        assert cursor

        url, params, headers = f"/v1/{resource}", {"limit": 1}, bearer(customer)
        if kind == "flip":
            # The final character of the signature partly encodes padding
            # bits, so changing it may not change the decoded signature.
            index = position % (len(cursor) - 1)
            replacement = next(c for c in _BASE64URL + "." if c != cursor[index])
            cursor = cursor[:index] + replacement + cursor[index + 1 :]
        elif kind == "truncate":
            cursor = cursor[: position % len(cursor)] or "x"
        elif kind == "garbage":
            assume(garbage != cursor)
            cursor = garbage
        elif kind == "other_resource":
            url = "/v1/orders" if resource == "products" else "/v1/products"
        elif kind == "other_filter":
            params = {**params, **(
                {"name": "choc"} if resource == "products" else {"status": "confirmed"}
            )}
        else:
            headers = bearer(api.create_user())

        response = api.client.get(url, params={**params, "cursor": cursor}, headers=headers)

        body = assert_problem(response, 422)
        assert body["code"] == "VALIDATION_ERROR"
        assert "data" not in body
