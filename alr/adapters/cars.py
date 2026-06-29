"""Two adapters:

cars  - Cars.com renders inventory client-side, so static httpx won't see the
        listings. This uses Playwright. It's optional: install browsers with
        `playwright install chromium`. Kept lazy-imported so the rest of the
        system runs without Playwright present.

seed  - Not a scraper. Wraps the deterministic seed generator behind the same
        adapter interface so `crawl(["seed"])` exercises the whole pipeline with
        zero network. This is what runs in docker/CI by default.
"""
from __future__ import annotations

import asyncio

from .base import BaseAdapter, adapter
from ..schema import RawListing
from ..seed import generate as _seed_generate


@adapter("seed")
class SeedAdapter(BaseAdapter):
    async def fetch(self) -> list[RawListing]:
        return list(_seed_generate())


@adapter("cars")
class CarsAdapter(BaseAdapter):
    LIST_URL = ("https://www.cars.com/shopping/results/"
                "?stock_type=used&maximum_distance=all")

    async def fetch(self) -> list[RawListing]:
        # Playwright's sync API raises if called inside a running event loop, so
        # the blocking crawl runs in a worker thread (no loop there).
        return await asyncio.to_thread(self._fetch_blocking)

    def _fetch_blocking(self) -> list[RawListing]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("[cars] playwright not installed; skipping. "
                  "pip install playwright && playwright install chromium")
            return []
        out: list[RawListing] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent="AutoLeaseRank/0.4")
                page.goto(self.LIST_URL, wait_until="networkidle", timeout=30000)
                for c in page.query_selector_all("div.vehicle-card"):
                    title_el = c.query_selector("h2.title")
                    price_el = c.query_selector("span.primary-price")
                    title = title_el.inner_text().strip() if title_el else ""
                    if not title:
                        continue
                    toks = title.split()
                    out.append(RawListing(
                        source="cars",
                        source_id=(c.get_attribute("data-listing-id") or title)[:40],
                        title=title,
                        make=toks[1] if len(toks) > 1 else None,
                        model=" ".join(toks[2:4]) if len(toks) > 2 else None,
                        raw={"price_text": price_el.inner_text() if price_el else None},
                    ))
                browser.close()
        except Exception as e:
            print(f"[cars] playwright crawl failed: {e}")
        return out
