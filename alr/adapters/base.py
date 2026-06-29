"""Plugin architecture. Every source is an adapter that yields RawListing.
Register with @adapter so the crawler can discover them by name from config.
Swapping the car domain for Zillow/eBay later means writing one more adapter,
not touching the pipeline."""
from __future__ import annotations

import abc
from typing import Iterable

import httpx

from ..config import HTTP_TIMEOUT, USER_AGENT
from ..schema import RawListing

REGISTRY: dict[str, type["BaseAdapter"]] = {}


def adapter(name: str):
    def deco(cls: type["BaseAdapter"]):
        cls.name = name
        REGISTRY[name] = cls
        return cls
    return deco


class BaseAdapter(abc.ABC):
    name: str = "base"

    def __init__(self) -> None:
        # Realistic browser headers - many sites WAF-block non-browser UAs with
        # a 403 on the first request. This is best-effort; sites behind a JS
        # challenge (Cloudflare/Incapsula) still need the Playwright adapter.
        self.client = httpx.Client(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={
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
            },
        )

    @abc.abstractmethod
    def fetch(self) -> Iterable[RawListing]:
        """Yield RawListing. Network failures should be caught here and logged,
        not raised, so one dead source never kills a crawl."""
        raise NotImplementedError

    def close(self) -> None:
        self.client.close()


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
