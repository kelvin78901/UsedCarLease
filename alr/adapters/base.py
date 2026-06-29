"""Plugin architecture. Every source is an adapter that yields RawListing.
Register with @adapter so the crawler can discover them by name from config.
Swapping the car domain for Zillow/eBay later means writing one more adapter,
not touching the pipeline.

Async (P1): each adapter owns an httpx.AsyncClient and an asyncio.Semaphore that
are created lazily in `aopen()` *inside the running event loop* (never in
__init__ — a Semaphore/AsyncClient built outside the loop binds to the wrong
loop and blows up later). Network requests go through `aget_json`, which bounds
concurrency with the per-adapter semaphore and retries 429/5xx/transport errors
with exponential backoff (tenacity)."""
from __future__ import annotations

import abc
import asyncio

import httpx
from tenacity import (AsyncRetrying, retry_if_exception, stop_after_attempt,
                      wait_exponential)

from ..config import HTTP_TIMEOUT
from ..schema import RawListing

REGISTRY: dict[str, type["BaseAdapter"]] = {}

# Realistic browser headers - many sites WAF-block non-browser UAs with a 403 on
# the first request. Sites behind a JS challenge still need a Playwright adapter.
# Accept-Encoding stays "gzip, deflate" ON PURPOSE: httpx would otherwise
# advertise br/zstd and we have no brotli installed -> decode errors.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


def adapter(name: str):
    def deco(cls: type["BaseAdapter"]):
        cls.name = name
        REGISTRY[name] = cls
        return cls
    return deco


class BaseAdapter(abc.ABC):
    name: str = "base"
    concurrency: int = 4          # subclasses override from config
    max_retries: int = 3

    def __init__(self) -> None:
        # NOTHING loop-bound here: the client + semaphore are created in aopen()
        # so they attach to the loop that actually runs the crawl.
        self._client: httpx.AsyncClient | None = None
        self._sem: asyncio.Semaphore | None = None

    async def aopen(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=HTTP_TIMEOUT, follow_redirects=True, headers=_HEADERS)
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "BaseAdapter":
        await self.aopen()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aget_json(self, url: str, *, params=None) -> dict:
        """GET -> parsed JSON, bounded by the per-adapter semaphore and retried
        on 429/5xx/transport/timeout with exponential backoff. The semaphore is
        released while tenacity backs off, so retrying requests don't hog slots."""
        async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_retries),
                wait=wait_exponential(multiplier=0.5, max=8),
                retry=retry_if_exception(_is_retryable),
                reraise=True):
            with attempt:
                async with self._sem:
                    r = await self._client.get(url, params=params)
                    r.raise_for_status()
                    return r.json()
        return {}  # unreachable: reraise=True re-raises on exhaustion

    @abc.abstractmethod
    async def fetch(self) -> list[RawListing]:
        """Return RawListings. Network failures should be caught here and logged,
        not raised, so one dead source never kills a crawl."""
        raise NotImplementedError

    def fetch_sync(self) -> list[RawListing]:
        """Run fetch() to completion from synchronous code (scripts/probe.py)."""
        async def _run():
            async with self:
                return await self.fetch()
        return asyncio.run(_run())

    def close(self) -> None:  # legacy no-op; aclose() is the real teardown
        pass


def get_adapters(names: list[str]) -> list[BaseAdapter]:
    out = []
    for n in names:
        n = n.strip()
        cls = REGISTRY.get(n)
        if cls is None:
            print(f"[adapters] unknown adapter '{n}' (have: {list(REGISTRY)})")
            continue
        out.append(cls())
    return out
