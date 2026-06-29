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

CRAWL_INTERVAL_MIN = int(os.getenv("ALR_CRAWL_INTERVAL_MIN", "30"))
HTTP_TIMEOUT = float(os.getenv("ALR_HTTP_TIMEOUT", "20"))
USER_AGENT = os.getenv(
    "ALR_USER_AGENT",
    "AutoLeaseRank/0.4 (+personal-research; respect robots.txt)",
)

# ranking default to LTR if a trained model exists, else heuristic rules
USE_LTR = os.getenv("ALR_USE_LTR", "auto")  # auto | rules | ltr
