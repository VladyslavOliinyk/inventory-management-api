import logging

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)

redis_client = redis.from_url(settings.redis_url, decode_responses=True)

VALUATION_CACHE_KEY = "financial_valuation"
VALUATION_CACHE_TTL = 300  # секунд


async def invalidate_valuation_cache() -> None:
    try:
        await redis_client.delete(VALUATION_CACHE_KEY)
    except Exception as exc:
        logger.error("Redis: не удалось сбросить кэш оценки: %s", exc)
