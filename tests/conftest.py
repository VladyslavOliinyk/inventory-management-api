import asyncio
import sys

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import hash_password
from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Base, User, UserRole
import app.redis_client as redis_module
import app.routers.warehouses as warehouses_module

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_TEST_DB = "inventory_test"
_base_url = settings.database_url.rsplit("/", 1)[0]
_test_db_url = f"{_base_url}/{_TEST_DB}"
_admin_url = f"{_base_url}/postgres"

test_engine = create_async_engine(_test_db_url, echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)
test_redis = aioredis.from_url(settings.redis_url, db=1, decode_responses=True)


async def _test_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    async def _run():
        admin_engine = create_async_engine(_admin_url, isolation_level="AUTOCOMMIT")
        async with admin_engine.connect() as conn:
            exists = await conn.scalar(
                text(f"SELECT 1 FROM pg_database WHERE datname = '{_TEST_DB}'")
            )
            if not exists:
                await conn.execute(text(f"CREATE DATABASE {_TEST_DB}"))
        await admin_engine.dispose()

        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await test_engine.dispose()

    asyncio.run(_run())

    app.dependency_overrides[get_db] = _test_get_db
    redis_module.redis_client = test_redis
    warehouses_module.redis_client = test_redis


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    await test_engine.dispose()
    test_redis.connection_pool.reset()
    async with test_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE stock_movements, products, warehouses, users RESTART IDENTITY CASCADE"
        ))
    await test_redis.flushdb()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def admin_headers(client: AsyncClient):
    async with TestSessionLocal() as db:
        db.add(User(
            email="admin@test.com",
            hashed_password=hash_password("admin123"),
            role=UserRole.admin,
        ))
        await db.commit()

    resp = await client.post("/auth/login", json={"email": "admin@test.com", "password": "admin123"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
