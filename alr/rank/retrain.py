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
    covered = len(hist[1]) if hist is not None else 0
    if hist is not None and covered >= 0.4 * len(listings):
        X, y, group, _ = hist
        source = "OUTCOME labels from history (sold-fast = relevant)"
    else:
        X, y, group, _ = bootstrap(listings)
        source = (f"value_edge bootstrap labels (history covered only "
                  f"{covered}/{len(listings)})")
    train(X, y, group)
    return source, y, group
