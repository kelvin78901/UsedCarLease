# Deploying AutoLeaseRank

Single process, single-file DuckDB. No DB server, no cluster. Pick one path.

## Critical constraints (read first)
- **One worker only.** DuckDB is embedded/single-process. Never run `uvicorn --workers N`
  (N>1), gunicorn multi-worker, or `--reload` in prod — multiple processes fight over
  the DuckDB file lock and the scheduler would double-run.
- **libomp** is required by LightGBM (Mac: `brew install libomp`; Docker image: handled).
- The scheduler **crawls** every interval; it does **not** retrain. Retrain on a cron
  (see deploy/retrain.sh) once real data accumulates.
- On the Marketcheck free tier, keep `ALR_CRAWL_INTERVAL_MIN>=60` and `ALR_MC_MAX_ROWS`
  modest so you don't exhaust the quota.

## Option A — Docker (recommended, any always-on box)
```bash
cp deploy/.env.prod.example .env        # edit: Marketcheck key, zip, adapters
docker compose --env-file .env up -d --build
# -> http://<host-ip>:8000 ; logs: docker compose logs -f
```
First boot seeds + trains a bootstrap model, then the scheduler crawls real data.
Data persists in the `alr-data` named volume.

## Option B — macOS always-on, no Docker (good for a Mac mini)
```bash
pip install -e . && brew install libomp
python scripts/seed_db.py && python scripts/train_ltr.py      # bootstrap
# edit the two PATHs + key in deploy/com.autoleaserank.plist, then:
cp deploy/com.autoleaserank.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.autoleaserank.plist
# -> http://localhost:8000 (and http://<mac-ip>:8000 on your LAN)
```

## Option C — Linux VPS, no Docker (systemd)
Same as B but with a systemd unit running:
`uvicorn alr.api.main:app --host 0.0.0.0 --port 8000 --workers 1`
with the env vars from .env.prod.example, plus `WatchdogSec`/`Restart=always`.

## Daily retrain (all options)
```bash
crontab -e
# 4am daily: retrain on the latest snapshot and hot-reload the API
0 4 * * *  /Users/kelvin/Downloads/autoleaserank/deploy/retrain.sh >> /tmp/alr-retrain.log 2>&1
```

## Remote access (optional)
Easiest secure path: install Tailscale on the host, then reach it from anywhere at
`http://<tailscale-name>:8000`. Avoid exposing port 8000 to the public internet
directly (no auth on the API).
