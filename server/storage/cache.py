"""Best-effort side-cache implementations.

SQLite is always authoritative.  Every Redis operation is guarded so a missing
package or an unavailable server only reduces performance, never availability.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol
import asyncio
import json
import logging
import time


logger = logging.getLogger(__name__)


class AsyncCache(Protocol):
    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_s: int | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> None: ...

    async def close(self) -> None: ...


class MemoryTTLCache:
    """Small process-local fallback with lazy expiry."""

    def __init__(self, default_ttl_s: int = 86_400, max_entries: int = 2_000):
        self.default_ttl_s = default_ttl_s
        self.max_entries = max_entries
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        now = time.monotonic()
        async with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                self._items.pop(key, None)
                return None
            return deepcopy(value)

    async def set(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        ttl = self.default_ttl_s if ttl_s is None else max(1, ttl_s)
        async with self._lock:
            now = time.monotonic()
            if len(self._items) >= self.max_entries:
                expired = [name for name, (end, _) in self._items.items() if end <= now]
                for name in expired:
                    self._items.pop(name, None)
                if len(self._items) >= self.max_entries:
                    oldest = min(self._items, key=lambda name: self._items[name][0])
                    self._items.pop(oldest, None)
            self._items[key] = (now + ttl, deepcopy(value))

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._items.pop(key, None)

    async def delete_prefix(self, prefix: str) -> None:
        async with self._lock:
            for key in [name for name in self._items if name.startswith(prefix)]:
                self._items.pop(key, None)

    async def close(self) -> None:
        async with self._lock:
            self._items.clear()


class RedisJSONCache:
    """Thin adapter around ``redis.asyncio``; constructed only when installed."""

    def __init__(self, client: Any, default_ttl_s: int = 86_400):
        self._client = client
        self.default_ttl_s = default_ttl_s

    async def get(self, key: str) -> Any | None:
        value = await self._client.get(key)
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return json.loads(value)

    async def set(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        await self._client.set(key, encoded, ex=ttl_s or self.default_ttl_s)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def delete_prefix(self, prefix: str) -> None:
        batch: list[Any] = []
        async for key in self._client.scan_iter(match=f"{prefix}*", count=100):
            batch.append(key)
            if len(batch) >= 100:
                await self._client.delete(*batch)
                batch.clear()
        if batch:
            await self._client.delete(*batch)

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


class SideCache:
    """Redis-first cache with an always-available in-memory fallback."""

    def __init__(self, fallback: MemoryTTLCache, primary: AsyncCache | None = None):
        self.fallback = fallback
        self.primary = primary
        self._primary_warning_emitted = False

    def _warn(self, exc: Exception) -> None:
        if not self._primary_warning_emitted:
            logger.warning("Redis side cache unavailable; using memory cache: %s", exc)
            self._primary_warning_emitted = True

    async def get(self, key: str) -> Any | None:
        if self.primary is not None:
            try:
                value = await self.primary.get(key)
                if value is not None:
                    await self.fallback.set(key, value)
                    return value
            except Exception as exc:  # Redis must never affect correctness
                self._warn(exc)
        return await self.fallback.get(key)

    async def set(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        await self.fallback.set(key, value, ttl_s)
        if self.primary is not None:
            try:
                await self.primary.set(key, value, ttl_s)
            except Exception as exc:
                self._warn(exc)

    async def delete(self, key: str) -> None:
        await self.fallback.delete(key)
        if self.primary is not None:
            try:
                await self.primary.delete(key)
            except Exception as exc:
                self._warn(exc)

    async def delete_prefix(self, prefix: str) -> None:
        await self.fallback.delete_prefix(prefix)
        if self.primary is not None:
            try:
                await self.primary.delete_prefix(prefix)
            except Exception as exc:
                self._warn(exc)

    async def close(self) -> None:
        await self.fallback.close()
        if self.primary is not None:
            try:
                await self.primary.close()
            except Exception as exc:
                self._warn(exc)


def build_side_cache(redis_url: str | None, ttl_s: int = 86_400) -> SideCache:
    memory = MemoryTTLCache(default_ttl_s=ttl_s)
    if not redis_url:
        return SideCache(memory)
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        return SideCache(memory, RedisJSONCache(client, default_ttl_s=ttl_s))
    except Exception as exc:  # includes optional dependency not installed
        logger.warning("Redis cache could not be configured; using memory cache: %s", exc)
        return SideCache(memory)


__all__ = ["AsyncCache", "MemoryTTLCache", "SideCache", "build_side_cache"]
