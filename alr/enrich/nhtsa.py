"""Vehicle enrichment. Two paths:

1. vPIC (NHTSA) - free, keyless VIN decode -> body class, displacement, etc.
   Real listings carry VINs; we decode them in batch.
2. Spec catalog - vPIC gives body/engine but not MSRP/hp/trim economics. We
   keep a small curated spec table keyed by make+model for the fields that
   actually drive ranking (hp, luxury, ev, awd). Seed listings (fake VINs) and
   any listing vPIC can't resolve fall through to this.
"""
from __future__ import annotations

import httpx

from ..config import HTTP_TIMEOUT, USER_AGENT, VPIC_BASE, VPIC_BATCH_SIZE
from ..schema import NormalizedListing
from ..seed import CATALOG
from ..store import db as _db

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


def decode_vins_batch(vins_with_years: list[tuple[str, int | None]],
                      client: httpx.Client,
                      chunk: int = VPIC_BATCH_SIZE) -> dict[str, dict]:
    """Decode many VINs via vPIC DecodeVINValuesBatch (<=50/call, form POST).

    Returns {VIN_UPPER: {body, hp, ev, awd}}. Results are matched by the echoed
    `VIN` field, not array position (vPIC may reorder / drop rows). Failed
    batches are logged and skipped, never raised."""
    out: dict[str, dict] = {}
    for i in range(0, len(vins_with_years), chunk):
        batch = vins_with_years[i:i + chunk]
        # vPIC batch format: "VIN,modelyear;VIN,modelyear;..." (comma splits VIN
        # from the optional year, semicolon splits records).
        payload = ";".join(f"{vin},{year}" if year else vin for vin, year in batch)
        try:
            r = client.post(f"{VPIC_BASE}/vehicles/DecodeVINValuesBatch/",
                            data={"format": "json", "data": payload})
            r.raise_for_status()
            results = r.json().get("Results", [])
        except Exception as e:
            print(f"[vpic] batch of {len(batch)} VINs failed: {e}")
            continue
        for res in results:
            vin = (res.get("VIN") or "").upper()
            if vin:
                out[vin] = _parse_vpic(res)
    return out


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


def enrich_all(listings: list[NormalizedListing],
               client: httpx.Client | None = None,
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
            decoded = decode_vins_batch(pairs, client)
            _db.vin_cache_put(con, decoded)
        table = {**cached, **decoded}
        for l in need:
            d = table.get(l.vin.upper())
            if d:
                _apply_decoded(l, d)

    for l in listings:
        _apply_catalog(l)
    return listings
