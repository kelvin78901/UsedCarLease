# AutoLeaseRank
<img width="1427" height="882" alt="image" src="https://github.com/user-attachments/assets/8a62c537-8acf-4312-93e9-2d1cb362ad93" />
<img width="1428" height="882" alt="image" src="https://github.com/user-attachments/assets/36998cc2-a66b-449a-b329-1044181dc095" />

A market-intelligence platform that aggregates **used-car listings and lease
transfers** from several sources, puts every deal on one honest cost axis, and
ranks them with **Learning-to-Rank** (LambdaMART + SHAP) instead of heuristic
sorting. Ships with a connected no-build dashboard, a FastAPI backend, and a
plugin adapter layer so the same engine can point at any marketplace.

```
http://localhost:8000      ← dashboard + API after `docker compose up`
```

---

## Why this exists

Car listings are scattered across platforms that expose different fields, and the
advertised number hides the real cost — one-time fees and incentives on a lease
transfer, an unrealistic 72-month amortization on a 20-year-old used car. Sorting
by the sticker number is misleading. AutoLeaseRank puts every listing on one
honest axis and then learns to order deals by **value relative to comparable
cars**, not by absolute cheapness.

**Lease transfers** carry their true monthly cost:

```
effective_monthly = monthly
                  + (drive_off + disposition_fee + transfer_fee
                     + acquisition_fee − seller_incentive) / months_remaining
```

A seller incentive (cash to assume the lease) is negative cost. **Used cars** get
an age-realistic finance payment (the term shrinks with age — no fake $104/mo on a
2006), then both are scored against their **peer group**, below.

---

## Architecture

```
   Adapters (plugin)     marketcheck · cars · leasehackr · swapalease · leasetrader · seed
        │  RawListing            (browser adapters run in isolated subprocesses)
        ▼
   Normalize ─► Dedup ─► Enrich (NHTSA vPIC batch + cache · spec catalog)
        │  canonical make/model               │  EnrichedListing
        ▼                                      ▼
   Feature engine        effective cost · age-realistic finance · PEER-GROUP value
        │
        ▼
   DuckDB  ── current snapshot (source-scoped)  +  append-only history (self-labeling)
        │
        ▼
   Ranking pipeline   S1 filter ─► S2 Pareto frontier ─► S3 LambdaMART score ─► S4 personalize
        │
        ▼
   FastAPI  /top_deals /recommend /stats /listing /vehicle  ──►  Dashboard
```

Single-process by design: DuckDB is an embedded store, so the crawl scheduler runs
**inside** the API process rather than a second container fighting for the file
lock. One writer only — never run `uvicorn --workers >1`.

---

## The value signal (what "a good deal" means)

Absolute cheapness is a trap: a depreciated 2006 beater is "90% off MSRP", and the
most expensive lease is never the best one. So value is **peer-relative** —
cheaper than comparable cars is good, more expensive is penalized:

- **Used cars** compare `effective $/mo` vs **make + body + 3-year-band** peers,
  minus an **age + high-mileage** penalty, on an **age-realistic finance term**
  (`72mo` when new → `24mo` floor). The board surfaces recent, reasonable-mileage
  cars priced low for their kind, not old depreciation.
- **Lease transfers** compare vs **same make + model + body** peers' median, with a
  body-level fallback when peers are sparse, plus a small drag on missing-power
  (0hp) listings. Cheaper-for-its-kind floats up; a +64%-vs-market Bentley does not.
- **Canonical make/model** at normalize time (`BMW` not `Bmw`, junk models dropped)
  so the *same brand from different sources lands in the same peer group*.

`segment_avg_effective` (the peer baseline) and `value_edge` (% vs that baseline)
are computed by `pipeline/features.recompute_used_market`, shared by the crawl, the
API serve path, and retraining so labels grade on exactly what the API shows.

---

## The ranking, honestly

LTR needs relevance labels you don't have on day one, so the system earns them:

1. **Bootstrap labels** grade every listing 0–4 by `value_edge` within its segment
   — the model is trained and serving from the first crawl.
2. **Outcome labels** (`labels_from_history`): once crawl history accrues, a
   listing that disappears fast was a good deal; one that lingers / price-cuts was
   not — graded from its snapshot *before* the outcome (no leakage). `feature_log`
   keeps sold listings trainable after they vanish.
3. **LightGBM LambdaRank** (LambdaMART) trains on those labels (each snapshot a
   query group, optimizing NDCG); **SHAP** `pred_contrib` gives every ranked deal a
   "why it ranks here". No LLM anywhere.

Three guards keep the auto-retrain from going wrong (all in `rank/retrain.py`):

- **Coverage gate** — use outcome labels only when history covers ≥40% of the
  snapshot, else the value_edge bootstrap (a frozen sweep resolves too few rows).
- **Degeneracy guard** — reject outcome labels when one grade dominates (>70%); a
  source-scoped re-crawl makes replaced rows look "sold", which inverts ranking.
- **Value-sanity check** — after training, if the model is *anti-correlated* with
  `value_edge` on leases (volatile scrape sources make luxury leases look "sold
  fast"), fall back to the bootstrap. This fires automatically and self-corrects.

The dashboard shows a 1–99 score (percentile of the model's ordering, so it spreads
even when the model clusters on near-identical inventory).

---

## Quickstart (docker)

```bash
docker compose up --build   # seeds + trains on first boot, serves on :8000
```

The in-process scheduler runs an initial crawl on boot, then **every
`ALR_CRAWL_INTERVAL_MIN` (default 3h)**, and **retrains daily**. **Auto-crawl runs
FREE sources only** (`leasehackr,swapalease,leasetrader,cars`). **Marketcheck is
deliberately NOT auto-crawled** — its 500-calls/mo Free quota would burn out in
hours and trip 429s, so it's a **manual, monthly-budgeted sweep** (below); its
swept inventory is preserved untouched by the source-scoped snapshot whenever the
free sources refresh. Set `ALR_ADAPTERS=seed` for pure offline.

**Manual Marketcheck sweep** (only when you have monthly quota — Free is 500/mo):

```bash
docker compose stop                     # free the DB lock (single-writer)
docker compose run --rm \
  -e ALR_ADAPTERS=marketcheck -e ALR_MC_ZIPS="20001,21201,22030,..." \
  -e ALR_MC_PRICE_BANDS="0-15000,15000-30000,30000-60000,60000-200000" \
  -e ALR_MC_RADIUS=100 -e ALR_MC_MAX_CALLS=170 -e ALR_MC_MAX_ROWS=10000 \
  -e ALR_MC_USED_THIS_MONTH=<calls already spent> \
  autoleaserank python -m alr.pipeline.run     # prints calls_used / cumulative~N/500
docker compose up -d                    # restart; auto-crawl preserves the sweep
```
Free tier caps `radius` at **100mi**; the sweep hard-stops at `ALR_MC_MAX_CALLS`.
Put your key in a gitignored `.env` (see `.env.example`); it never enters the repo.

## Quickstart (local, offline)

```bash
pip install -e .            # or: make install
python scripts/seed_db.py   # seed offline (no network)            → make seed
python scripts/train_ltr.py # train on the snapshot                → make train
uvicorn alr.api.main:app --port 8000                               → make api
```

---

## Data sources & honest volume

A live snapshot is roughly **~4k listings**: Marketcheck (used, a manual DMV/PA
sweep, ~3.4k, source-scoped so it persists between free crawls), Cars.com (used,
multi-metro, ~70), and the lease-transfer forums — leasehackr (~60), swapalease
(~520), leasetrader (~90–400, rate-limit sensitive). Concurrency makes the crawl
fast but can't manufacture inventory; free sources realistically yield single-digit
thousands. Reaching 10k–100k *repeatably* needs **Marketcheck Standard** (the sweep
code already supports it — widen `ALR_MC_ZIPS/MAKES/PRICE_BANDS`) and/or proxies.

| adapter | what it pulls |
|---|---|
| `marketcheck` | used-car inventory API; concurrent `zip × price-band` sweep, hard call cap + cumulative print, inverse-amortized sale price |
| `cars` | Cars.com used inventory via the page's embedded `srp_results` JSON (vin/price/mileage/body/CPO); multi-metro via `ALR_CARS_ZIPS` |
| `leasehackr` | Discourse `.json` private-transfers board; parses the deal-sheet body (msrp/monthly/fees/miles), occasionally a VIN |
| `swapalease` | per-make search pages, **+ incremental detail-page enrichment** (VIN / incentive / trim / odometer), capped per crawl |
| `leasetrader` | Angular `/search-results`, scroll-loaded; polite delay + exponential backoff (it rate-limits bursts) |
| `seed` | deterministic offline generator for dev/CI |

**Browser adapters run each scrape in an isolated subprocess** (`_pw_runner`),
serialized with a hard timeout — this container can't run multiple Playwright
lifecycles in one process, so a slow/blocked site emits 0 instead of hanging the
crawl. Extracted VINs feed the shared batched vPIC enrichment (hp/body/ev), cached
in DuckDB so repeat crawls spend no vPIC traffic on known VINs.

---

## Dashboard

No build step (React UMD + Babel from a CDN, single `web/index.html`). Tabs split
**Leases / Used cars / All** (`listing_type`). Used cars lead with sale price + an
age-realistic "est. finance" note; leases show real effective $/mo. Controls:
free-text **search** (`q`), **dynamic state chips** (from `/stats.by_state`, with a
**DMV** shortcut), **sort** (best / price / newest / state-proximity),
year/mileage/price range filters (used), make multi-select, **CPO**/AWD. Every
ranked deal links to a detail sub-page with its **SHAP** driver bars and a
**Pareto frontier** (cost vs power) scatter.

---

## API

| Method | Path | Notes |
|---|---|---|
| GET  | `/stats` | active count, median/min effective $/mo, `by_body` / `by_make` / `by_state` / `by_type` / `used_cpo`, `year_range` |
| GET  | `/top_deals` | full 4-stage rank. Params: `budget, bodies, listing_type, cpo_only, want_awd, want_lux, min_mpm, max_months, states, pref_states, sort, near, q, makes, year_min, year_max, odo_max, price_min, price_max, awd_only, top_k`. `listing_type`=`all\|lease\|used`; **`q`** = free-text AND search; **`states`/`makes`** = hard filters; **`sort`**=`score\|price_asc\|price_desc\|newest\|distance` (`distance` needs `near=<state>`, state-level — the data has no dealer lat/lng) |
| POST | `/recommend` | same, JSON body (`PrefBody`) |
| GET  | `/listing/{key}` · `/vehicle/{vin}` | one decoded + scored listing (scored within the full snapshot) |
| POST | `/reload` | re-read snapshot + model from disk |
| GET  | `/health` | liveness + counts |

```bash
curl "localhost:8000/top_deals?listing_type=used&q=mach-e&states=VA,MD&sort=price_asc&top_k=5"
```

---

## Adding a source (the plugin point)

Write one **async** adapter returning `RawListing`s; the pipeline never changes.
JSON sources go through `self.aget_json` (per-adapter semaphore + tenacity backoff
on 429/5xx). Browser sources implement a sync `_fetch_blocking()` and return
`await fetch_via_subprocess(self.name)` — `_pw_runner` runs it in its own process.
Add the name to `ALR_ADAPTERS` and it's in the next crawl. The same contract is why
this generalizes past cars — a `zillow` or `ebay` adapter reuses the whole stack.

---

## Project layout

```
alr/
  schema.py            Raw → Normalized → Enriched → Scored (Pydantic contract)
  config.py            env-driven config
  adapters/            async base + registry; marketcheck, cars, leasehackr,
                       swapalease+leasetrader, seed; _pw_runner (subprocess browser)
  enrich/nhtsa.py      batched vPIC VIN decode (+ DuckDB vin_cache) + spec catalog
  pipeline/
    normalize.py       canonical make/model + dedup-key; dedup.py
    features.py        effective-cost + age-realistic finance + peer-group value
    run.py             async crawl orchestrator
  rank/
    rules.py           Pareto frontier + interpretable score
    pipeline.py        4-stage rank (filter → pareto → score → personalize)
    ltr.py             LambdaMART train + scorer + SHAP contributions
    labels.py          bootstrap (value_edge) + labels_from_history (outcomes)
    retrain.py         coverage gate + degeneracy guard + value-sanity check
  store/db.py          DuckDB: current (source-scoped) + history + vin_cache + feature_log
  api/main.py          FastAPI + scheduler + static dashboard mount
scripts/               seed_db.py, train_ltr.py
tests/                 swapalease detail-parser unit test (+ fixture)
web/index.html         connected dashboard (React + Recharts, no build step)
```

---

## Notes

- Scoring stays LambdaMART + SHAP — no LLM. Value weights and penalties are
  explicit and tunable in `pipeline/features.py` and `rank/`.
- Scraper selectors live in one block per adapter — a site redesign is a quick fix.
- This is a personal research tool; respect each site's terms and `robots.txt`.
