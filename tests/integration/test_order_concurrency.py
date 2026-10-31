"""Concurrent HTTP requests competing for the same stock or order."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orders.model import OrderModel
from tests.support import ApiHarness, api_harness, bearer

pytestmark = pytest.mark.concurrency


@pytest.fixture
def file_api(tmp_path: Path) -> Iterator[ApiHarness]:
    """Harness on a file database so each thread uses its own connection."""

    with api_harness(f"sqlite:///{tmp_path / 'concurrency.db'}") as harness:
        yield harness


def _race(*requests: Callable[[TestClient], object]) -> list:
    """Start every request at the same time, each on its own client."""

    barrier = Barrier(len(requests))

    def run(request: Callable[[TestClient], object]):
        client = TestClient(app)
        barrier.wait()
        return request(client)

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        return list(executor.map(run, requests))


def _order(product_id, quantity: int = 1) -> dict:
    return {"items": [{"product_id": str(product_id), "quantity": quantity}]}


def test_two_customers_cannot_both_buy_the_last_unit(file_api: ApiHarness) -> None:
    first, second = file_api.create_user(), file_api.create_user()
    product = file_api.create_product(stock_quantity=1)

    responses = _race(
        lambda client: client.post(
            "/v1/orders",
            json=_order(product.id),
            headers={**bearer(first), "Idempotency-Key": "concurrency-first-01"},
        ),
        lambda client: client.post(
            "/v1/orders",
            json=_order(product.id),
            headers={**bearer(second), "Idempotency-Key": "concurrency-second-1"},
        ),
    )

    assert sorted(response.status_code for response in responses) == [201, 409]
    assert file_api.stock(product.id) == 0
    assert file_api.count(OrderModel) == 1


def test_concurrent_retries_with_one_key_create_one_order(file_api: ApiHarness) -> None:
    customer = file_api.create_user()
    product = file_api.create_product(stock_quantity=10)
    headers = {**bearer(customer), "Idempotency-Key": "concurrency-same-key-1"}

    responses = _race(
        *[
            lambda client: client.post("/v1/orders", json=_order(product.id, 2), headers=headers)
            for _ in range(3)
        ]
    )

    assert [response.status_code for response in responses] == [201, 201, 201]
    assert len({response.json()["id"] for response in responses}) == 1
    assert file_api.count(OrderModel) == 1
    assert file_api.stock(product.id) == 8


def test_concurrent_cancellations_restore_stock_once(file_api: ApiHarness) -> None:
    customer = file_api.create_user()
    admin = file_api.create_admin()
    product = file_api.create_product(stock_quantity=4)
    created = file_api.client.post(
        "/v1/orders",
        json=_order(product.id, 3),
        headers={**bearer(customer), "Idempotency-Key": "concurrency-cancel-01"},
    )
    url = f"/v1/orders/{created.json()['id']}/cancel"

    responses = _race(
        lambda client: client.post(url, headers=bearer(customer)),
        lambda client: client.post(url, headers=bearer(admin)),
    )

    assert [response.status_code for response in responses] == [200, 200]
    assert {response.json()["status"] for response in responses} == {"canceled"}
    assert file_api.stock(product.id) == 4
