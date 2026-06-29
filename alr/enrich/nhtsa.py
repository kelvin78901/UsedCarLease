"""Vehicle enrichment. Two paths:

1. vPIC (NHTSA) - free, keyless VIN decode -> body class, displacement, etc.
   Real listings carry VINs; we decode them in batch.
2. Spec catalog - vPIC gives body/engine but not MSRP/hp/trim economics. We
   keep a small curated spec table keyed by make+model for the fields that
   actually drive ranking (hp, luxury, ev, awd). Seed listings (fake VINs) and
   any listing vPIC can't resolve fall through to this.
"""
from __future__ import annotations

import asyncio

import httpx
from tenacity import (AsyncRetrying, retry_if_exception, stop_after_attempt,
                      wait_exponential)

from ..config import (HTTP_TIMEOUT, USER_AGENT, VPIC_BASE, VPIC_BATCH_SIZE,
                      VPIC_CONCURRENCY)
from ..schema import NormalizedListing
from ..seed import CATALOG
from ..store import db as _db

_VPIC_RETRY_STATUS = {429, 500, 502, 503, 504}


def _vpic_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _VPIC_RETRY_STATUS
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))

# make -> luxury flag (cheap heuristic; refine as needed)
LUXURY_MAKES = {"BMW", "Audi", "Mercedes", "Genesis", "Lexus", "Porsche", "Volvo", "Acura"}

# build spec lookup from the same catalog the seed uses: (make, model_prefix) -> spec
_SPEC: dict[tuple[str, str], dict] = {}
for make, model, body, msrp, hp, lux, ev, awd in CATALOG:
    _SPEC[(make.lower(), model.split()[0].lower())] = {
        "body": body, "msrp": msrp, "hp": hp,
        "luxury": bool(lux), "ev": bool(ev), "awd_cap": bool(awd),
    }


def _catalog_spec(make: str, model: str) -> dict | None:
    key = (make.lower(), model.split()[0].lower()) if model else None
    if key and key in _SPEC:
        return _SPEC[key]
    # fall back to any model under the make
    for (mk, _), spec in _SPEC.items():
        if mk == (make or "").lower():
            return spec
    return None


def _parse_vpic(res: dict) -> dict:
    """A flat vPIC Results row -> the four fields we care about."""
    drive = (res.get("DriveType") or "").upper()
    return {
        "body": res.get("BodyClass") or None,
        "hp": int(float(res["EngineHP"])) if res.get("EngineHP") else None,
        "ev": (res.get("FuelTypePrimary") or "").lower().startswith("electric"),
        "awd": drive.startswith("AWD") or "4WD" in drive,
    }


def decode_vin(vin: str, client: httpx.Client) -> dict:
    """Single VIN -> vPIC fields. Empty dict on failure. Kept for probe.py."""
    try:
        r = client.get(f"{VPIC_BASE}/vehicles/DecodeVinValues/{vin}?format=json")
        r.raise_for_status()
        return _parse_vpic(r.json().get("Results", [{}])[0])
    except Exception:
        return {}


async def decode_vins_batch(vins_with_years: list[tuple[str, int | None]],
                            client: httpx.AsyncClient,
                            chunk: int = VPIC_BATCH_SIZE,
                            concurrency: int = VPIC_CONCURRENCY) -> dict[str, dict]:
    """Decode many VINs via vPIC DecodeVINValuesBatch (<=50/call, form POST),
    several batches concurrently (bounded by `concurrency`), with retry/backoff.

    Returns {VIN_UPPER: {body, hp, ev, awd}}. Results are matched by the echoed
    `VIN` field, not array position (vPIC may reorder / drop rows). Failed
    batches are logged and skipped, never raised."""
    sem = asyncio.Semaphore(concurrency)

    async def _one(batch: list[tuple[str, int | None]]) -> dict[str, dict]:
        # vPIC batch format: "VIN,modelyear;VIN,modelyear;..." (comma splits VIN
        # from the optional year, semicolon splits records).
        payload = ";".join(f"{vin},{year}" if year else vin for vin, year in batch)
        async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=0.5, max=8),
                retry=retry_if_exception(_vpic_retryable), reraise=True):
            with attempt:
                async with sem:
                    r = await client.post(
                        f"{VPIC_BASE}/vehicles/DecodeVINValuesBatch/",
                        data={"format": "json", "data": payload})
                    r.raise_for_status()
                    results = r.json().get("Results", [])
                return {v.upper(): _parse_vpic(res) for res in results
                        if (v := (res.get("VIN") or ""))}
        return {}

    batches = [vins_with_years[i:i + chunk]
               for i in range(0, len(vins_with_years), chunk)]
    merged: dict[str, dict] = {}
    for part in await asyncio.gather(*(_one(b) for b in batches),
                                     return_exceptions=True):
        if isinstance(part, dict):
            merged.update(part)
        else:
            print(f"[vpic] batch failed after retries: {part}")
    return merged


def _apply_catalog(l: NormalizedListing) -> None:
    """Offline pass over one listing. Fills body/hp/ev/luxury/msrp from the spec
    catalog WITHOUT clobbering values an adapter already provided (carried as
    _hp/_ev/_awd by normalize). Precedence: adapter-provided -> (vPIC later) ->
    catalog fallback. The old code unconditionally overwrote hp with a catalog
    guess that falls back to *any* model under the make."""
    hp = int(l.__dict__.get("_hp", 0) or 0)
    ev = bool(l.__dict__.get("_ev", False))
    ev_known = "_ev" in l.__dict__          # adapter/vPIC already settled ev
    awd = bool(l.__dict__.get("_awd", False))
    luxury = l.make in LUXURY_MAKES

    spec = _catalog_spec(l.make, l.model)
    if spec:
        if l.body in (None, "", "Unknown"):
            l.body = spec["body"]
        luxury = spec["luxury"]
        if not hp:
            hp = spec["hp"]
        # only let the (make-level fallback) catalog set ev when nobody else has;
        # never flip an adapter/vPIC-provided False to True.
        if not ev and not ev_known:
            ev = spec["ev"]
        if not l.msrp:
            l.msrp = spec["msrp"]
    l.body = l.body or "Unknown"

    l.__dict__["_hp"] = int(hp or 0)
    l.__dict__["_ev"] = bool(ev)
    l.__dict__["_awd"] = bool(awd)
    l.__dict__["_luxury"] = bool(luxury)


def _apply_decoded(l: NormalizedListing, d: dict) -> None:
    """Merge a vPIC decode into a listing, filling only what's still missing."""
    if d.get("body") and l.body in (None, "", "Unknown"):
        l.body = d["body"]
    if d.get("hp"):
        l.__dict__["_hp"] = int(d["hp"])
    if d.get("ev"):
        l.__dict__["_ev"] = True
    if d.get("awd"):
        l.__dict__["_awd"] = True


def enrich(listing: NormalizedListing, client: httpx.Client | None = None) -> NormalizedListing:
    """Single-listing enrichment. vPIC (real VIN) first, then catalog as the
    last-resort fallback. Retained for one-at-a-time callers; the crawl uses
    enrich_all (batched + cached)."""
    if (client and listing.vin and not listing.vin.startswith("SEED")
            and not int(listing.__dict__.get("_hp", 0) or 0)):
        _apply_decoded(listing, decode_vin(listing.vin, client))
    _apply_catalog(listing)
    return listing


async def enrich_all(listings: list[NormalizedListing],
                     client: httpx.AsyncClient | None = None,
                     con=None) -> list[NormalizedListing]:
    """Two-phase enrichment over the whole snapshot, precedence
    adapter -> vPIC -> catalog:
      1. for real-VIN listings still missing hp: read the DuckDB vin_cache, batch
         vPIC-decode only the cache misses (<=50/call), write results back;
      2. catalog (offline) as the LAST-RESORT fallback for everything still thin
         (no-VIN forum listings, or VINs vPIC couldn't resolve).
    This replaces the old one-vPIC-call-per-listing loop AND fixes the bug where
    a make-level catalog guess preempted real vPIC data."""
    need = [l for l in listings
            if l.vin and not l.vin.startswith("SEED")
            and not int(l.__dict__.get("_hp", 0) or 0)]
    if need:
        cached = _db.vin_cache_get(con, {l.vin for l in need}) if con else {}
        miss = [l for l in need if l.vin.upper() not in cached]
        decoded: dict[str, dict] = {}
        if client and miss:
            pairs = [(l.vin, l.__dict__.get("_year")) for l in miss]
            decoded = await decode_vins_batch(pairs, client)
            _db.vin_cache_put(con, decoded)
        table = {**cached, **decoded}
        for l in need:
            d = table.get(l.vin.upper())
            if d:
                _apply_decoded(l, d)

    for l in listings:
        _apply_catalog(l)
    return listings
