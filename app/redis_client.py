import json
import logging

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)

redis_client = redis.from_url(settings.redis_url, decode_responses=True)

VALUATION_CACHE_KEY = "financial_valuation"
VALUATION_CACHE_TTL = 300  # seconds
IDEMPOTENCY_TTL = 86400  # 24 hours — for stored results
IDEMPOTENCY_PROCESSING_TTL = 60  # seconds — for "in-flight" marker


async def invalidate_valuation_cache() -> None:
    try:
        await redis_client.delete(VALUATION_CACHE_KEY)
    except Exception as exc:
        logger.error("Redis: failed to invalidate valuation cache: %s", exc)


def _idempotency_redis_key(user_id: int, path: str, key: str) -> str:
    return f"idempotency:{user_id}:{path}:{key}"


async def idempotency_get(user_id: int, path: str, key: str) -> dict | None:
    """
    Returns cached result {"status_code": int, "body": ...} if exists,
    or {"status_code": 0} if still processing,
    or None if key is not set (first request).
    Returns None also on Redis errors so the request proceeds normally.
    """
    try:
        redis_key = _idempotency_redis_key(user_id, path, key)
        value = await redis_client.get(redis_key)
        if value is None:
            return None
        return json.loads(value)
    except Exception as exc:
        logger.error("Redis: idempotency_get failed: %s", exc)
        return None


async def idempotency_set_processing(user_id: int, path: str, key: str) -> bool:
    """
    Atomically sets key to "processing" via SET NX.
    Returns True if we acquired the key (first request), False if already exists.
    Returns True on Redis errors so the request proceeds normally.
    """
    try:
        redis_key = _idempotency_redis_key(user_id, path, key)
        acquired = await redis_client.set(
            redis_key,
            json.dumps({"status_code": 0}),
            nx=True,
            ex=IDEMPOTENCY_PROCESSING_TTL,
        )
        return bool(acquired)
    except Exception as exc:
        logger.error("Redis: idempotency_set_processing failed: %s", exc)
        return True


async def idempotency_set_result(user_id: int, path: str, key: str, status_code: int, body: object) -> None:
    """Saves the final response. Called after successful commit."""
    try:
        redis_key = _idempotency_redis_key(user_id, path, key)
        await redis_client.set(
            redis_key,
            json.dumps({"status_code": status_code, "body": body}),
            ex=IDEMPOTENCY_TTL,
        )
    except Exception as exc:
        logger.error("Redis: idempotency_set_result failed: %s", exc)


async def idempotency_delete(user_id: int, path: str, key: str) -> None:
    """Removes the key on error so the client can retry after fixing the issue."""
    try:
        redis_key = _idempotency_redis_key(user_id, path, key)
        await redis_client.delete(redis_key)
    except Exception as exc:
        logger.error("Redis: idempotency_delete failed: %s", exc)
