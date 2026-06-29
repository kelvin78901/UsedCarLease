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
import html
import json
import os
import re

from .base import BaseAdapter, adapter, BROWSER_LOCK, fetch_via_subprocess
from .marketcheck import amortized_monthly, TERM
from ..schema import RawListing
from ..seed import generate as _seed_generate

CARS_ZIP = os.getenv("ALR_CARS_ZIP", "90001")
# multi-zip sweep (e.g. east-coast metros); defaults to the single CARS_ZIP.
CARS_ZIPS = [z.strip() for z in os.getenv("ALR_CARS_ZIPS", CARS_ZIP).split(",") if z.strip()]
CARS_PAGES = int(os.getenv("ALR_CARS_PAGES", "5"))
CARS_STOCK = os.getenv("ALR_CARS_STOCK", "used")       # used | cpo | new
CARS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# the SRP ships its data in this embedded JSON blob; far more stable than the
# server-driven-UI DOM (whose card classes change).
_SRP_SCRIPT = "script#CarsWeb\\.SearchController\\.index"


@adapter("seed")
class SeedAdapter(BaseAdapter):
    async def fetch(self) -> list[RawListing]:
        return list(_seed_generate())


@adapter("cars")
class CarsAdapter(BaseAdapter):
    """Cars.com used-car inventory via Playwright. Parses the page's embedded
    `srp_results` JSON (vin/price/msrp/mileage/body/drivetrain/cpo) rather than
    the DOM, waits for the listing cards, and paginates. CPO comes from the
    payload's `cpoIndicator` / `banners.cpo`."""

    async def fetch(self) -> list[RawListing]:
        return await fetch_via_subprocess(self.name)

    def _fetch_blocking(self) -> list[RawListing]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("[cars] playwright not installed; skipping "
                  "(pip install playwright && playwright install chromium)")
            return []
        out: list[RawListing] = []
        try:
            with BROWSER_LOCK, sync_playwright() as p:
                # channel="chromium" uses the full chrome binary; the default
                # headless_shell segfaults in some containers.
                browser = p.chromium.launch(headless=True, channel="chromium", args=[
                    "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled"])
                ctx = browser.new_context(user_agent=CARS_UA, locale="en-US",
                                          viewport={"width": 1366, "height": 900})
                page = ctx.new_page()
                for z in CARS_ZIPS:
                    for pno in range(1, CARS_PAGES + 1):
                        got = self._scrape_page(page, pno, z)
                        if got is None:           # blocked / page error
                            break
                        out.extend(got)
                        if len(got) == 0:
                            break                 # past the last page for this zip
                browser.close()
        except Exception as e:
            print(f"[cars] playwright crawl failed: {e}")
        cpo = sum(1 for r in out if r.raw.get("cpo"))
        print(f"[cars] {len(out)} used listings ({cpo} CPO) from {CARS_STOCK} "
              f"near {','.join(CARS_ZIPS)}, {CARS_PAGES} page(s) each")
        return out

    def _scrape_page(self, page, pno: int, zip_code: str = CARS_ZIP):
        url = (f"https://www.cars.com/shopping/results/?stock_type={CARS_STOCK}"
               f"&maximum_distance=all&zip={zip_code}&page_size=20&page={pno}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("[data-listing-id]", timeout=30000)
        except Exception as e:
            print(f"[cars] page {pno} blocked/empty: {type(e).__name__} {str(e)[:80]}")
            return None
        try:
            raw = page.eval_on_selector(_SRP_SCRIPT, "e=>e.textContent")
            results = json.loads(raw).get("srp_results", {}).get("results", [])
        except Exception as e:
            print(f"[cars] page {pno} JSON parse failed: {e}")
            return None
        # dealer/city/state aren't in the payload -> read them from the card text
        dom = {c["id"]: c["txt"] for c in page.eval_on_selector_all(
            "[data-listing-id]", "els=>els.map(e=>({id:e.getAttribute('data-listing-id'),txt:e.innerText}))")}
        rows = [self._to_raw(r, dom) for r in results]
        return [r for r in rows if r]

    @staticmethod
    def _payload(r: dict) -> dict:
        pp = {}
        for iv in (r.get("on_view_interactions") or []):
            pl = iv.get("payload")
            if pl:
                try:
                    pp.update(json.loads(html.unescape(pl)))
                except Exception:
                    pass
        return pp

    @staticmethod
    def _loc(txt: str):
        """(dealer, city, state) best-effort from the card's innerText."""
        m = re.search(r"([A-Za-z][\w .'-]+?),\s*([A-Z]{2})\s*\(", txt or "")
        city, state = (m.group(1).strip(), m.group(2)) if m else (None, None)
        dealer = None
        if m:
            lines = [x.strip() for x in txt.split("\n") if x.strip()]
            try:
                i = next(j for j, ln in enumerate(lines) if ln.startswith(m.group(0)[:8]))
                for ln in reversed(lines[:i]):
                    if not re.match(r"^[\d.,$]+$|mi\.$|/mo$|Check", ln):  # skip price/rating/cta
                        dealer = ln[:60]
                        break
            except StopIteration:
                pass
        return dealer, city, state

    def _to_raw(self, r: dict, dom: dict) -> RawListing | None:
        pp = self._payload(r)
        price = float(pp.get("price") or 0)
        make = pp.get("make")
        if not (make and price > 0):
            return None
        lid = pp.get("listingId") or r.get("listing_id")
        dt = (pp.get("drivetrain") or "").upper()
        fuel = (pp.get("fuelType") or "").lower()
        msrp = float(pp.get("msrp") or 0) or None
        trim = pp.get("trim") or ""
        model = " ".join(x for x in (pp.get("model"), trim) if x)
        cpo = bool(pp.get("cpoIndicator") or (pp.get("banners") or {}).get("cpo"))
        dealer, city, state = self._loc(dom.get(lid, ""))
        try:
            year = int(pp.get("year") or 0)
        except (TypeError, ValueError):
            year = None
        return RawListing(
            source="cars",
            source_id=str(lid or pp.get("vin")),
            url=f"https://www.cars.com/vehicledetail/{lid}/" if lid else None,
            title=" ".join(str(x) for x in (pp.get("year"), make, model) if x),
            make=make, model=model or pp.get("model"),
            vin=pp.get("vin"),
            msrp=msrp,
            monthly=amortized_monthly(price),       # ESTIMATED finance payment
            months_remaining=TERM,
            state=state,
            days_on_market=0,
            raw={
                "body": pp.get("bodyStyle"),
                "awd": ("AWD" in dt or "4WD" in dt or "FOUR" in dt),
                "ev": fuel.startswith("electric"),
                "year": year,
                "odometer": int(pp.get("mileage") or 0),
                "price": price,
                "cpo": cpo,
                "monthly_is_estimate": True,
                "city": city,
                "dealer": dealer,
                "source_site": "cars.com",
            },
        )
