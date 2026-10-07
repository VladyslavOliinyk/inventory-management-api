import asyncio
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import hash_password
from app.database import AsyncSessionLocal, engine
from app.main import app
from app.models import Base, User, UserRole

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="session", autouse=True)
def ensure_tables():
    async def _run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_run())


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    await engine.dispose()
    async with engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE stock_movements, products, warehouses, users RESTART IDENTITY CASCADE"
        ))


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def admin_headers(client: AsyncClient):
    async with AsyncSessionLocal() as db:
        db.add(User(
            email="admin@test.com",
            hashed_password=hash_password("admin123"),
            role=UserRole.admin,
        ))
        await db.commit()

    resp = await client.post("/auth/login", json={"email": "admin@test.com", "password": "admin123"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
