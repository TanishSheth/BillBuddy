"""Optional Redis helpers with safe in-process fallbacks."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from .config import REDIS_URL

logger = logging.getLogger(__name__)

_redis = None
_redis_failed = False
_memory_rate: Dict[str, List[float]] = {}
_memory_rate_lock = threading.Lock()
_pubsub_thread = None
_local_handlers: Dict[str, List[Callable[[str], None]]] = {}
_local_handlers_lock = threading.Lock()


def get_redis():
    """Return a Redis client or None when unavailable."""
    global _redis, _redis_failed
    if _redis_failed or not REDIS_URL:
        return None
    if _redis is not None:
        return _redis
    try:
        import redis

        client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=1.5)
        client.ping()
        _redis = client
        logger.info("Connected to Redis at %s", REDIS_URL.split("@")[-1])
        return _redis
    except Exception as err:
        _redis_failed = True
        logger.warning("Redis unavailable (%s); using in-process fallbacks", err)
        return None


def rate_limit_hit(key: str, limit: int, window_seconds: int) -> bool:
    """Return True when the caller should be rejected (over limit)."""
    client = get_redis()
    now = time.time()
    if client is not None:
        try:
            pipe = client.pipeline()
            redis_key = f"rl:{key}"
            pipe.zremrangebyscore(redis_key, 0, now - window_seconds)
            pipe.zadd(redis_key, {str(now): now})
            pipe.zcard(redis_key)
            pipe.expire(redis_key, window_seconds + 1)
            _removed, _added, count, _exp = pipe.execute()
            return int(count) > limit
        except Exception as err:
            logger.warning("Redis rate limit failed; falling back to memory: %s", err)

    with _memory_rate_lock:
        bucket = _memory_rate.setdefault(key, [])
        bucket[:] = [t for t in bucket if now - t < window_seconds]
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        return False


def publish(channel: str, payload: dict) -> None:
    body = json.dumps(payload)
    client = get_redis()
    if client is not None:
        try:
            client.publish(channel, body)
            return
        except Exception as err:
            logger.warning("Redis publish failed: %s", err)
    _dispatch_local(channel, body)


def subscribe(channel: str, handler: Callable[[str], None]) -> None:
    with _local_handlers_lock:
        _local_handlers.setdefault(channel, []).append(handler)
    client = get_redis()
    if client is None:
        return
    global _pubsub_thread
    if _pubsub_thread and _pubsub_thread.is_alive():
        return

    def _run() -> None:
        while True:
            try:
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.psubscribe("group-events:*")
                for message in pubsub.listen():
                    if message is None or message.get("type") not in {"message", "pmessage"}:
                        continue
                    ch = message.get("channel") or message.get("pattern")
                    data = message.get("data")
                    if isinstance(ch, bytes):
                        ch = ch.decode("utf-8", errors="ignore")
                    if isinstance(data, bytes):
                        data = data.decode("utf-8", errors="ignore")
                    if not isinstance(ch, str) or not isinstance(data, str):
                        continue
                    _dispatch_local(ch, data)
            except Exception as err:
                logger.warning("Redis pubsub loop error: %s", err)
                time.sleep(2)

    _pubsub_thread = threading.Thread(target=_run, name="redis-pubsub", daemon=True)
    _pubsub_thread.start()


def _dispatch_local(channel: str, body: str) -> None:
    with _local_handlers_lock:
        handlers = list(_local_handlers.get(channel, []))
        # Also match prefix subscribers used for group-events:*
        for key, values in _local_handlers.items():
            if key.endswith(":*") and channel.startswith(key[:-1]):
                handlers.extend(values)
    for handler in handlers:
        try:
            handler(body)
        except Exception:
            logger.exception("Local pubsub handler failed for %s", channel)
