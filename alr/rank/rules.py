"""Interpretable ranking. This is the cold-start ranker and the fallback when no
LTR model is trained. It is also the thing that generates weak labels for the
LTR model later, so it has to be defensible on its own.

Two pieces:
  pareto_frontier - non-dominated set on (effective cost down, horsepower up)
  deal_score      - market edge + frontier bonus + freshness - instability
"""
from __future__ import annotations

from ..schema import EnrichedListing


def pareto_frontier(listings: list[EnrichedListing]) -> set[str]:
    """A listing is dominated if another is <= on cost and >= on hp, strictly
    better on at least one axis. Frontier = the non-dominated set."""
    front: set[str] = set()
    for a in listings:
        dominated = any(
            b.listing_key != a.listing_key
            and b.effective_monthly <= a.effective_monthly
            and b.hp >= a.hp
            and (b.effective_monthly < a.effective_monthly or b.hp > a.hp)
            for b in listings
        )
        if not dominated:
            front.add(a.listing_key)
    return front


def deal_score(l: EnrichedListing, on_frontier: bool) -> float:
    s = 50 + l.value_edge * 200
    if on_frontier:
        s += 6
    if l.days_on_market < 3:
        s += 4
    s -= l.price_drops * 2.5
    return max(2.0, min(99.0, round(s)))


def explain(l: EnrichedListing, on_frontier: bool, pers: float) -> str:
    edge = round(l.value_edge * 100)
    parts = [f"{edge}% under the {l.body} segment median of "
             f"${round(l.segment_avg_effective):,}."]
    parts.append("On the cost-power frontier - nothing dominates it."
                 if on_frontier else
                 "Off the frontier; some listings beat its power-per-dollar.")
    if l.seller_incentive > 0:
        parts.append(f"Seller adds ${round(l.seller_incentive):,} to take over.")
    if pers > 0:
        parts.append(f"+{round(pers)} from your preferences.")
    return " ".join(parts)
