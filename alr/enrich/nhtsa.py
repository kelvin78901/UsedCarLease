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

from ..config import HTTP_TIMEOUT, USER_AGENT, VPIC_BASE
from ..schema import NormalizedListing
from ..seed import CATALOG

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


def decode_vin(vin: str, client: httpx.Client) -> dict:
    """Single VIN -> vPIC fields. Empty dict on failure."""
    try:
        r = client.get(f"{VPIC_BASE}/vehicles/DecodeVinValues/{vin}?format=json")
        r.raise_for_status()
        res = r.json().get("Results", [{}])[0]
        return {
            "body": res.get("BodyClass") or None,
            "hp": int(float(res.get("EngineHP"))) if res.get("EngineHP") else None,
            "ev": (res.get("FuelTypePrimary", "").lower().startswith("electric")),
            "awd": (res.get("DriveType", "").upper().startswith("AWD")
                    or "4WD" in res.get("DriveType", "").upper()),
        }
    except Exception:
        return {}


def enrich(listing: NormalizedListing, client: httpx.Client | None = None) -> NormalizedListing:
    """Populate body/hp/luxury/ev/awd. Tries the spec catalog first (fast,
    offline), then vPIC for real VINs that miss the catalog."""
    spec = _catalog_spec(listing.make, listing.model)
    body = listing.body
    hp = 0
    luxury = listing.make in LUXURY_MAKES
    ev = False
    awd = False

    if spec:
        body = spec["body"]
        hp = spec["hp"]
        luxury = spec["luxury"]
        ev = spec["ev"]
        if not listing.msrp:
            listing.msrp = spec["msrp"]

    # real VIN + missing hp -> ask vPIC
    if client and listing.vin and not listing.vin.startswith("SEED") and not hp:
        d = decode_vin(listing.vin, client)
        body = d.get("body") or body
        hp = d.get("hp") or hp
        ev = d.get("ev") or ev
        awd = d.get("awd") or awd

    listing.body = body or "Unknown"
    # awd from raw blob if the adapter knew it (seed does)
    awd = bool(listing.__dict__.get("_awd", awd))

    # stash derived flags on the model via attribute (promoted in pipeline.features)
    listing.__dict__["_hp"] = int(hp or 0)
    listing.__dict__["_luxury"] = bool(luxury)
    listing.__dict__["_ev"] = bool(ev)
    listing.__dict__["_awd"] = bool(awd)
    return listing
