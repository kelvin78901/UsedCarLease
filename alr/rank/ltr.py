"""Learning-to-Rank with LightGBM's LambdaRank (LambdaMART) objective.

Why LambdaRank and not regression: we don't care about predicting a deal's
price, we care about ordering deals correctly within a market snapshot. The
"query" is a crawl snapshot; the group is all listings in that snapshot; the
label is graded relevance (0-4) derived from outcomes.

Cold start: there are no outcome labels on day one, so build_labels.py bootstraps
graded relevance from the interpretable ranker + simulated sell-through, then
swaps in real labels as crawl history accumulates (sold-fast = relevant,
long-on-market / reposted / price-cut = not). This is the honest version of the
"learning to rank" story: rules first, self-collected labels, then a model.
"""
from __future__ import annotations

from pathlib import Path

import lightgbm as lgb
import numpy as np

from ..config import LTR_MODEL_PATH
from ..schema import EnrichedListing
from ..pipeline.features import FEATURE_COLS, to_feature_row


def train(X: np.ndarray, y: np.ndarray, group: list[int],
          out_path: Path = LTR_MODEL_PATH) -> lgb.Booster:
    """X: (n, n_features), y: graded relevance 0..4, group: rows per query."""
    dtrain = lgb.Dataset(X, label=y, group=group,
                         feature_name=FEATURE_COLS, free_raw_data=False)
    params = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "ndcg_eval_at": [5, 10],
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 10,
        "lambdarank_truncation_level": 20,
        "label_gain": [0, 1, 3, 7, 15],
        "verbose": -1,
    }
    booster = lgb.train(params, dtrain, num_boost_round=300)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out_path))
    return booster


class LTRScorer:
    """Loads a trained booster and scores a single listing. Returned by
    load() or None if no model exists, so the rank pipeline can fall back to
    rules transparently."""

    # human-readable labels for the model's features, for explanations
    LABELS = {
        "effective_monthly": "effective $/mo", "monthly": "monthly payment",
        "msrp": "MSRP", "msrp_discount_pct": "MSRP discount", "value_edge": "vs-market edge",
        "hp": "horsepower", "months_remaining": "term left", "miles_per_month": "miles/mo",
        "remaining_miles": "miles left", "drive_off": "drive-off", "transfer_fee": "transfer fee",
        "acquisition_fee": "acq fee", "disposition_fee": "disposition fee",
        "seller_incentive": "seller incentive", "days_on_market": "days on market",
        "price_drops": "price drops", "favorites": "interest", "luxury": "luxury",
        "ev": "EV", "awd": "AWD",
    }

    def __init__(self, booster: lgb.Booster):
        self.booster = booster

    @classmethod
    def load(cls, path: Path = LTR_MODEL_PATH) -> "LTRScorer | None":
        if not Path(path).exists():
            return None
        try:
            return cls(lgb.Booster(model_file=str(path)))
        except Exception as e:
            print(f"[ltr] failed to load model: {e}")
            return None

    def _row(self, l: EnrichedListing) -> np.ndarray:
        r = to_feature_row(l)
        return np.array([[r[c] for c in FEATURE_COLS]], dtype=float)

    def __call__(self, l: EnrichedListing) -> float:
        return float(self.booster.predict(self._row(l))[0])

    def contributions(self, l: EnrichedListing, top: int = 4):
        """SHAP-style per-feature contributions to this listing's score
        (LightGBM pred_contrib). Returns the top features by |impact| as
        [(label, signed_contribution), ...] - the model's own 'why'."""
        contrib = self.booster.predict(self._row(l), pred_contrib=True)[0]
        # last column is the base/expected value; drop it
        pairs = [(FEATURE_COLS[i], float(contrib[i])) for i in range(len(FEATURE_COLS))]
        pairs.sort(key=lambda kv: abs(kv[1]), reverse=True)
        return [(self.LABELS.get(k, k), round(v, 3)) for k, v in pairs[:top] if abs(v) > 1e-6]
