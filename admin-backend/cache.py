"""Async TTL cache with single-flight + graceful-stale degradation.

Process-local — admin-backend is single-process so no Redis is needed. Pattern
(read-through cache with per-key lock + invalidate helpers) observed in
icehunter db.go:14-49 (no LICENSE => clean-room; re-implemented from scratch
in Python with asyncio.Lock).

Public API:
  @ttl_cache("call_name", ttl=30, max_stale=60)
  async def fn(...): ...

  cache.invalidate("call_name", *args)
  cache.invalidate_prefix("call_name")
  cache.clear()                       # full drop (tests only)

Cache key is (call_name, *args). Caller is responsible for keeping args
hashable (str, int, bool, frozenset). For per-player data the caller MUST
include account_id in the wrapped fn's args so it lands in the key — no
cross-account leakage.

Return-shape contract: when the wrapped coroutine raises AND a cached value
exists AND now < cached_at + ttl + max_stale, return the cached value with
`stale=True` merged in (dicts only). If the cached value is not a dict,
return it unchanged. Past max_stale, the original exception is re-raised.
"""
import asyncio
import time
from functools import wraps

_store: dict[tuple, dict] = {}        # key -> {"value": ..., "fetched_at": float}
_locks: dict[tuple, asyncio.Lock] = {}


def _lock_for(key: tuple) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def ttl_cache(call_name: str, ttl: float = 30.0, max_stale: float = 60.0):
    """Decorator. Wraps an async fn with read-through caching + single-flight."""
    def deco(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            # kwargs are intentionally NOT part of the key — callers must pass
            # everything that varies result by positional arg. This keeps keys
            # hashable without sorting + canonicalising kwargs every call.
            key = (call_name, *args)
            now = time.monotonic()
            cached = _store.get(key)
            if cached is not None and now - cached["fetched_at"] < ttl:
                return cached["value"]

            async with _lock_for(key):
                # Re-check under the lock; another coroutine may have refilled.
                now = time.monotonic()
                cached = _store.get(key)
                if cached is not None and now - cached["fetched_at"] < ttl:
                    return cached["value"]
                try:
                    value = await fn(*args, **kwargs)
                except Exception:
                    if cached is not None and now - cached["fetched_at"] < ttl + max_stale:
                        value = cached["value"]
                        if isinstance(value, dict):
                            return {**value, "stale": True}
                        return value
                    raise
                _store[key] = {"value": value, "fetched_at": now}
                return value

        return wrapper
    return deco


def invalidate(call_name: str, *args):
    """Drop a single cache key. Safe to call when key isn't present."""
    _store.pop((call_name, *args), None)


def invalidate_prefix(call_name: str):
    """Drop every key for a given call_name (mirrors icehunter
    invalidateAllJourneyCache). Used after bulk-mutate operations."""
    stale = [k for k in _store if k and k[0] == call_name]
    for k in stale:
        _store.pop(k, None)


def clear():
    """Drop everything. Tests only — production callers should use the
    targeted invalidate helpers."""
    _store.clear()
    _locks.clear()
