"""Feature engineering. The heart of the system: the effective-cost engine.

Advertised monthly payment is a lie by omission. The true carrying cost of a
lease takeover amortizes every one-time amount over the remaining term:

    effective_monthly = monthly
                      + (drive_off + disposition_fee + transfer_fee
                         + acquisition_fee - seller_incentive) / months_remaining

A seller incentive (cash to assume the lease) is negative cost. Once every
listing is on this single axis, comparison across sources becomes meaningful and
the rest of the system (Pareto, scoring, LTR) has a sound target to work with.

Polars does the segment aggregation so market-relative features (segment median,
value edge) are computed over the whole snapshot in one pass.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timezone

from ..schema import EnrichedListing, NormalizedListing

CURRENT_YEAR = datetime.now(timezone.utc).year


def effective_monthly(l: NormalizedListing) -> float:
    one_time = (l.drive_off + l.disposition_fee + l.transfer_fee
                + l.acquisition_fee - l.seller_incentive)
    return round(l.monthly + one_time / max(1, l.months_remaining))


def used_finance_term(year: int) -> int:
    """Realistic used-car loan term: ~72mo when new, shrinking with age to a 24mo
    floor. A 20yr-old car amortized over 72mo gives a fake-low monthly ($104/mo),
    which made the oldest cars look like the best deals."""
    if not year:
        return 72
    age = max(0, CURRENT_YEAR - year)
    return max(24, min(72, 72 - max(0, age - 3) * 6))


def _seg_key(l) -> str:
    """Used cars compare WITHIN make+body+3yr-band peers (so 'cheap' means cheap vs
    similar cars, not cheap because it's 20 years old). Leases compare by body."""
    if getattr(l, "price", 0) and l.price > 0:
        yb = (l.year // 3) * 3 if getattr(l, "year", 0) else 0
        return f"{l.make}|{l.body}|{yb}"
    return l.body


def recompute_used_market(listings) -> None:
    """In-place. Used cars: (1) age-realistic est. finance monthly, (2) value vs
    make/body/year-band peers minus an age + high-mileage penalty -- so the top
    deals become recent, reasonable-mileage cars that are cheap for their kind,
    not depreciated 2006 beaters. Leases keep their real effective_monthly +
    body-segment value. Shared by build_features (crawl) and api._load (serve)."""
    from ..adapters.marketcheck import amortized_monthly
    for l in listings:
        if getattr(l, "price", 0) and l.price > 0 and getattr(l, "year", 0):
            l.effective_monthly = amortized_monthly(l.price, term=used_finance_term(l.year))
    groups: dict[str, list] = defaultdict(list)
    for l in listings:
        groups[_seg_key(l)].append(l.effective_monthly)
    avg = {k: statistics.mean(v) for k, v in groups.items() if v}
    for l in listings:
        a = avg.get(_seg_key(l)) or 1.0
        edge = (a - l.effective_monthly) / a
        if getattr(l, "price", 0) and l.price > 0:          # used: age + mileage drag
            age = max(0, CURRENT_YEAR - l.year) if getattr(l, "year", 0) else 12
            odo = getattr(l, "odometer", 0) or 0
            edge -= 0.012 * age + odo / 1_000_000           # 20yr -> -.24, 150k mi -> -.15
        l.segment_avg_effective = round(a, 1)
        l.value_edge = round(max(-2.0, min(1.0, edge)), 4)


def build_features(enriched: list[NormalizedListing]) -> list[EnrichedListing]:
    if not enriched:
        return []

    out: list[EnrichedListing] = []
    for l in enriched:
        # MSRP discount only for a real MSRP. NOTE: it's no longer a used-car value
        # signal (a 20yr car is "90% off MSRP" purely from depreciation) -- value
        # comes from recompute_used_market's peer comparison below.
        if l.msrp and l.msrp >= 12000 and l.price > 0 and l.msrp > l.price:
            disc = round((l.msrp - l.price) / l.msrp * 100)
        elif l.msrp and l.msrp >= 12000 and l.price <= 0:
            disc = round((1 - (l.monthly * l.months_remaining) / (l.msrp * 0.55)) * 100)
        else:
            disc = 0
        out.append(EnrichedListing(
            **l.model_dump(),
            hp=int(l.__dict__.get("_hp", 0)),
            luxury=bool(l.__dict__.get("_luxury", False)),
            ev=bool(l.__dict__.get("_ev", False)),
            awd=bool(l.__dict__.get("_awd", False)),
            effective_monthly=effective_monthly(l),
            msrp_discount_pct=disc,
            segment_avg_effective=0.0,
            value_edge=0.0,
        ))
    recompute_used_market(out)   # age-realistic monthly + peer/age/mileage value
    return out


# columns the LTR model trains on
FEATURE_COLS = [
    "effective_monthly", "monthly", "msrp", "msrp_discount_pct", "value_edge",
    "hp", "months_remaining", "miles_per_month", "remaining_miles",
    "drive_off", "transfer_fee", "acquisition_fee", "disposition_fee",
    "seller_incentive", "days_on_market", "price_drops", "favorites",
    "luxury", "ev", "awd",
]


def to_feature_row(l: EnrichedListing) -> dict:
    d = l.model_dump()
    return {c: float(d.get(c, 0) or 0) for c in FEATURE_COLS}
