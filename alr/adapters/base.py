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
import threading

import httpx
from tenacity import (AsyncRetrying, retry_if_exception, stop_after_attempt,
                      wait_exponential)

from ..config import HTTP_TIMEOUT
from ..schema import RawListing

REGISTRY: dict[str, type["BaseAdapter"]] = {}

# Serialize headless-browser launches across Playwright adapters (cars / swapalease
# / leasetrader). Several chromium processes launching at once in a container is
# fragile (crashes). The adapters still run under the same asyncio.gather and in
# to_thread worker threads; only the browser work itself queues on this lock.
BROWSER_LOCK = threading.Lock()

# Playwright adapters run their scrape in an isolated subprocess (one browser
# lifecycle per process — reliable in-container), serialized so only one browser
# runs at a time.
import json as _json          # noqa: E402
import os as _os               # noqa: E402
import subprocess as _subprocess  # noqa: E402
import sys as _sys             # noqa: E402
import tempfile as _tempfile   # noqa: E402
_PW_SUBPROC_LOCK = asyncio.Lock()


def _run_pw_blocking(name: str, timeout: float):
    """Blocking: run the runner in its own process, output to a temp file, return
    its bytes (or None on timeout). subprocess.run reaps via os.waitpid (asyncio's
    child watcher hangs here), and a FILE (not a pipe) avoids the EOF-hang from a
    lingering chromium child holding the stdout fd."""
    fd, path = _tempfile.mkstemp(prefix=f"pw_{name}_", suffix=".json")
    _os.close(fd)
    try:
        with open(path, "wb") as outf:
            _subprocess.run([_sys.executable, "-m", "alr.adapters._pw_runner", name],
                            stdout=outf, timeout=timeout)   # stderr inherits -> logs
        with open(path, "rb") as f:
            return f.read()
    except _subprocess.TimeoutExpired:
        return None
    finally:
        try:
            _os.unlink(path)
        except OSError:
            pass


async def fetch_via_subprocess(name: str, timeout: float = 200.0) -> list[RawListing]:
    """Run a Playwright adapter's scrape in an isolated subprocess (one browser
    lifecycle per process — reliable in-container), serialized so only one runs.
    The hard timeout means a slow/blocked site emits 0 instead of hanging the crawl."""
    async with _PW_SUBPROC_LOCK:
        out = await asyncio.to_thread(_run_pw_blocking, name, timeout)
    if out is None:
        print(f"[{name}] browser subprocess timed out (>{timeout:.0f}s) -> emit 0")
        return []
    try:
        data = _json.loads(out.decode() or "[]")
    except Exception as e:
        print(f"[{name}] browser subprocess output unparseable: {e}")
        return []
    return [RawListing.model_validate(d) for d in data]

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
    request_delay: float = 0.0    # politeness sleep per request (held in the slot)

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
                    if self.request_delay:
                        await asyncio.sleep(self.request_delay)
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
