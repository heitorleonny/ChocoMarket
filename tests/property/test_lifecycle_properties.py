"""Property-based tests for logical deletion of users and products."""

from __future__ import annotations

from hypothesis import given, strategies as st

from app.common.types import ProductStatusEnum, UserStatusEnum
from app.products.model import ProductModel
from app.users.model import UserModel
from tests.support import api_harness, bearer

_USER_PRESERVED = ("name", "email", "role", "password", "created_at")
_PRODUCT_PRESERVED = ("name", "description", "price_cents", "stock_quantity", "created_at")


@given(
    resource=st.sampled_from(["user", "product"]),
    initial_status=st.sampled_from(["active", "inactive", "out_of_stock"]),
    repetitions=st.integers(min_value=1, max_value=4),
    self_service_first=st.booleans(),
)
def test_logical_deletion_is_idempotent(
    resource: str, initial_status: str, repetitions: int, self_service_first: bool
) -> None:
    # Feature: api-chocomarket, Property 8: Exclusão lógica é idempotente
    # **Validates: Requirements 2.9, 2.10, 3.10, 3.11**
    with api_harness() as api:
        admin = api.create_admin()
        if resource == "user":
            status = (
                UserStatusEnum.INACTIVE
                if initial_status == "inactive"
                else UserStatusEnum.ACTIVE
            )
            entity = api.create_user(name="Pessoa", status=status)
            model, preserved, url = UserModel, _USER_PRESERVED, f"/v1/users/{entity.id}"
            # An active customer may deactivate their own profile once; after
            # that the account can no longer authenticate, so admins repeat.
            requesters = [
                entity if self_service_first and status is UserStatusEnum.ACTIVE else admin
            ] + [admin] * (repetitions - 1)
            inactive = UserStatusEnum.INACTIVE
        else:
            entity = api.create_product(
                name="Bombom",
                description="Recheado",
                stock_quantity=0 if initial_status == "out_of_stock" else 7,
                status=ProductStatusEnum(initial_status),
            )
            model, preserved, url = (
                ProductModel,
                _PRODUCT_PRESERVED,
                f"/v1/products/{entity.id}",
            )
            requesters = [admin] * repetitions
            inactive = ProductStatusEnum.INACTIVE

        for requester in requesters:
            response = api.client.delete(url, headers=bearer(requester))
            assert response.status_code == 204, response.text
            assert response.content == b""

            stored = api.get(model, entity.id)
            assert stored is not None
            assert stored.status is inactive
            assert {field: getattr(stored, field) for field in preserved} == {
                field: getattr(entity, field) for field in preserved
            }
