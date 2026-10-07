from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="Inventory Management API", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}
