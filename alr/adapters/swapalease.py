"""Swapalease + LeaseTrader adapters (Playwright).

Selectors reverse-engineered from the LIVE sites (2025). Both render results
client-side, but neither WAF-blocks a normal headless Chromium, so they are
treated as real sources (not placeholders). If a site later puts up an
interactive anti-bot challenge, the helpers detect it and emit 0 honestly.

Swapalease: per-make search pages /lease/{Make}/search.aspx list cards as
    div.listing-item > a[href="/lease/details/..salid=N"]
      span.listing-title     -> "2025 BMW i4"
      span.listing-location   -> "Los Angeles,CA"
    and the card text carries "$394/mo for 34 months".
LeaseTrader: /search-results is an Angular app; each card is div.for_grid with
    labeled lines: Lease Payment / Months Remaining / Down Payment / Location.
"""
from __future__ import annotations

import asyncio
import os
import re

from .base import BaseAdapter, adapter, BROWSER_LOCK, fetch_via_subprocess
from ..schema import RawListing

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_CHALLENGE = re.compile(r"just a moment|checking your browser|cf-challenge|"
                        r"cloudflare|attention required|turnstile|captcha|"
                        r"verify you are human|access denied", re.I)


# LeaseTrader throttles repeated hits -> a polite pre-load pause + exponential
# backoff on the load. The Playwright subprocess hard-timeout is the outer guard,
# so even a fully blocked load just emits 0 without stalling the crawl.
LT_DELAY = float(os.getenv("ALR_LT_DELAY", "2.0"))     # base backoff seconds
LT_RETRIES = int(os.getenv("ALR_LT_RETRIES", "3"))     # load attempts


def _money(s):
    if not s:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)", s)
    return float(m.group(1).replace(",", "")) if m else None


def _launch(p):
    return p.chromium.launch(headless=True, channel="chromium", args=[
        "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled"])


def _blocked(page, tag) -> bool:
    """True (and prints the real reason) only when an anti-bot wall is detected."""
    body = page.inner_text("body")[:200] if page.query_selector("body") else ""
    head = (page.title() or "") + " " + body
    if _CHALLENGE.search(head):
        print(f"[{tag}] BLOCKED by anti-bot (interactive challenge): "
              f"{head.strip()[:90]!r} -> emit 0. Needs proxy/stealth API (gated).")
        return True
    return False


@adapter("swapalease")
class SwapaleaseAdapter(BaseAdapter):
    BASE = "https://www.swapalease.com"
    # one search page per make (each lists ~20+ takeovers); the generic page only
    # shows ~10 featured. Override/extend with ALR_SWAP_MAKES.
    MAKES = [m.strip() for m in os.getenv(
        "ALR_SWAP_MAKES",
        "Toyota,Honda,BMW,Mercedes-Benz,Ford,Chevrolet,Audi,Lexus,Jeep,Subaru,"
        "Nissan,Hyundai,Kia,Volkswagen,Porsche,Tesla,Cadillac,GMC,Ram,Volvo,"
        "Acura,Infiniti,Mazda,Genesis,Land-Rover").split(",") if m.strip()]

    async def fetch(self) -> list[RawListing]:
        return await fetch_via_subprocess(self.name)

    def _fetch_blocking(self) -> list[RawListing]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("[swapalease] playwright not installed; skipping")
            return []
        out: list[RawListing] = []
        seen: set[str] = set()
        try:
            with BROWSER_LOCK, sync_playwright() as p:
                br = _launch(p)
                ctx = br.new_context(user_agent=_UA, locale="en-US",
                                     viewport={"width": 1366, "height": 900})
                page = ctx.new_page()
                for mk in self.MAKES:
                    url = f"{self.BASE}/lease/{mk}/search.aspx"
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2200)
                    except Exception as e:
                        print(f"[swapalease] {mk} nav failed: {type(e).__name__}")
                        continue
                    if not page.query_selector("div.listing-item"):
                        if _blocked(page, "swapalease"):
                            break
                        continue
                    for c in page.query_selector_all("div.listing-item"):
                        r = self._card(c)
                        if r and r.source_id not in seen:
                            seen.add(r.source_id)
                            out.append(r)
                br.close()
        except Exception as e:
            print(f"[swapalease] crawl failed: {type(e).__name__} {str(e)[:100]}")
        print(f"[swapalease] {len(out)} lease takeovers across {len(self.MAKES)} makes")
        return out

    def _card(self, c):
        t = c.query_selector("span.listing-title")
        title = t.inner_text().strip() if t else None
        if not title:
            return None
        a = c.query_selector("a")
        href = a.get_attribute("href") if a else None
        loc = c.query_selector("span.listing-location")
        loc = loc.inner_text().strip() if loc else ""
        sm = re.search(r",\s*([A-Z]{2})\b", loc)
        pm = re.search(r"\$([\d,]+)\s*/\s*mo(?:\s*for\s*(\d+)\s*month)?",
                       c.inner_text(), re.I)
        sid = re.search(r"salid=(\d+)", href or "")
        toks = re.sub(r"^\s*\d{4}\s*", "", title).split()
        return RawListing(
            source="swapalease",
            source_id=sid.group(1) if sid else re.sub(r"\W+", "", title)[:40],
            url=(self.BASE + href) if (href and href.startswith("/")) else href,
            title=title,
            make=toks[0] if toks else None,
            model=" ".join(toks[1:3]) if len(toks) > 1 else None,
            monthly=_money(pm.group(1)) if pm else None,
            months_remaining=int(pm.group(2)) if (pm and pm.group(2)) else None,
            state=sm.group(1) if sm else None,
        )


@adapter("leasetrader")
class LeaseTraderAdapter(BaseAdapter):
    LIST_URL = "https://www.leasetrader.com/search-results"

    async def fetch(self) -> list[RawListing]:
        return await fetch_via_subprocess(self.name)

    def _fetch_blocking(self) -> list[RawListing]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("[leasetrader] playwright not installed; skipping")
            return []
        out: list[RawListing] = []
        seen: set[str] = set()
        try:
            with BROWSER_LOCK, sync_playwright() as p:
                br = _launch(p)
                ctx = br.new_context(user_agent=_UA, locale="en-US",
                                     viewport={"width": 1366, "height": 900})
                page = ctx.new_page()
                # polite pre-load pause + exponential backoff: leasetrader.com
                # rate-limits bursts; back off rather than hammer (which is what got
                # us throttled). Normal case succeeds on attempt 1.
                loaded = False
                for attempt in range(LT_RETRIES):
                    page.wait_for_timeout(int(LT_DELAY * 1000 * (2 ** attempt)))  # 2s,4s,8s
                    try:
                        page.goto(self.LIST_URL, wait_until="domcontentloaded", timeout=35000)
                        page.wait_for_selector("div.for_grid", timeout=12000)
                        loaded = True
                        break
                    except Exception:
                        if _blocked(page, "leasetrader"):
                            br.close()
                            return []
                        print(f"[leasetrader] load attempt {attempt + 1}/{LT_RETRIES} "
                              f"empty (rate-limited?) -> backing off")
                if not loaded:
                    print(f"[leasetrader] no cards after {LT_RETRIES} tries -> emit 0")
                    br.close()
                    return []
                # the Angular list lazy-loads on scroll; pull a few batches
                for _ in range(8):
                    page.mouse.wheel(0, 25000)
                    page.wait_for_timeout(1200)
                for c in page.query_selector_all("div.for_grid"):
                    r = self._card(c)
                    if r and r.source_id not in seen:
                        seen.add(r.source_id)
                        out.append(r)
                br.close()
        except Exception as e:
            print(f"[leasetrader] crawl failed: {type(e).__name__} {str(e)[:100]}")
        print(f"[leasetrader] {len(out)} lease takeovers")
        return out

    def _card(self, c):
        txt = c.inner_text()
        if "$" not in txt:
            return None
        title = re.sub(r"\s*Lease\s*$", "", txt.strip().split("\n")[0], flags=re.I).strip()
        if not title:
            return None

        def after(label):
            m = re.search(rf"{label}\s*:?\s*\n?\s*\$?\s*([\d,\.]+)", txt, re.I)
            return m.group(1) if m else None
        months = after("Months Remaining")
        lm = re.search(r"Location\s*:?\s*\n?\s*([^\n]+)", txt, re.I)
        sm = re.search(r",\s*([A-Z]{2})\b", lm.group(1)) if lm else None
        a = c.query_selector("a")
        href = a.get_attribute("href") if a else None
        toks = re.sub(r"^\s*\d{4}\s*", "", title).split()
        return RawListing(
            source="leasetrader",
            source_id=re.sub(r"\W+", "", title)[:50],
            url=("https://www.leasetrader.com" + href) if (href and href.startswith("/")) else href,
            title=title,
            make=toks[0] if toks else None,
            model=" ".join(toks[1:3]) if len(toks) > 1 else None,
            monthly=_money(after("Lease Payment")),
            months_remaining=int(float(months)) if months else None,
            drive_off=_money(after("Down Payment")),
            state=sm.group(1) if sm else None,
        )
