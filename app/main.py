from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

from app.database import Base, engine
from app.core.errors import register_exception_handlers
from app.core.http import RequestIDMiddleware
from app.orders.router import router as orders_router
from app.products.router import router as products_router
from app.users import model as user_model
from app.users.router import router as users_router

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield

app = FastAPI(
    title="ChocoMarket API",
    version="1.0.0",
    description="API da loja online ChocoMarket.",
    lifespan=lifespan,
)
app.add_middleware(RequestIDMiddleware)
register_exception_handlers(app)

app.include_router(users_router)
app.include_router(products_router)
app.include_router(orders_router)


@cache
def load_contract() -> dict[str, Any]:
    """Load the hand-written HTTP contract, the source of truth for the API."""

    with CONTRACT_PATH.open(encoding="utf-8") as contract_file:
        return yaml.safe_load(contract_file)


def contract_openapi() -> dict[str, Any]:
    """Serve ``openapi.yaml`` at ``/openapi.json`` and ``/docs``.

    The schema FastAPI would generate differs in names, error media types and
    documented status codes, so publishing it would contradict the contract.
    Contract tests check that the registered routes match this document.

    A relative server is listed first so "Try it out" in ``/docs`` calls the
    running instance instead of the contract's production URL.
    """

    contract = load_contract()
    return {
        **contract,
        "servers": [
            {"url": "/", "description": "Este servidor"},
            *contract.get("servers", []),
        ],
    }


app.openapi = contract_openapi


@app.get("/health", tags=["Health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
