"""Retrain the LambdaMART ranker from the current snapshot, preferring real
outcome labels (sold-fast = relevant) once enough crawl history has accumulated,
falling back to the rules bootstrap otherwise. Shared by scripts/train_ltr.py and
the in-process scheduler so both pick labels the same way."""
from __future__ import annotations

from ..store import db as _db
from .ltr import train
from .labels import bootstrap, labels_from_history


def retrain(con=None):
    """Train + save the model. Returns (label_source, y, group). Reuses an open
    DuckDB connection if given (so the scheduler stays single-process)."""
    own = con is None
    con = con or _db.connect()
    try:
        listings = _db.load_current(con)
        if len(listings) < 10:
            raise ValueError("snapshot too small; seed or crawl first")
        hist = labels_from_history(con)
    finally:
        if own:
            con.close()

    # Use outcome labels only if they cover a meaningful fraction of the snapshot.
    # When most listings are a frozen sweep (preserved, not re-crawled), history
    # resolves only a handful of churning rows -> the LambdaMART model overfits
    # them and can even INVERT the value ordering on the unseen bulk. The
    # value_edge bootstrap (every listing) ranks homogeneous used cars stably.
    # Also reject DEGENERATE outcome labels (one grade dominates). A source-scoped
    # re-crawl makes the replaced VINs look like they "sold" -> ~all grade 4 ->
    # a meaningless, often rank-inverting signal. Healthy graded relevance is
    # spread across grades.
    covered = len(hist[1]) if hist is not None else 0
    degenerate = False
    if hist is not None and covered:
        from collections import Counter
        degenerate = max(Counter(hist[1]).values()) / covered > 0.7
    if hist is not None and covered >= 0.4 * len(listings) and not degenerate:
        X, y, group, _ = hist
        source = "OUTCOME labels from history (sold-fast = relevant)"
    else:
        X, y, group, _ = bootstrap(listings)
        why = "degenerate label dist" if degenerate else f"history covered only {covered}/{len(listings)}"
        source = f"value_edge bootstrap labels ({why})"
    train(X, y, group)
    return source, y, group
