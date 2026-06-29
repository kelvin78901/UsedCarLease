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

import json
from collections import defaultdict

import numpy as np

from ..config import LTR_MIN_HISTORY_ROWS
from ..pipeline.features import FEATURE_COLS, to_feature_row
from ..schema import EnrichedListing
from ..store import db as _db
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


def labels_from_history(con, min_rows: int = LTR_MIN_HISTORY_ROWS):
    """The real, outcome-supervised labels. Each crawl is a query group; a
    listing's graded relevance comes from what the market actually did to it,
    not from the rules:

      * disappeared (sold) soon after being seen -> high grade (sold fast = good
        deal); the faster it vanished, the higher (4 next-crawl ... 1 slow);
      * still listed many crawls later -> low grade (1 lingering, 0 stale);
      * too fresh to have a resolved outcome -> excluded (censored).

    Features are the listing's snapshot AT that crawl (from feature_log), so the
    label (a *future* disappearance) is predicted from past state -- temporal
    supervision, not leakage. Returns (X, y, group, keys) or None when there
    isn't enough resolved history yet (caller falls back to bootstrap)."""
    rows = _db.load_feature_log(con)
    if not rows:
        return None
    crawls = sorted({r[0] for r in rows})
    if len(crawls) < 2:                      # need >=1 observed transition
        return None
    idx = {ts: i for i, ts in enumerate(crawls)}
    last_i = len(crawls) - 1

    present: dict[int, set] = defaultdict(set)
    feat_at: dict[tuple, str] = {}
    seen_at: dict[str, list] = defaultdict(list)
    src_of: dict[str, str] = {}
    crawled_src: dict[int, set] = defaultdict(set)   # sources active per crawl
    for ts, key, data in rows:
        ci = idx[ts]
        present[ci].add(key)
        feat_at[(ci, key)] = data
        seen_at[key].append(ci)
        src = src_of.get(key) or json.loads(data).get("source")
        src_of[key] = src
        crawled_src[ci].add(src)
    last_seen = {k: max(v) for k, v in seen_at.items()}
    first_seen = {k: min(v) for k, v in seen_at.items()}

    X, y, group, keys = [], [], [], []
    for ci in range(last_i):                 # crawls with a future to observe
        examples = []
        for key in present[ci]:
            ls = last_seen[key]
            # "disappeared" only counts as SOLD if the listing's source was
            # actually re-crawled afterwards; a source that stopped crawling
            # (e.g. quota-exhausted Marketcheck) just leaves censored rows.
            src = src_of[key]
            source_recrawled = any(src in crawled_src[j] for j in range(ls + 1, last_i + 1))
            if ls < last_i and source_recrawled:     # gone while source still crawled => sold
                gap = ls - ci                # extra crawls it survived after ci
                grade = 4 if gap <= 0 else 3 if gap == 1 else 2 if gap == 2 else 1
            elif ls >= last_i:               # still listed at the latest crawl
                alive = last_i - first_seen[key] + 1
                if alive >= 6:
                    grade = 0                # stale
                elif alive >= 4:
                    grade = 1                # slow mover
                else:
                    continue                 # censored: too fresh to judge
            else:
                continue                     # source stopped crawling -> censored
            examples.append((key, grade))
        # LambdaRank needs >=2 docs and >=2 distinct grades per query to learn
        if len(examples) >= 2 and len({g for _, g in examples}) >= 2:
            for key, grade in examples:
                l = EnrichedListing(**json.loads(feat_at[(ci, key)]))
                row = to_feature_row(l)
                X.append([row[c] for c in FEATURE_COLS])
                y.append(grade)
                keys.append(key)
            group.append(len(examples))

    if not group or sum(group) < min_rows:
        return None
    return np.array(X, dtype=float), np.array(y, dtype=int), group, keys
