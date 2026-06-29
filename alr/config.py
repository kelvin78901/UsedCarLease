"""Runtime config. Everything overridable by env so the same image runs in
docker, locally, or in CI."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("ALR_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("ALR_DB_PATH", DATA_DIR / "autoleaserank.duckdb"))
LTR_MODEL_PATH = Path(os.getenv("ALR_LTR_MODEL", DATA_DIR / "ltr_lambdamart.txt"))

# adapters to run on each crawl (comma-separated). Live scrapers require network.
ENABLED_ADAPTERS = os.getenv("ALR_ADAPTERS", "leasehackr,marketcheck").split(",")

# NHTSA vPIC is free + keyless
VPIC_BASE = os.getenv("ALR_VPIC_BASE", "https://vpic.nhtsa.dot.gov/api")
VPIC_BATCH_SIZE = int(os.getenv("ALR_VPIC_BATCH_SIZE", "50"))  # DecodeVINValuesBatch cap

CRAWL_INTERVAL_MIN = int(os.getenv("ALR_CRAWL_INTERVAL_MIN", "30"))
HTTP_TIMEOUT = float(os.getenv("ALR_HTTP_TIMEOUT", "20"))

# --- concurrency (P1) -------------------------------------------------------
# per-source in-flight request caps (politeness / rate limits) + retry attempts.
LH_CONCURRENCY = int(os.getenv("ALR_LH_CONCURRENCY", "2"))   # Discourse 429s easily
LH_DELAY = float(os.getenv("ALR_LH_DELAY", "0.5"))          # politeness sleep per request
LH_RETRIES = int(os.getenv("ALR_LH_RETRIES", "3"))
MC_CONCURRENCY = int(os.getenv("ALR_MC_CONCURRENCY", "3"))   # keep low: free tier
MC_DELAY = float(os.getenv("ALR_MC_DELAY", "0"))            # per-request sleep; raise for big Free sweeps (429s)
MC_RETRIES = int(os.getenv("ALR_MC_RETRIES", "3"))
VPIC_CONCURRENCY = int(os.getenv("ALR_VPIC_CONCURRENCY", "2"))
USER_AGENT = os.getenv(
    "ALR_USER_AGENT",
    "AutoLeaseRank/0.4 (+personal-research; respect robots.txt)",
)

# ranking default to LTR if a trained model exists, else heuristic rules
USE_LTR = os.getenv("ALR_USE_LTR", "auto")  # auto | rules | ltr

# --- outcome-label training (history) ---------------------------------------
# how many recent crawls of feature snapshots to retain for outcome labelling,
# and the min resolved rows before train_ltr uses history instead of bootstrap.
FEATURE_LOG_KEEP = int(os.getenv("ALR_FEATURE_LOG_KEEP", "60"))
LTR_MIN_HISTORY_ROWS = int(os.getenv("ALR_LTR_MIN_HISTORY", "20"))
