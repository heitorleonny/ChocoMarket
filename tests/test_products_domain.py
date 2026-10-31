from __future__ import annotations
from __future__ import annotations

from datetime import UTC
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.common.types import ProductStatusEnum
from app.database import Base, create_database_engine, create_session_factory
from app.products.repository import ProductRepository
from app.products.schemas import CreateProductRequest, ProductResponse, UpdateProductRequest
from app.products.service import (
    apply_product_patch,
    product_from_create,
    status_for_product_creation,
    transition_product_status,
)


@pytest.fixture()
def product_session():
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    with session_factory() as db:
        yield db
    engine.dispose()


def test_create_schema_defaults_description_and_forbids_extra_fields() -> None:
    payload = CreateProductRequest(name="Chocolate", price_cents=1590, stock_quantity=4)

    assert payload.description == ""
    with pytest.raises(ValidationError):
        CreateProductRequest(
            name="Chocolate",
            price_cents=1590,
            stock_quantity=4,
            status="active",
        )


def test_product_schemas_enforce_contract_limits_and_non_empty_patch() -> None:
    with pytest.raises(ValidationError):
        CreateProductRequest(name="", price_cents=1590, stock_quantity=1)
    with pytest.raises(ValidationError):
        CreateProductRequest(name="Chocolate", price_cents=0, stock_quantity=1)
    with pytest.raises(ValidationError):
        CreateProductRequest(name="Chocolate", price_cents=1590, stock_quantity=1_000_001)
    with pytest.raises(ValidationError):
        UpdateProductRequest()
    with pytest.raises(ValidationError):
        UpdateProductRequest(stock_quantity=None)


def test_product_response_projects_model_fields_without_credentials() -> None:
    product = product_from_create(
        CreateProductRequest(
            name="Chocolate",
            description="Dark chocolate",
            price_cents=1590,
            stock_quantity=4,
        )
    )
    product.id = uuid4()

    response = ProductResponse.model_validate(product)

    assert response.id is not None
    assert response.created_at.tzinfo is UTC
    assert response.model_dump().keys() == {
        "id",
        "name",
        "description",
        "price_cents",
        "stock_quantity",
        "status",
        "created_at",
        "updated_at",
    }
    assert "password" not in response.model_dump()


def test_status_for_creation_follows_stock() -> None:
    assert status_for_product_creation(0) is ProductStatusEnum.OUT_OF_STOCK
    assert status_for_product_creation(1) is ProductStatusEnum.ACTIVE


def test_status_transition_prioritizes_zero_stock_and_explicit_inactive() -> None:
    assert transition_product_status(
        current_status=ProductStatusEnum.ACTIVE,
        stock_quantity=0,
    ) is ProductStatusEnum.OUT_OF_STOCK
    assert transition_product_status(
        current_status=ProductStatusEnum.OUT_OF_STOCK,
        stock_quantity=5,
    ) is ProductStatusEnum.ACTIVE
    assert transition_product_status(
        current_status=ProductStatusEnum.INACTIVE,
        stock_quantity=5,
    ) is ProductStatusEnum.INACTIVE
    assert transition_product_status(
        current_status=ProductStatusEnum.INACTIVE,
        stock_quantity=5,
        requested_status=ProductStatusEnum.ACTIVE,
    ) is ProductStatusEnum.ACTIVE
    assert transition_product_status(
        current_status=ProductStatusEnum.INACTIVE,
        stock_quantity=0,
        requested_status=ProductStatusEnum.INACTIVE,
    ) is ProductStatusEnum.OUT_OF_STOCK


def test_apply_patch_preserves_absent_fields_and_derives_status() -> None:
    product = product_from_create(
        CreateProductRequest(
            name="Original",
            description="Description",
            price_cents=2000,
            stock_quantity=8,
        )
    )

    apply_product_patch(product, UpdateProductRequest(stock_quantity=0))

    assert product.name == "Original"
    assert product.description == "Description"
    assert product.price_cents == 2000
    assert product.stock_quantity == 0
    assert product.status is ProductStatusEnum.OUT_OF_STOCK


def test_repository_supports_create_filters_update_and_idempotent_deactivate(product_session) -> None:
    dark = ProductRepository.create(
        product_session,
        name="Dark Chocolate",
        description="70% cacao",
        price_cents=1590,
        stock_quantity=10,
    )
    milk = ProductRepository.create(
        product_session,
        name="Milk Chocolate",
        description="Smooth",
        price_cents=990,
        stock_quantity=0,
    )
    product_session.commit()

    assert dark.status is ProductStatusEnum.ACTIVE
    assert milk.status is ProductStatusEnum.OUT_OF_STOCK
    assert ProductRepository.get_by_id(product_session, str(dark.id)).id == dark.id
    assert ProductRepository.get_by_id(product_session, "not-a-uuid") is None

    matches = ProductRepository.list(
        product_session,
        name="DARK",
        min_price_cents=1000,
        max_price_cents=2000,
        status=ProductStatusEnum.ACTIVE,
    )
    assert [product.id for product in matches] == [dark.id]

    ProductRepository.update(
        product_session,
        milk,
        UpdateProductRequest(stock_quantity=5),
    )
    product_session.commit()
    assert milk.stock_quantity == 5
    assert milk.status is ProductStatusEnum.ACTIVE

    ProductRepository.deactivate(product_session, dark)
    product_session.commit()
    first_updated_at = dark.updated_at
    assert dark.status is ProductStatusEnum.INACTIVE

    ProductRepository.deactivate(product_session, dark)
    product_session.commit()
    assert dark.status is ProductStatusEnum.INACTIVE
    assert dark.updated_at == first_updated_at
