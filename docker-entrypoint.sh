#!/usr/bin/env bash
set -e

# First boot: no snapshot -> seed the pipeline offline so the API has data.
if [ ! -f "${ALR_DB_PATH}" ]; then
  echo "[entrypoint] no snapshot found; seeding pipeline..."
  python scripts/seed_db.py
fi

# First boot: no model -> train LambdaMART from the snapshot.
if [ ! -f "${ALR_LTR_MODEL}" ]; then
  echo "[entrypoint] no LTR model found; training..."
  python scripts/train_ltr.py
fi

exec "$@"
