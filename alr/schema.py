"""Unified data contract. Every adapter must emit RawListing; the pipeline
promotes RawListing -> NormalizedListing -> EnrichedListing -> ScoredListing.
Keeping these as Pydantic models makes the heterogeneous sources converge on
one schema and gives free validation at every stage boundary."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RawListing(BaseModel):
    """Whatever an adapter could scrape. Sparse on purpose - sources differ."""
    source: str
    source_id: str                      # platform-native id (thread id, stock no)
    url: Optional[str] = None
    title: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    vin: Optional[str] = None
    msrp: Optional[float] = None
    monthly: Optional[float] = None       # advertised monthly payment
    months_remaining: Optional[int] = None
    miles_per_year: Optional[int] = None
    remaining_miles: Optional[int] = None
    drive_off: Optional[float] = None
    transfer_fee: Optional[float] = None
    acquisition_fee: Optional[float] = None
    disposition_fee: Optional[float] = None
    seller_incentive: Optional[float] = None   # cash to assume the lease
    state: Optional[str] = None
    days_on_market: Optional[int] = None
    price_drops: Optional[int] = None
    favorites: Optional[int] = None
    raw: dict = Field(default_factory=dict)    # keep the original blob for debugging
    crawled_at: datetime = Field(default_factory=_now)


class NormalizedListing(BaseModel):
    """Defaults filled, units harmonized, dedup key assigned."""
    listing_key: str                    # stable dedup key
    source: str
    source_id: str
    url: Optional[str] = None
    make: str
    model: str
    vin: Optional[str] = None
    body: str = "Unknown"
    msrp: float
    monthly: float
    months_remaining: int
    miles_per_month: int
    remaining_miles: int
    drive_off: float = 0.0
    transfer_fee: float = 0.0
    acquisition_fee: float = 0.0
    disposition_fee: float = 0.0
    seller_incentive: float = 0.0
    state: str = "NA"
    days_on_market: int = 0
    price_drops: int = 0
    favorites: int = 0
    crawled_at: datetime = Field(default_factory=_now)


class EnrichedListing(NormalizedListing):
    """+ vehicle metadata (vPIC) and derived market features."""
    hp: int = 0
    luxury: bool = False
    ev: bool = False
    awd: bool = False
    effective_monthly: float = 0.0
    msrp_discount_pct: float = 0.0
    segment_avg_effective: float = 0.0
    value_edge: float = 0.0             # (seg_avg - eff) / seg_avg


class ScoredListing(EnrichedListing):
    rank: int = 0
    base_score: float = 0.0
    personalized_score: float = 0.0
    personalization_bonus: float = 0.0
    on_frontier: bool = False
    ltr_score: Optional[float] = None
    scorer: str = "rules"                 # "ltr" | "rules"
    score_drivers: list = []              # model SHAP contributions [(label, value), ...]
    explanation: str = ""
