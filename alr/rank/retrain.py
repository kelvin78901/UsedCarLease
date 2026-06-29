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
        # match the serving path: age-realistic finance + peer value for used cars
        # (the stored value_edge predates the fix), so labels grade on the same
        # signal the API serves.
        from ..pipeline.features import recompute_used_market
        recompute_used_market(listings)
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
        used_outcome = True
    else:
        X, y, group, _ = bootstrap(listings)
        why = "degenerate label dist" if degenerate else f"history covered only {covered}/{len(listings)}"
        source = f"value_edge bootstrap labels ({why})"
        used_outcome = False
    train(X, y, group)

    # Post-train sanity: the model must NOT rank more-expensive-for-its-kind cars
    # above cheaper ones. Volatile scrape sources (swapalease/leasetrader re-scraped
    # each crawl) make luxury leases look "sold fast", inverting the lease value
    # order even when the global label dist looks healthy. If the trained model is
    # anti-correlated with value_edge on leases, fall back to the value_edge
    # bootstrap (which ranks by value directly).
    if used_outcome:
        corr = _value_corr(listings, lease_only=True)
        if corr < 0.05:
            print(f"[retrain] outcome labels inverted lease value (corr={corr:+.2f}) "
                  "-> value_edge bootstrap")
            X, y, group, _ = bootstrap(listings)
            train(X, y, group)
            source = f"value_edge bootstrap labels (outcome inverted lease value, corr={corr:+.2f})"
    return source, y, group


def _value_corr(listings, lease_only=False) -> float:
    """Pearson correlation between the freshly-trained model's score and value_edge.
    Positive = ranks cheaper-for-its-kind higher (good)."""
    import statistics
    from .ltr import LTRScorer
    rows = ([l for l in listings if not (getattr(l, "price", 0) and l.price > 0)]
            if lease_only else listings)
    if len(rows) < 10:
        return 1.0
    sc = LTRScorer.load()
    ss = [float(sc(l)) for l in rows]
    vv = [l.value_edge for l in rows]
    ms, mv = statistics.mean(ss), statistics.mean(vv)
    cov = sum((s - ms) * (v - mv) for s, v in zip(ss, vv))
    ds = sum((s - ms) ** 2 for s in ss) ** 0.5
    dv = sum((v - mv) ** 2 for v in vv) ** 0.5
    return cov / (ds * dv) if ds and dv else 0.0
