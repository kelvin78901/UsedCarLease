"""RawListing -> NormalizedListing. Fill defaults, harmonize units, assign a
dedup key. Listings too sparse to rank (no monthly / no msrp) are dropped here
rather than poisoning the feature store."""
from __future__ import annotations

from ..schema import NormalizedListing, RawListing

_BODY_FROM_TITLE = [
    ("suv", "SUV"), ("crossover", "SUV"), ("sedan", "Sedan"),
    ("coupe", "Coupe"), ("truck", "Truck"), ("ev", "EV"),
]


def _guess_body(raw: RawListing) -> str:
    if raw.raw.get("body"):
        return raw.raw["body"]
    t = (raw.title or "").lower()
    for kw, label in _BODY_FROM_TITLE:
        if kw in t:
            return label
    return "Unknown"


def normalize(raw: RawListing) -> NormalizedListing | None:
    if not raw.make or not raw.monthly:
        return None  # un-rankable

    months = raw.months_remaining or 24
    mpy = raw.miles_per_year or 12000
    mpm = max(1, round(mpy / 12))
    rem_miles = raw.remaining_miles or mpm * months

    # stable dedup key: real VIN wins, else source-native id
    vin = raw.vin if (raw.vin and not raw.vin.startswith("SEED")) else None
    key = f"vin:{vin}" if vin else f"{raw.source}:{raw.source_id}"

    n = NormalizedListing(
        listing_key=key,
        source=raw.source,
        source_id=raw.source_id,
        url=raw.url,
        make=raw.make,
        model=raw.model or "Unknown",
        vin=raw.vin,
        body=_guess_body(raw),
        msrp=float(raw.msrp or 0.0),
        monthly=float(raw.monthly),
        months_remaining=int(months),
        miles_per_month=int(mpm),
        remaining_miles=int(rem_miles),
        drive_off=float(raw.drive_off or 0.0),
        transfer_fee=float(raw.transfer_fee or 0.0),
        acquisition_fee=float(raw.acquisition_fee or 0.0),
        disposition_fee=float(raw.disposition_fee or 0.0),
        seller_incentive=float(raw.seller_incentive or 0.0),
        state=raw.state or "NA",
        days_on_market=int(raw.days_on_market or 0),
        price_drops=int(raw.price_drops or 0),
        favorites=int(raw.favorites or 0),
        cpo=bool(raw.raw.get("cpo")),
        odometer=int(raw.raw.get("odometer") or 0),
        price=float(raw.raw.get("price") or 0.0),
        dealer_city=(raw.raw.get("city") or "")[:60],
        crawled_at=raw.crawled_at,
    )
    # carry adapter-known build data forward for the enricher (precedence:
    # adapter-provided beats vPIC beats catalog). Marketcheck supplies all four.
    if "awd" in raw.raw:
        n.__dict__["_awd"] = bool(raw.raw["awd"])
    if "ev" in raw.raw:
        n.__dict__["_ev"] = bool(raw.raw["ev"])
    if raw.raw.get("hp"):
        n.__dict__["_hp"] = int(raw.raw["hp"])
    if raw.raw.get("year"):
        try:
            n.__dict__["_year"] = int(raw.raw["year"])
        except (TypeError, ValueError):
            pass
    return n
