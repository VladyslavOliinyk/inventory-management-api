from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import engine
from app.redis_client import redis_client
from app.routers import auth, movements, products, warehouses


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()
    await redis_client.aclose()


app = FastAPI(title="Inventory Management API", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(warehouses.router)
app.include_router(products.router)
app.include_router(movements.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
