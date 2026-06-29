"""Train the LTR model from the current snapshot and save it. After this the API
ranks with the model (set ALR_USE_LTR=auto, the default) instead of the rules.

    python scripts/train_ltr.py
"""
import numpy as np

from alr.store import db as _db
from alr.rank.ltr import train
from alr.rank.labels import bootstrap, labels_from_history


def main():
    con = _db.connect()
    listings = _db.load_current(con)
    if len(listings) < 10:
        con.close()
        raise SystemExit("snapshot too small; run scripts/seed_db.py or a crawl first")

    # Prefer real outcome labels once enough crawl history has accumulated;
    # fall back to the rules-bootstrap cold start otherwise.
    hist = labels_from_history(con)
    con.close()
    if hist is not None:
        X, y, group, _ = hist
        source = "OUTCOME labels from history (sold-fast = relevant)"
    else:
        X, y, group, _ = bootstrap(listings)
        source = "cold-start bootstrap labels (insufficient history)"
    print(f"training on {source}: {len(y)} rows across {len(group)} query groups, "
          f"label dist={np.bincount(y, minlength=5).tolist()}")
    booster = train(X, y, group)
    print(f"saved model. best ndcg@10 trees={booster.num_trees()}")
    # quick sanity: top-5 by model score
    from alr.rank.ltr import LTRScorer
    scorer = LTRScorer.load()
    ranked = sorted(listings, key=scorer, reverse=True)[:5]
    print("\ntop 5 by LTR:")
    for l in ranked:
        print(f"  {l.make:9} {l.model:16} ${l.effective_monthly:>4.0f}/mo  "
              f"edge={l.value_edge*100:+.0f}%  hp={l.hp}")


if __name__ == "__main__":
    main()
