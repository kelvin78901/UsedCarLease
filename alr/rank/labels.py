"""Turn a snapshot into LambdaRank training data: (X, y, group).

Cold-start labels are bootstrapped from the interpretable ranker - each body
segment is a query group, and listings are graded 0-4 by their within-segment
deal-score quantile. This teaches the model the ordering the rules imply, but
over the full feature space, so it generalizes past the hand-tuned weights.

`labels_from_history` is the real version: once the history table has multiple
crawls, a listing's relevance comes from how fast it sold (disappeared) and how
much interest it drew - genuine outcome supervision, no hand weights.
"""
from __future__ import annotations

import numpy as np

from ..pipeline.features import FEATURE_COLS, to_feature_row
from ..schema import EnrichedListing
from . import rules

# quantile -> graded relevance, paired with label_gain [0,1,3,7,15]
_BANDS = [(0.90, 4), (0.70, 3), (0.40, 2), (0.20, 1), (0.0, 0)]


def _grade(rankpos: int, n: int) -> int:
    pct = 1 - rankpos / max(1, n)  # 1.0 = best
    for thr, g in _BANDS:
        if pct >= thr:
            return g
    return 0


def bootstrap(listings: list[EnrichedListing]):
    """Return X, y, group, keys. One query group per body segment."""
    by_body: dict[str, list[EnrichedListing]] = {}
    front = rules.pareto_frontier(listings)
    for l in listings:
        by_body.setdefault(l.body, []).append(l)

    X, y, group, keys = [], [], [], []
    for body, group_listings in by_body.items():
        if len(group_listings) < 3:
            continue
        scored = sorted(
            group_listings,
            key=lambda l: rules.deal_score(l, l.listing_key in front),
            reverse=True,
        )
        n = len(scored)
        for pos, l in enumerate(scored):
            row = to_feature_row(l)
            X.append([row[c] for c in FEATURE_COLS])
            y.append(_grade(pos, n))
            keys.append(l.listing_key)
        group.append(n)
    return np.array(X, dtype=float), np.array(y, dtype=int), group, keys
