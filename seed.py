"""
Создаёт начальных пользователей в БД.
Запуск: docker compose run --rm app python seed.py
"""
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.auth import hash_password
from app.config import settings
from app.models import User, UserRole

USERS = [
    {"email": "admin@example.com", "password": "admin123", "role": UserRole.admin},
    {"email": "storekeeper@example.com", "password": "store123", "role": UserRole.storekeeper},
]


async def seed() -> None:
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        for u in USERS:
            existing = await db.execute(select(User).where(User.email == u["email"]))
            if existing.scalar_one_or_none():
                print(f"Already exists: {u['email']}")
                continue

            db.add(User(
                email=u["email"],
                hashed_password=hash_password(u["password"]),
                role=u["role"],
            ))
            print(f"Created: {u['email']} ({u['role'].value})")

        await db.commit()

    await engine.dispose()


asyncio.run(seed())
