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

import asyncio
import re

from .base import BaseAdapter, adapter, BROWSER_LOCK
from ..schema import RawListing


def _money(s):
    if not s:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)", s)
    return float(m.group(1).replace(",", "")) if m else None


_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_CHALLENGE = re.compile(r"just a moment|checking your browser|cf-challenge|"
                        r"cloudflare|attention required|turnstile|captcha|"
                        r"verify you are human|access denied", re.I)


def _playwright_cards(url, sel, tag="swapalease"):
    """(card_text_dict, href) per result card. Tries playwright-stealth to clear a
    Cloudflare JS challenge. If an interactive challenge (Turnstile/hCaptcha) or a
    block is detected, reports the real reason and returns [] — never fabricates."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"[{tag}] playwright not installed "
              "(pip install playwright && playwright install chromium)")
        return []
    out = []
    try:
        with BROWSER_LOCK, sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="chromium", args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=_UA, locale="en-US",
                                      viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            try:                              # strip automation fingerprints
                from playwright_stealth import stealth_sync
                stealth_sync(page)
            except Exception:
                pass
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)       # give the JS challenge time to clear
            try:
                page.wait_for_selector(sel["card"], timeout=20000)
            except Exception:
                head = (page.title() or "") + " " + (page.inner_text("body")[:200] if page.query_selector("body") else "")
                if _CHALLENGE.search(head):
                    print(f"[{tag}] BLOCKED by anti-bot (interactive challenge): "
                          f"{head.strip()[:90]!r} -> emit 0. Needs proxy/stealth API (gated).")
                else:
                    print(f"[{tag}] no listing cards (selectors stale or 0 results): "
                          f"title={page.title()!r} -> emit 0")
                browser.close()
                return []
            for c in page.query_selector_all(sel["card"]):
                def txt(key):
                    el = c.query_selector(sel.get(key, "")) if sel.get(key) else None
                    return el.inner_text().strip() if el else None
                link = c.query_selector(sel.get("link", "a"))
                href = link.get_attribute("href") if link else None
                out.append(({k: txt(k) for k in ("title", "monthly", "months", "miles")}, href))
            browser.close()
    except Exception as e:
        print(f"[{tag}] playwright crawl failed (blocked/anti-bot?): {type(e).__name__} {str(e)[:100]}")
    return out


@adapter("swapalease")
class SwapaleaseAdapter(BaseAdapter):
    LIST_URL = "https://www.swapalease.com/lease/search.aspx"
    SEL = {"card": "div.listing-item, .vehicle-card, .searchResultItem",
           "title": ".vehicle-title, h2, .title", "monthly": ".monthly-payment, .payment, .price",
           "months": ".months-remaining, .term", "miles": ".miles-allowed, .mileage", "link": "a"}

    async def fetch(self) -> list[RawListing]:
        return await asyncio.to_thread(self._fetch_blocking)

    def _fetch_blocking(self) -> list[RawListing]:
        out: list[RawListing] = []
        for fields, href in _playwright_cards(self.LIST_URL, self.SEL):
            title = fields.get("title") or ""
            if not title:
                continue
            toks = re.sub(r"^\s*\d{4}\s*", "", title).split()
            sid = re.search(r"(\d{5,})", href or title)
            out.append(RawListing(
                source="swapalease",
                source_id=sid.group(1) if sid else title[:40],
                url=href, title=title,
                make=toks[0] if toks else None,
                model=" ".join(toks[1:3]) if len(toks) > 1 else None,
                monthly=_money(fields.get("monthly")),
                months_remaining=int(_money(fields.get("months")) or 0) or None,
                miles_per_year=int(_money(fields.get("miles")) or 0) or None,
            ))
        return out


@adapter("leasetrader")
class LeaseTraderAdapter(BaseAdapter):
    LIST_URL = "https://www.leasetrader.com/lease-deals"
    SEL = {"card": ".deal-card, .listing, .vehicle", "title": "h3, .title, h2",
           "monthly": ".price, .monthly, .payment", "months": ".term, .months",
           "miles": ".mileage, .miles", "link": "a"}

    async def fetch(self) -> list[RawListing]:
        return await asyncio.to_thread(self._fetch_blocking)

    def _fetch_blocking(self) -> list[RawListing]:
        out: list[RawListing] = []
        for fields, href in _playwright_cards(self.LIST_URL, self.SEL, tag="leasetrader"):
            title = fields.get("title") or ""
            if not title:
                continue
            toks = re.sub(r"^\s*\d{4}\s*", "", title).split()
            out.append(RawListing(
                source="leasetrader",
                source_id=re.sub(r"\W+", "", title)[:40],
                url=href, title=title,
                make=toks[0] if toks else None,
                model=" ".join(toks[1:3]) if len(toks) > 1 else None,
                monthly=_money(fields.get("monthly")),
                months_remaining=int(_money(fields.get("months")) or 0) or None,
            ))
        return out
