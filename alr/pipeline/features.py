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

import polars as pl

from ..schema import EnrichedListing, NormalizedListing


def effective_monthly(l: NormalizedListing) -> float:
    one_time = (l.drive_off + l.disposition_fee + l.transfer_fee
                + l.acquisition_fee - l.seller_incentive)
    return round(l.monthly + one_time / max(1, l.months_remaining))


def build_features(enriched: list[NormalizedListing]) -> list[EnrichedListing]:
    if not enriched:
        return []

    rows = []
    for l in enriched:
        eff = effective_monthly(l)
        # discount only when MSRP is a real figure (used-car msrp is junk/≈price).
        # Used cars: true discount off MSRP = (msrp-price)/msrp. Leases: keep the
        # lease formula (monthly*term vs msrp).
        if l.msrp and l.msrp >= 12000:
            if l.price > 0:
                disc = round((l.msrp - l.price) / l.msrp * 100) if l.msrp > l.price else 0
            else:
                disc = round((1 - (l.monthly * l.months_remaining) / (l.msrp * 0.55)) * 100)
        else:
            disc = 0
        rows.append({
            "listing_key": l.listing_key, "body": l.body,
            "effective_monthly": eff, "msrp_discount_pct": disc,
        })

    df = pl.DataFrame(rows)
    seg = df.group_by("body").agg(pl.col("effective_monthly").mean().alias("seg_avg"))
    df = df.join(seg, on="body", how="left").with_columns(
        ((pl.col("seg_avg") - pl.col("effective_monthly")) / pl.col("seg_avg")).alias("value_edge")
    )
    feat = {r["listing_key"]: r for r in df.to_dicts()}

    out: list[EnrichedListing] = []
    for l in enriched:
        f = feat[l.listing_key]
        out.append(EnrichedListing(
            **l.model_dump(),
            hp=int(l.__dict__.get("_hp", 0)),
            luxury=bool(l.__dict__.get("_luxury", False)),
            ev=bool(l.__dict__.get("_ev", False)),
            awd=bool(l.__dict__.get("_awd", False)),
            effective_monthly=f["effective_monthly"],
            msrp_discount_pct=f["msrp_discount_pct"],
            segment_avg_effective=round(f["seg_avg"], 1),
            value_edge=round(f["value_edge"], 4),
        ))
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
