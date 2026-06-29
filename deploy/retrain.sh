#!/usr/bin/env bash
# Retrain the LambdaMART ranker on the latest snapshot. Run daily via cron once
# real data has accumulated. Safe to run while the API is up (reads DB, writes
# the model file; API picks it up on next /reload or restart).
set -e
cd "$(dirname "$0")/.."
python scripts/train_ltr.py
# tell a running API to reload the new model (ignore error if API is down)
curl -s -X POST http://localhost:8000/reload >/dev/null 2>&1 || true
echo "retrained + reloaded $(date)"
