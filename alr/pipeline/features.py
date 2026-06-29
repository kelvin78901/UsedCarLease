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


MIN_PEERS = 4   # need this many same-kind peers to trust the fine comparison


def _is_used(l) -> bool:
    return bool(getattr(l, "price", 0) and l.price > 0)


def _fine_key(l) -> str:
    """Closest peers: used = make+body+3yr-band; lease = make+model+body, so 'cheap'
    means cheap for THIS car, not cheap because it's old (used) or a cheap class
    (lease). The u|/l| prefix keeps used and lease pools separate."""
    if _is_used(l):
        yb = (l.year // 3) * 3 if getattr(l, "year", 0) else 0
        return f"u|{l.make}|{l.body}|{yb}"
    mb = (l.model or "").split()[0] if l.model else ""
    return f"l|{l.make}|{mb}|{l.body}"


def _broad_key(l) -> str:
    """Fallback pool when the fine peer group is too small (leases are sparse):
    body level, still separated by used vs lease."""
    return f"{'u' if _is_used(l) else 'l'}|{l.body}"


def recompute_used_market(listings) -> None:
    """In-place value signal, shared by build_features (crawl) + api._load + retrain.

    Used cars: age-realistic est. finance monthly + value vs make/body/year-band
    peers, minus an age + high-mileage penalty (recent, reasonable-mileage, cheap-
    for-kind floats up; depreciated beaters don't).

    Leases: value = effective $/mo vs same make/model/body peers' MEDIAN -- cheaper
    than its kind is good, MORE EXPENSIVE (vs-mkt positive) is penalized, never
    rewarded. Sparse peer groups fall back to the body-level median instead of a
    degenerate single-listing baseline that produced fake top scores; 0hp leases
    (VIN not yet enriched) take a small drag so missing features can't top the board.
    """
    from ..adapters.marketcheck import amortized_monthly
    for l in listings:
        if _is_used(l) and getattr(l, "year", 0):
            l.effective_monthly = amortized_monthly(l.price, term=used_finance_term(l.year))
    fine: dict[str, list] = defaultdict(list)
    broad: dict[str, list] = defaultdict(list)
    for l in listings:
        fine[_fine_key(l)].append(l.effective_monthly)
        broad[_broad_key(l)].append(l.effective_monthly)
    fine_med = {k: statistics.median(v) for k, v in fine.items()}
    broad_med = {k: statistics.median(v) for k, v in broad.items()}
    for l in listings:
        fk = _fine_key(l)
        base = (fine_med[fk] if len(fine[fk]) >= MIN_PEERS
                else broad_med.get(_broad_key(l), l.effective_monthly)) or 1.0
        edge = (base - l.effective_monthly) / base
        if _is_used(l):                                     # age + mileage drag
            age = max(0, CURRENT_YEAR - l.year) if getattr(l, "year", 0) else 12
            odo = getattr(l, "odometer", 0) or 0
            edge -= 0.012 * age + odo / 1_000_000           # 20yr -> -.24, 150k mi -> -.15
        elif not getattr(l, "hp", 0):                       # lease w/ missing power
            edge -= 0.10
        l.segment_avg_effective = round(base, 1)
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
