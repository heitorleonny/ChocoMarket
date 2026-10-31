.PHONY: dev test format lint

dev:
	uvicorn app.main:app --reload

test:
	pytest

format:
	ruff format .

lint:
	ruff check .
