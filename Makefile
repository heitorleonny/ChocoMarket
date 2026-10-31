.PHONY: dev test test-unit test-property test-integration test-contract test-concurrency format lint

dev:
	uvicorn app.main:app --reload

test:
	pytest

test-unit:
	pytest -m unit

test-property:
	pytest -m property

test-integration:
	pytest -m integration

test-contract:
	pytest -m contract

test-concurrency:
	pytest -m concurrency

format:
	ruff format .

lint:
	ruff check .
