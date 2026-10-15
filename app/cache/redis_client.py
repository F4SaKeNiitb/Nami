import logging

from redis.asyncio import Redis

log = logging.getLogger(__name__)


def create_redis(url: str | None) -> Redis | None:
    """Binary-safe client (vectors are raw bytes). Returns None when Redis isn't configured."""
    if not url:
        log.info("NAMI_REDIS_URL not set; caching disabled")
        return None
    return Redis.from_url(url, decode_responses=False, socket_timeout=2, socket_connect_timeout=2)
