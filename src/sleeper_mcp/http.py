"""HTTP layer: one shared client with rate limiting and a short-lived TTL cache.

Sleeper asks clients to stay under 1000 calls/minute or risk an IP block. We cap
well below that (default 300/min, override with SLEEPER_MAX_CALLS_PER_MIN) and
cache responses in memory so repeated tool calls in one conversation are cheap.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

API_BASE = "https://api.sleeper.app/v1"
# A few undocumented endpoints (schedule) only live on this host.
ALT_API_BASE = "https://api.sleeper.com"

DEFAULT_MAX_CALLS_PER_MIN = 300


class SleeperAPIError(ToolError):
    """A problem worth showing the model as-is (bad ID, unknown player, API down)."""


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` starts per `period` seconds."""

    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._starts: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._starts and now - self._starts[0] >= self.period:
                    self._starts.popleft()
                if len(self._starts) < self.max_calls:
                    self._starts.append(now)
                    return
                await asyncio.sleep(self.period - (now - self._starts[0]) + 0.01)


class TTLCache:
    """Tiny in-memory cache with per-entry expiry."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> tuple[bool, Any]:
        hit = self._data.get(key)
        if hit is None:
            return False, None
        expires, value = hit
        if time.monotonic() >= expires:
            self._data.pop(key, None)
            return False, None
        return True, value

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = (time.monotonic() + ttl, value)

    def clear(self) -> None:
        self._data.clear()


class SleeperHTTP:
    def __init__(self, max_calls_per_min: int | None = None, timeout: float = 30.0):
        limit = max_calls_per_min or int(
            os.environ.get("SLEEPER_MAX_CALLS_PER_MIN", DEFAULT_MAX_CALLS_PER_MIN)
        )
        self.limiter = RateLimiter(min(limit, 900))
        self.cache = TTLCache()
        self._client: httpx.AsyncClient | None = None
        self._timeout = timeout
        self._inflight: dict[str, asyncio.Future] = {}
        self.calls_made = 0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": "sleeper-mcp/0.1 (read-only MCP server)"},
                follow_redirects=True,
            )
        return self._client

    async def get(
        self,
        path: str,
        *,
        ttl: float = 60.0,
        params: dict[str, Any] | None = None,
        base: str = API_BASE,
    ) -> Any:
        """GET a JSON endpoint. `ttl=0` disables caching for this call."""
        url = f"{base}{path}"
        key = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")

        if ttl > 0:
            found, value = self.cache.get(key)
            if found:
                return value
            # Collapse concurrent identical requests into one network call.
            pending = self._inflight.get(key)
            if pending is not None:
                return await asyncio.shield(pending)

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if ttl > 0:
            self._inflight[key] = fut
        try:
            value = await self._fetch(url, params)
            if ttl > 0:
                self.cache.set(key, value, ttl)
            fut.set_result(value)
            return value
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so asyncio doesn't warn when nobody else awaits
            raise
        finally:
            self._inflight.pop(key, None)

    async def _fetch(self, url: str, params: dict[str, Any] | None) -> Any:
        for attempt in range(3):
            await self.limiter.acquire()
            self.calls_made += 1
            try:
                resp = await self._http().get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise SleeperAPIError(f"Network error calling Sleeper: {exc}") from exc
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 2:
                    raise SleeperAPIError(f"Sleeper returned HTTP {resp.status_code} for {url}")
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise SleeperAPIError(f"Sleeper returned HTTP {resp.status_code} for {url}")
            if not resp.content:
                return None
            return resp.json()
        raise SleeperAPIError(f"Failed to fetch {url}")

    async def get_raw(self, url: str) -> bytes:
        """Uncached GET returning raw bytes (used for the big player dump)."""
        await self.limiter.acquire()
        self.calls_made += 1
        resp = await self._http().get(url, timeout=120.0)
        resp.raise_for_status()
        return resp.content

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
