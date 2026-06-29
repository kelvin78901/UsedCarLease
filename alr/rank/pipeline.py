"""The 4-stage ranking pipeline that the API serves and the dashboard mirrors.

  Stage 1  Hard filter      budget, body, min miles, max term
  Stage 2  Pareto frontier  flag non-dominated listings
  Stage 3  Score            LTR if a model is loaded, else interpretable rules
  Stage 4  Personalize      re-weight by user preferences, re-sort

Returns the ranked ScoredListing list plus per-stage counts for the pipeline
meter in the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..schema import EnrichedListing, ScoredListing
from . import rules

# Used-car purchases (estimated finance monthly) vs lease takeovers (real monthly).
USED_SOURCES = {"marketcheck", "cars"}


def is_used(l) -> bool:
    return l.source in USED_SOURCES


@dataclass
class Prefs:
    budget: float = 1400
    bodies: set[str] = field(default_factory=set)  # empty = no body filter (all types)
    listing_type: str = "all"      # all | lease | used
    cpo_only: bool = False         # used cars: certified pre-owned only
    want_awd: bool = False
    want_lux: bool = False
    min_mpm: int = 0
    max_months: int = 120   # inclusive by default: financed used cars carry a 72mo term
    pref_states: set[str] = field(default_factory=set)
    top_k: int = 100


def _type_ok(l, lt: str) -> bool:
    if lt == "used":
        return is_used(l)
    if lt == "lease":
        return not is_used(l)
    return True  # "all"


@dataclass
class RankResult:
    ranked: list[ScoredListing]
    counts: dict[str, int]


def _model_explanation(l, drivers, pers):
    """Phrase the model's top SHAP contributions as the 'why', so the displayed
    reason is the model's own attribution, not a hand-tuned heuristic."""
    if not drivers:
        return "Ranked by the LambdaMART model."
    ups = [f"{lab}" for lab, v in drivers if v > 0]
    downs = [f"{lab}" for lab, v in drivers if v < 0]
    parts = ["LambdaMART ranked this on "]
    if ups:
        parts.append("strong " + ", ".join(ups[:3]))
    if downs:
        parts.append((" despite weak " if ups else "weak ") + ", ".join(downs[:2]))
    s = "".join(parts) + "."
    if pers > 0:
        s += f" +{round(pers)} from your preferences."
    return s


def rank(listings: list[EnrichedListing], prefs: Prefs,
         ltr_scorer=None) -> RankResult:
    total = len(listings)

    # Stage 1 - hard filter. Empty bodies set = no body filter (show all types),
    # so listings whose body has no UI chip (Pickup/Wagon/Van/...) aren't dropped.
    filtered = [
        l for l in listings
        if (not prefs.bodies or l.body in prefs.bodies)
        and _type_ok(l, prefs.listing_type)
        and (not prefs.cpo_only or l.cpo)
        and l.effective_monthly <= prefs.budget
        and l.miles_per_month >= prefs.min_mpm
        and l.months_remaining <= prefs.max_months
    ]

    # Stage 2 - Pareto
    front = rules.pareto_frontier(filtered)

    # Stage 3 - score. The badge always shows an interpretable 0-99 rules score;
    # when an LTR model is present it drives the *ordering* (normalized to the
    # same 0-100 scale so the personalization bonus stays comparable).
    raw_ltr = {}
    if ltr_scorer is not None and filtered:
        vals = {l.listing_key: float(ltr_scorer(l)) for l in filtered}
        lo, hi = min(vals.values()), max(vals.values())
        span = (hi - lo) or 1.0
        raw_ltr = {k: 2 + 97 * (v - lo) / span for k, v in vals.items()}  # -> ~2..99

    scored: list[ScoredListing] = []
    for l in filtered:
        onf = l.listing_key in front
        display = rules.deal_score(l, onf)               # 0-99, interpretable
        order_base = raw_ltr.get(l.listing_key, display)  # LTR if available

        # Stage 4 - personalization bonus (same 0-100 scale)
        pers = 0.0
        if prefs.want_awd and l.awd:
            pers += 7
        if prefs.want_lux and l.luxury:
            pers += 6
        if l.state in prefs.pref_states:
            pers += 8

        # explanation + drivers come from the MODEL when one is loaded; the
        # rules string is only a cold-start fallback.
        if l.listing_key in raw_ltr and ltr_scorer is not None:
            drivers = ltr_scorer.contributions(l)
            scorer_name = "ltr"
            expl = _model_explanation(l, drivers, pers)
        else:
            drivers = []
            scorer_name = "rules"
            expl = rules.explain(l, onf, pers)

        scored.append(ScoredListing(
            **l.model_dump(),
            on_frontier=onf,
            base_score=display,
            personalization_bonus=pers,
            personalized_score=round(min(99.0, order_base + pers), 1),
            ltr_score=round(raw_ltr[l.listing_key], 2) if l.listing_key in raw_ltr else None,
            scorer=scorer_name,
            score_drivers=drivers,
            explanation=expl,
        ))

    scored.sort(key=lambda x: x.personalized_score, reverse=True)
    for i, s in enumerate(scored, 1):
        s.rank = i

    return RankResult(
        ranked=scored[: prefs.top_k],
        counts={"crawled": total, "filtered": len(filtered),
                "frontier": len(front), "ranked": min(len(scored), prefs.top_k)},
    )
