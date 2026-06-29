"""Dedup. The same car shows up across platforms and across crawls. Collapse on
listing_key; when two records collide, keep the one with the lower effective
cost if known, else the one with more populated fields (richer record)."""
from __future__ import annotations

from ..schema import NormalizedListing


def _richness(x: NormalizedListing) -> int:
    return sum(
        1 for v in (x.msrp, x.drive_off, x.transfer_fee, x.disposition_fee,
                    x.seller_incentive, x.remaining_miles) if v
    )


def dedup(listings: list[NormalizedListing]) -> list[NormalizedListing]:
    best: dict[str, NormalizedListing] = {}
    for l in listings:
        cur = best.get(l.listing_key)
        if cur is None:
            best[l.listing_key] = l
            continue
        # prefer richer record; tie-break on lower monthly
        if (_richness(l), -l.monthly) > (_richness(cur), -cur.monthly):
            best[l.listing_key] = l
    return list(best.values())
