# AutoLeaseRank

A market-intelligence platform that aggregates vehicle lease listings, computes
the **true effective cost** of each deal, and ranks them with **Learning-to-Rank**
instead of heuristic sorting. Ships with a connected dashboard, a FastAPI
backend, a LambdaMART ranker, and a plugin adapter layer so the same engine can
later point at any marketplace, not just cars.

```
http://localhost:8000      ← dashboard + API after `docker compose up`
```

---

## Why this exists

Lease listings are scattered across platforms that expose different fields, and
the advertised monthly payment hides one-time fees, incentives, and transfer
costs. Sorting by monthly payment is misleading. AutoLeaseRank puts every
listing on one honest axis — **effective monthly cost** — and then learns to
order deals by quality.

```
effective_monthly = monthly
                  + (drive_off + disposition_fee + transfer_fee
                     + acquisition_fee − seller_incentive) / months_remaining
```

A seller incentive (cash to assume the lease) is negative cost. This single
number is the backbone of every downstream feature.

---

## Architecture

```
   Adapters (plugin)          leasehackr · swapalease · leasetrader · cars · seed
        │  RawListing
        ▼
   Normalize ─► Dedup ─► Enrich (NHTSA vPIC + spec catalog)
        │                         │  EnrichedListing
        ▼                         ▼
   Feature engine (Polars)   effective cost · msrp discount · segment edge
        │
        ▼
   DuckDB  ── current snapshot  +  append-only history (for self-labeling)
        │
        ▼
   Ranking pipeline   S1 hard filter ─► S2 Pareto frontier ─► S3 score ─► S4 personalize
        │                                                   (LambdaMART or rules)
        ▼
   FastAPI  /top_deals /recommend /stats /vehicle  ──►  Dashboard
```

Single-process by design: DuckDB is an embedded store, so the crawl scheduler
runs inside the API process rather than a second container fighting for the file
lock. (One writer only — never run `uvicorn --workers >1`.)

---

## Scale, concurrency & honest data volume

The crawl is fully **async**: all adapters fetch concurrently (`asyncio.gather`),
each bounded by its own `asyncio.Semaphore` (`ALR_*_CONCURRENCY`) with tenacity
retry/backoff on 429/5xx; one dead source can't kill the run. VIN enrichment is
**batched** (vPIC `DecodeVINValuesBatch`, ≤50/call) and **cached** in DuckDB
(`vin_cache`), so repeat crawls spend no vPIC traffic on known VINs. Marketcheck
runs a concurrent **sweep** over `(zip × make × price-band)` slices with a global
row budget enforced at append time, deduped by VIN downstream.

Concurrency makes the crawl fast, but it can't manufacture inventory. Honest
free-source ceiling (no paid tier, no proxies), **measured**: a single fast
Marketcheck-Free sweep yields **~3–4k distinct** — the Free tier **rate-limits
the burst (429)** so most of a 500-call sweep fails after retries (raise
`ALR_MC_DELAY` / lower `ALR_MC_CONCURRENCY` to throttle a slower sweep across the
month toward the ~25k theoretical 500-call cap). The lease-transfer forums add
only a few hundred. So realistic free volume is **~3–4k per burst, ~1–3k
repeatable**; 10k+ doesn't exist on free sources. After a Free burn the quota is
spent, so **source-scoped snapshots** keep the swept inventory served while
leasehackr keeps refreshing. Reaching 10k–100k *repeatably* needs **Marketcheck
Standard** (the sweep code already supports it — just widen
`ALR_MC_ZIPS/MAKES/PRICE_BANDS`) and/or a Playwright + proxy cluster.

---

## The ranking, honestly

This is framed as Learning-to-Rank, but LTR needs relevance labels and you have
none on day one. So the system is built to earn them:

1. **Rules first.** An interpretable score (market edge + Pareto bonus +
   freshness − instability) ranks deals from the first crawl. Defensible, no
   training needed.
2. **Self-collected labels.** Every crawl appends to a `history` table. A listing
   that disappears fast was a good deal; one that lingers, drops price, or gets
   reposted was not. That diff is graded relevance — real outcome supervision.
3. **Model.** `LightGBM LambdaRank` (LambdaMART) trains on those labels, with each
   market snapshot as a query group, optimizing NDCG. It generalizes past the
   hand-tuned rule weights.

Out of the box the trainer **bootstraps** labels from the rule ranker (each body
segment is a query group, listings graded 0–4 by within-segment deal score) so
the model is trained and serving on first boot. **Once ≥2 crawls of history
accumulate, `train_ltr.py` automatically switches to `labels_from_history`** —
each crawl is a query group and a listing's grade comes from what the market
actually did (disappeared fast = sold = high grade; lingering / price-cut =
low), predicted from its snapshot *before* the outcome (temporal supervision, no
leakage). Retained feature snapshots (`feature_log`, pruned to the last
`ALR_FEATURE_LOG_KEEP` crawls) make sold listings trainable after they vanish.

The dashboard always shows the interpretable 0–99 score; when a model is loaded
it drives the *ordering* (normalized to the same scale so personalization stays
comparable).

---

## Quickstart (local)

```bash
pip install -e .          # or: make install
python scripts/seed_db.py # seed the pipeline offline (no network)  → make seed
python scripts/train_ltr.py                                          # → make train
uvicorn alr.api.main:app --port 8000                                 # → make api
# open http://localhost:8000
```

## Quickstart (docker)

```bash
docker compose up --build   # seeds + trains on first boot, serves on :8000
```

It crawls **live by default** (`leasehackr,marketcheck`; put your Marketcheck key
in a gitignored `.env`) — an initial crawl runs on boot, then every
`ALR_CRAWL_INTERVAL_MIN`, retraining daily. Set `ALR_ADAPTERS=seed` for pure
offline. Scrapers respect each site's terms — this is a personal-use system.

---

## API

| Method | Path | Notes |
|---|---|---|
| GET  | `/stats` | market pulse: active count, median/min effective $/mo, body mix, active ranker |
| GET  | `/top_deals` | full 4-stage rank; query params `budget, bodies, want_awd, want_lux, min_mpm, max_months, pref_states, top_k`. **`bodies` empty = all types** (no filter); the dashboard renders one chip per real body type from `/stats.by_body`, all on by default. `max_months` defaults to 120 so financed used cars (72mo term) aren't excluded. |
| POST | `/recommend` | same, JSON body (`PrefBody`) |
| GET  | `/vehicle/{vin}` | one decoded + scored listing |
| POST | `/reload` | re-read snapshot + model from disk |
| GET  | `/health` | liveness + counts |

```bash
curl "localhost:8000/top_deals?budget=700&bodies=EV,SUV&want_awd=true&top_k=5"
```

---

## Adding a source (the plugin point)

The pipeline never changes — you write one **async** adapter that returns
`RawListing`s. Network calls go through `self.aget_json`, which bounds
concurrency with the adapter's semaphore and retries 429/5xx with backoff:

```python
from alr.adapters.base import BaseAdapter, adapter
from alr.schema import RawListing

@adapter("mysource")
class MySourceAdapter(BaseAdapter):
    concurrency = 5            # in-flight request cap for this source

    async def fetch(self) -> list[RawListing]:
        data = await self.aget_json("https://.../search.json")
        return [RawListing(source="mysource", source_id=row["id"],
                           make=row["make"], monthly=row["price"])
                for row in data["results"]]
```

Playwright/browser adapters keep their synchronous code and bridge with
`asyncio.to_thread(self._fetch_blocking)` (the sync Playwright API can't run
inside the event loop). Add `mysource` to `ALR_ADAPTERS` and it's in the next
crawl. The same contract is why this generalizes past cars — a `zillow` or
`ebay` adapter reuses the entire normalize → rank → serve stack.

Included adapters: `leasehackr` (Discourse `.json`, real; private-transfers board
by default — regional "marketplace" boards are broker ads with ~0 real transfers,
opt in via `ALR_LH_AUTODISCOVER=1`; polite rate-limited with a deal-sheet quality
gate), `marketcheck` (used-car inventory API, real; concurrent zip/make/price
sweep), `cars` (Cars.com used-car inventory, real — parses the page's embedded
`srp_results` JSON for vin/price/mileage/body/CPO, paginated), `swapalease` /
`leasetrader` (Playwright + stealth; reachable but their placeholder selectors
are stale → emit 0 today), `seed` (offline generator for dev/CI). Concurrent
headless-browser launches are serialized by a lock; Chromium runs via
`channel="chromium"` (the default headless shell segfaults in some containers).

The dashboard splits **Leases / Used cars / All** (tabs → `listing_type`): used
cars (marketcheck + cars) lead with sale price + an "est. finance pmt @7.5%/72mo"
label; leases keep the real effective-$/mo logic. Used cars carry a **CPO** badge
+ a "CPO only" filter; `/stats` reports `by_type` and `used_cpo`.

---

## Project layout

```
alr/
  schema.py            Raw → Normalized → Enriched → Scored (Pydantic contract)
  config.py            env-driven config
  seed.py              deterministic offline data (mirrors the dashboard)
  adapters/            async base + registry; leasehackr, marketcheck, swapalease, leasetrader, cars, seed
  enrich/nhtsa.py      batched vPIC VIN decode (+ DuckDB vin_cache) + spec catalog fallback
  pipeline/
    normalize.py  dedup.py  features.py (effective-cost engine)  run.py (async orchestrator)
  rank/
    rules.py           Pareto frontier + interpretable score
    pipeline.py        4-stage rank (filter → pareto → score → personalize)
    ltr.py             LambdaMART train + scorer
    labels.py          graded-relevance labels: bootstrap (cold start) + labels_from_history (outcomes)
  store/db.py          DuckDB: current snapshot + history + vin_cache + feature_log
  api/main.py          FastAPI + static dashboard mount
  scheduler.py         standalone scheduler (separate-store setups)
scripts/               seed_db.py, train_ltr.py
web/index.html         connected dashboard (React + Recharts, no build step)
```

---

## Evaluation

LTR is trained with NDCG@5/@10 as the eval metric. Offline ranking quality
(NDCG, MAP, MRR, Precision@k) is computed per snapshot; once history accrues,
online metrics (did top-ranked deals actually sell faster?) close the loop and
become the next round of labels.

## Notes

- Scraper selectors live in one block per adapter — site redesigns are a quick
  fix, not a rewrite.
- Effective-cost weights and personalization bonuses are explicit and tunable in
  `rank/pipeline.py` and `rank/rules.py`.
- This is a personal research tool; respect the terms and `robots.txt` of any
  site you point it at.
