"""Swapalease + LeaseTrader adapters (Playwright).

Reality check (verified via inspection): both sites JS-render their search
results and WAF-block plain httpx (403). So these use Playwright, not httpx.

IMPORTANT - UNVERIFIED: unlike the Leasehackr adapter, these have NOT been run
against the live sites from here (sandbox can't reach them). The selectors below
are best-effort placeholders. On your machine:

    pip install playwright && playwright install chromium
    python scripts/probe.py swapalease

then open the saved page, inspect the real card/field selectors, and fix the SEL
dict. These sites also gate seller contact behind registration and may present a
Cloudflare/Incapsula challenge that needs a real (non-headless / stealth) browser.
Treat Leasehackr as the primary source; these are bonus coverage.
"""
from __future__ import annotations

import re
from typing import Iterable

from .base import BaseAdapter, adapter
from ..schema import RawListing


def _money(s):
    if not s:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)", s)
    return float(m.group(1).replace(",", "")) if m else None


def _playwright_cards(url, sel, user_agent="AutoLeaseRank/0.4"):
    """Yield (card_text_dict, href) for each result card. Returns [] if Playwright
    is missing or the site blocks us."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[swapalease] playwright not installed; "
              "pip install playwright && playwright install chromium")
        return []
    out = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"))
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(2500)  # let lazy results settle
            for c in page.query_selector_all(sel["card"]):
                def txt(key):
                    el = c.query_selector(sel.get(key, "")) if sel.get(key) else None
                    return el.inner_text().strip() if el else None
                link = c.query_selector(sel.get("link", "a"))
                href = link.get_attribute("href") if link else None
                out.append(({k: txt(k) for k in ("title", "monthly", "months", "miles")}, href))
            browser.close()
    except Exception as e:
        print(f"[swapalease] playwright crawl failed (blocked/anti-bot?): {e}")
    return out


@adapter("swapalease")
class SwapaleaseAdapter(BaseAdapter):
    LIST_URL = "https://www.swapalease.com/lease/search.aspx"
    SEL = {"card": "div.listing-item, .vehicle-card, .searchResultItem",
           "title": ".vehicle-title, h2, .title", "monthly": ".monthly-payment, .payment, .price",
           "months": ".months-remaining, .term", "miles": ".miles-allowed, .mileage", "link": "a"}

    def fetch(self) -> Iterable[RawListing]:
        for fields, href in _playwright_cards(self.LIST_URL, self.SEL):
            title = fields.get("title") or ""
            if not title:
                continue
            toks = re.sub(r"^\s*\d{4}\s*", "", title).split()
            sid = re.search(r"(\d{5,})", href or title)
            yield RawListing(
                source="swapalease",
                source_id=sid.group(1) if sid else title[:40],
                url=href, title=title,
                make=toks[0] if toks else None,
                model=" ".join(toks[1:3]) if len(toks) > 1 else None,
                monthly=_money(fields.get("monthly")),
                months_remaining=int(_money(fields.get("months")) or 0) or None,
                miles_per_year=int(_money(fields.get("miles")) or 0) or None,
            )


@adapter("leasetrader")
class LeaseTraderAdapter(BaseAdapter):
    LIST_URL = "https://www.leasetrader.com/lease-deals"
    SEL = {"card": ".deal-card, .listing, .vehicle", "title": "h3, .title, h2",
           "monthly": ".price, .monthly, .payment", "months": ".term, .months",
           "miles": ".mileage, .miles", "link": "a"}

    def fetch(self) -> Iterable[RawListing]:
        for fields, href in _playwright_cards(self.LIST_URL, self.SEL):
            title = fields.get("title") or ""
            if not title:
                continue
            toks = re.sub(r"^\s*\d{4}\s*", "", title).split()
            yield RawListing(
                source="leasetrader",
                source_id=re.sub(r"\W+", "", title)[:40],
                url=href, title=title,
                make=toks[0] if toks else None,
                model=" ".join(toks[1:3]) if len(toks) > 1 else None,
                monthly=_money(fields.get("monthly")),
                months_remaining=int(_money(fields.get("months")) or 0) or None,
            )
