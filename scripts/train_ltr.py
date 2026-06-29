"""Train the LTR model from the current snapshot and save it. After this the API
ranks with the model (set ALR_USE_LTR=auto, the default) instead of the rules.

    python scripts/train_ltr.py
"""
import numpy as np

from alr.store import db as _db
from alr.rank.retrain import retrain


def main():
    try:
        source, y, group = retrain()
    except ValueError as e:
        raise SystemExit(str(e))
    print(f"training on {source}: {len(y)} rows across {len(group)} query groups, "
          f"label dist={np.bincount(y, minlength=5).tolist()}")
    print("saved model.")
    # quick sanity: top-5 by model score
    from alr.rank.ltr import LTRScorer
    con = _db.connect()
    listings = _db.load_current(con)
    con.close()
    scorer = LTRScorer.load()
    ranked = sorted(listings, key=scorer, reverse=True)[:5]
    print("\ntop 5 by LTR:")
    for l in ranked:
        print(f"  {l.make:9} {l.model:16} ${l.effective_monthly:>4.0f}/mo  "
              f"edge={l.value_edge*100:+.0f}%  hp={l.hp}")


if __name__ == "__main__":
    main()
