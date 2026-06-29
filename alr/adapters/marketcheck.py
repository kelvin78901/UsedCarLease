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

import os
import re
from datetime import datetime, timezone
from typing import Iterable

from .base import BaseAdapter, adapter
from ..schema import RawListing

API = "https://api.marketcheck.com/v2/search/car/active"
KEY = os.getenv("ALR_MARKETCHECK_KEY", "")
ZIP = os.getenv("ALR_MC_ZIP", "20001")            # default DC; override per your area
RADIUS = os.getenv("ALR_MC_RADIUS", "100")
MAX_ROWS = int(os.getenv("ALR_MC_MAX_ROWS", "100"))   # total listings to pull
PAGE = min(50, MAX_ROWS)                                # API max 50/req
APR = float(os.getenv("ALR_MC_APR", "0.075"))         # finance assumptions for
TERM = int(os.getenv("ALR_MC_TERM", "72"))            # the price->monthly estimate
EXTRA_PARAMS = os.getenv("ALR_MC_PARAMS", "")          # e.g. "make=Toyota&price_range=10000-30000"


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
    def fetch(self) -> Iterable[RawListing]:
        if not KEY:
            print("[marketcheck] no API key. Set ALR_MARKETCHECK_KEY "
                  "(free tier at marketcheck.com/apis). Skipping.")
            return
        pulled = 0
        start = 0
        while pulled < MAX_ROWS:
            params = {
                "api_key": KEY, "car_type": "used", "zip": ZIP, "radius": RADIUS,
                "rows": min(PAGE, MAX_ROWS - pulled), "start": start,
                "include_relevant_links": "false",
            }
            for kv in EXTRA_PARAMS.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k] = v
            try:
                r = self.client.get(API, params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                print(f"[marketcheck] request failed: {e}")
                return
            listings = data.get("listings", [])
            if not listings:
                break
            for L in listings:
                rl = self._to_raw(L)
                if rl:
                    yield rl
            pulled += len(listings)
            start += len(listings)
            if pulled >= data.get("num_found", 0):
                break
        print(f"[marketcheck] pulled {pulled} used listings near {ZIP}")

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
