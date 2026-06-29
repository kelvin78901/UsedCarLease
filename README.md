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
lock.

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
the model is trained and serving on first boot. Swap in `labels_from_history`
once crawls accumulate.

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

It runs the offline `seed` adapter by default. For live data, set
`ALR_ADAPTERS=leasehackr,swapalease,leasetrader` in `docker-compose.yml`
(scrapers need outbound network and respect each site's terms — this is a
personal-use system).

---

## API

| Method | Path | Notes |
|---|---|---|
| GET  | `/stats` | market pulse: active count, median/min effective $/mo, body mix, active ranker |
| GET  | `/top_deals` | full 4-stage rank; query params `budget, bodies, want_awd, want_lux, min_mpm, pref_states, top_k` |
| POST | `/recommend` | same, JSON body (`PrefBody`) |
| GET  | `/vehicle/{vin}` | one decoded + scored listing |
| POST | `/reload` | re-read snapshot + model from disk |
| GET  | `/health` | liveness + counts |

```bash
curl "localhost:8000/top_deals?budget=700&bodies=EV,SUV&want_awd=true&top_k=5"
```

---

## Adding a source (the plugin point)

The pipeline never changes — you write one adapter that yields `RawListing`:

```python
from alr.adapters.base import BaseAdapter, adapter
from alr.schema import RawListing

@adapter("mysource")
class MySourceAdapter(BaseAdapter):
    def fetch(self):
        r = self.client.get("https://...")
        for row in parse(r.text):
            yield RawListing(source="mysource", source_id=row.id,
                             make=row.make, monthly=row.price, ...)
```

Add `mysource` to `ALR_ADAPTERS` and it's in the next crawl. The same contract
is why this generalizes past cars — a `zillow` or `ebay` adapter reuses the
entire normalize → rank → serve stack.

Included adapters: `leasehackr` (Discourse `.json` API, real), `swapalease` /
`leasetrader` (httpx + selectolax, real), `cars` (Playwright, optional), `seed`
(deterministic offline generator for dev/CI).

---

## Project layout

```
alr/
  schema.py            Raw → Normalized → Enriched → Scored (Pydantic contract)
  config.py            env-driven config
  seed.py              deterministic offline data (mirrors the dashboard)
  adapters/            base + registry, leasehackr, swapalease, leasetrader, cars, seed
  enrich/nhtsa.py      vPIC VIN decode + spec catalog fallback
  pipeline/
    normalize.py  dedup.py  features.py (effective-cost engine)  run.py (orchestrator)
  rank/
    rules.py           Pareto frontier + interpretable score
    pipeline.py        4-stage rank (filter → pareto → score → personalize)
    ltr.py             LambdaMART train + scorer
    labels.py          graded-relevance label builder (bootstrap + history)
  store/db.py          DuckDB: current snapshot + history
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
