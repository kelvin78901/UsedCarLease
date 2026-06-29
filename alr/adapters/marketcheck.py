"""Marketcheck adapter - structured used-car inventory across the whole US market.

Why this instead of scraping Cars.com/dealer sites: Marketcheck aggregates active
listings from ~every US/CA dealer site + major marketplaces into one clean,
normalized JSON API, updated daily. One key replaces dozens of fragile,
WAF-blocked scrapers. It also returns `dom` (days on market) natively - exactly
the self-labeling signal the ranker wants (fast-selling = good deal).

Real API (verified against docs):
    GET https://api.marketcheck.com/v2/search/car/active
        ?api_key=...&car_type=used&zip=...&radius=...&rows=50&start=0
    -> { num_found, listings: [ {id, vin, price, miles, msrp, dom, dom_active,
         first_seen_at_date, seller_type, vdp_url, source,
         dealer:{city,state}, build:{year,make,model,trim,body_type,
         drivetrain,fuel_type,...}} ] }

Used cars are purchases, not leases - there is no monthly payment. To rank them
alongside lease transfers on one axis, we convert price to an estimated financed
monthly via standard amortization (APR/term configurable). This is an estimate,
flagged as such in `raw`.

Needs ALR_MARKETCHECK_KEY. Free tier exists; respect its rate limits via
ALR_MC_MAX_ROWS. Without a key the adapter no-ops cleanly.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone

from .base import BaseAdapter, adapter
from ..config import MC_CONCURRENCY, MC_RETRIES
from ..schema import RawListing

API = "https://api.marketcheck.com/v2/search/car/active"
KEY = os.getenv("ALR_MARKETCHECK_KEY", "")
ZIP = os.getenv("ALR_MC_ZIP", "20001")            # default DC; override per your area
RADIUS = os.getenv("ALR_MC_RADIUS", "100")
MAX_ROWS = int(os.getenv("ALR_MC_MAX_ROWS", "100"))   # GLOBAL row budget across the sweep
PAGE = max(1, min(50, MAX_ROWS))                        # API max 50/req
APR = float(os.getenv("ALR_MC_APR", "0.075"))         # finance assumptions for
TERM = int(os.getenv("ALR_MC_TERM", "72"))            # the price->monthly estimate
EXTRA_PARAMS = os.getenv("ALR_MC_PARAMS", "")          # e.g. "make=Toyota&price_range=10000-30000"

# Query sweep: cartesian product of (zip x make x price-band). Each slice is one
# query (capped by the API at 1500 paginated rows). Defaults to ONE slice so the
# free tier (500 calls/mo) is safe; scale up purely by setting these env lists.
ZIPS = [z.strip() for z in os.getenv("ALR_MC_ZIPS", ZIP).split(",") if z.strip()]
MAKES = [m.strip() for m in os.getenv("ALR_MC_MAKES", "").split(",") if m.strip()]
BANDS = [b.strip() for b in os.getenv("ALR_MC_PRICE_BANDS", "").split(",") if b.strip()]
PER_QUERY_CAP = int(os.getenv("ALR_MC_PER_QUERY_CAP", "1500"))   # API hard cap / query


def _hp_from_build(b: dict) -> int:
    """Best-effort horsepower from the Marketcheck build object. The API usually
    omits a dedicated hp field, so we also scrape the descriptive engine string
    (e.g. '2.0L Turbo 248 hp'). 0 when unknown -> vPIC batch fills the gap."""
    for k in ("horsepower", "engine_power", "power"):
        v = b.get(k)
        if v:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                pass
    m = re.search(r"(\d{2,4})\s*hp", b.get("engine") or "", re.I)
    return int(m.group(1)) if m else 0


def amortized_monthly(price: float, apr: float = APR, term: int = TERM) -> float:
    if not price or price <= 0:
        return 0.0
    r = apr / 12
    if r == 0:
        return round(price / term)
    return round(price * r / (1 - (1 + r) ** -term))


@adapter("marketcheck")
class MarketcheckAdapter(BaseAdapter):
    concurrency = MC_CONCURRENCY    # keep low: free tier rate limits
    max_retries = MC_RETRIES

    @staticmethod
    def _sweep_plan() -> list[dict]:
        """Cartesian product of the configured slices -> base query params."""
        plan = []
        for z in ZIPS:
            for mk in (MAKES or [None]):
                for band in (BANDS or [None]):
                    p = {"car_type": "used", "zip": z, "radius": RADIUS}
                    if mk:
                        p["make"] = mk
                    if band:
                        p["price_range"] = band
                    plan.append(p)
        return plan

    async def _page(self, base: dict, start: int) -> tuple[list, int]:
        """One API call: (listings, num_found). Failures degrade to ([], 0) so a
        single bad slice/page never kills the whole sweep."""
        params = {"api_key": KEY, "include_relevant_links": "false",
                  "rows": PAGE, "start": start, **base}
        for kv in EXTRA_PARAMS.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                params[k] = v
        try:
            data = await self.aget_json(API, params=params)
        except Exception as e:
            print(f"[marketcheck] page (zip={base.get('zip')} start={start}) failed: {e}")
            return [], 0
        return data.get("listings", []), int(data.get("num_found", 0))

    async def fetch(self) -> list[RawListing]:
        if not KEY:
            print("[marketcheck] no API key. Set ALR_MARKETCHECK_KEY "
                  "(free tier at marketcheck.com/apis). Skipping.")
            return []
        plan = self._sweep_plan()
        budget = MAX_ROWS
        rows: list[dict] = []

        # phase 1: probe start=0 of every slice concurrently to learn num_found,
        # then schedule the remaining offset pages within the row budget.
        probes = await asyncio.gather(*(self._page(s, 0) for s in plan))
        pending: list[tuple[dict, int]] = []
        for s, (listings, num_found) in zip(plan, probes):
            if budget <= 0:
                break
            take = listings[:budget]          # slice at append: never overshoot
            rows.extend(take)
            budget -= len(take)
            start = len(listings)
            cap = min(num_found, PER_QUERY_CAP)
            # only queue pages we still have budget for (each page is up to PAGE rows)
            while start < cap and (budget - len(pending) * PAGE) > 0:
                pending.append((s, start))
                start += PAGE

        # phase 2: fetch the queued pages concurrently; still slice at append.
        for listings, _ in await asyncio.gather(*(self._page(s, st) for s, st in pending)):
            if budget <= 0:
                break
            take = listings[:budget]
            rows.extend(take)
            budget -= len(take)

        out = [r for r in (self._to_raw(L) for L in rows) if r]
        calls = len(plan) + len(pending)
        print(f"[marketcheck] sweep slices={len(plan)} calls~{calls} "
              f"pulled={len(rows)} -> {len(out)} rankable "
              f"(free tier budget = 500 calls/mo)")
        return out

    @staticmethod
    def _to_raw(L: dict) -> RawListing | None:
        b = L.get("build") or {}
        price = L.get("price")
        if not (b.get("make") and price):
            return None
        dealer = L.get("dealer") or {}
        dt = (b.get("drivetrain") or "").upper()
        fuel = (b.get("fuel_type") or "").lower()
        body = b.get("body_type") or b.get("vehicle_type")
        monthly = amortized_monthly(float(price))
        return RawListing(
            source="marketcheck",
            source_id=str(L.get("id") or L.get("vin")),
            url=L.get("vdp_url"),
            title=L.get("heading"),
            make=b.get("make"),
            model=" ".join(x for x in (b.get("model"), b.get("trim")) if x) or b.get("model"),
            vin=L.get("vin"),
            msrp=L.get("msrp"),
            monthly=monthly,                       # ESTIMATED finance payment
            months_remaining=TERM,                 # finance term, for the cost amortization
            drive_off=0.0,
            state=dealer.get("state"),
            days_on_market=L.get("dom_active") or L.get("dom") or 0,
            price_drops=0,
            raw={
                "body": body,
                "awd": ("AWD" in dt or "4WD" in dt or "4X4" in dt),
                "ev": fuel.startswith("electric"),
                "hp": _hp_from_build(b),
                "year": b.get("year"),
                "odometer": L.get("miles"),
                "price": price,
                "seller_type": L.get("seller_type"),
                "source_site": L.get("source"),
                "monthly_is_estimate": True,
                "city": dealer.get("city"),
            },
        )
