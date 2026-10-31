from fastapi import FastAPI

from app.users.router import router as users_router


app = FastAPI(
    title="ChocoMarket API",
    version="1.0.0",
    description="API da loja online ChocoMarket.",
)

app.include_router(users_router)


@app.get("health", tags=["Health"])
def health() -> dict[str, str]:
    return {"status": "ok"}