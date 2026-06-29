# HANDOFF — AutoLeaseRank → Claude Code

You are taking over an existing, working project. **Read this whole file before
touching code.** It tells you what is already verified (don't redo it), what is
broken, the owner's goal, and — critically — the honest constraints so you don't
waste time chasing a target that's gated by data access, not by code.

The owner's goal, verbatim: **"全量跑，几万几十万的数据，所有的源并发"**
→ ingest tens of thousands to hundreds of thousands of listings, all sources
crawled concurrently.

---

## 0. STATUS — updated 2026-06-29 (P0 + P1 + P2-bonus shipped)

The concurrency/scale work in §6 is **done** (git: baseline → P0 → P1 → P2-bonus,
one commit per phase). What changed since this doc was first written:

- **P0 (done).** `hp=0` fixed: batch vPIC `DecodeVINValuesBatch` + a DuckDB
  `vin_cache` + precedence **adapter → vPIC → catalog** (the old catalog
  make-level fallback was clobbering real data and mislabeling gas cars as EV).
  Leasehackr is multi-board with **live category autodiscovery** (no hardcoded
  ids). Marketcheck passes build hp/year through; `normalize` carries
  `_hp/_ev/_year`. Sold-deal/title cleanup confirmed.
- **P1 (done).** Fully async: `BaseAdapter` → `httpx.AsyncClient` (client +
  semaphore built in `aopen()`, never `__init__`); `run.py` → `asyncio.gather`
  with per-source semaphores + tenacity retry; Playwright adapters bridged via
  `asyncio.to_thread`; Marketcheck concurrent `(zip×make×band)` sweep with a
  global row budget sliced at append. Measured: Leasehackr fetch 9.1s→2.4s
  (conc 1→5). New deps: `tenacity`.
- **P2-bonus (done).** `rank/labels.py:labels_from_history` — real outcome
  labels (sold-fast = relevant) from a retained `feature_log`; `train_ltr.py`
  auto-uses it once ≥2 crawls exist, else bootstrap. **Schedule `deploy/retrain.sh`
  (or `python scripts/train_ltr.py`) periodically** so the model switches to
  outcome labels as history accrues.
- **P2 PAID — NOT done, owner-gated.** Marketcheck Standard ($749/mo) + richer
  sweep, and Playwright + residential-proxy scraping. Owner decided free sources
  (~1–3k repeatable) suffice for now; revisit Standard on demand. **Do not start
  without explicit sign-off.**

**Honest volume ceiling (free, no proxies):** one-time ~15–20k (≈95% a single
Marketcheck-Free quota burn), repeatable ~1–3k. 10k–100k repeatable is gated by
data access, not code — see §5.

**Doc corrections from live testing:** the vPIC batch payload is
`VIN,year;VIN,year` (comma between VIN/year, semicolon between records), **not**
the `vin;year|...` written in §4.1 — the wrong format silently returns garbage.

**Findings from a real Docker run (leasehackr + marketcheck live):**
- Leasehackr's regional **marketplace** boards (`c/marketplace/{california/14,
  northeast/15,midwest/16,south/17,west/18}`) are **broker/dealer ad threads —
  ~0 real transfers.** Only `c/private-transfers/12` has genuine lease takeovers.
  So autodiscovery is now **off by default** (set `ALR_LH_AUTODISCOVER=1` to
  include the ad boards). The earlier "~1–2k from all boards" estimate was
  optimistic; real Leasehackr transfer inventory ≈ the private-transfers board
  (a few hundred). Marketcheck remains the only real volume.
- Discourse **rate-limits hard (429)** under concurrency=5/no-delay. Defaults are
  now polite: `ALR_LH_CONCURRENCY=2` + `ALR_LH_DELAY=0.5` (per-request sleep held
  in the semaphore slot), and only the pages MAX_TOPICS needs are fetched. A real
  run then took ~55s with zero 429s.
- The body parser now **demands a real deal-sheet fingerprint** (maturity date /
  effective miles / mileage / transfer fee / calc link) plus sane bounds
  (monthly ∈ [50,6000], MSRP ≥ 8000, `$84k`→84000) — without it, broker ads with
  `monthly=$1` / `MSRP=$4` ranked #1. End-to-end live in Docker now serves clean,
  real transfers + Marketcheck inventory through the 4-stage LTR rank.

**Live deploy round (real crawl常驻 + Free full-sweep burn):**
- Compose crawls **live by default** (key from gitignored `.env`); the in-process
  scheduler runs an **initial crawl on boot** + every 180min and **retrains
  daily** (`rank/retrain.py`, auto outcome-vs-bootstrap labels). Two more parse
  bugs fixed (color-as-make → alias table; trailing `|`/`/`).
- **Snapshots are now source-scoped** (`store/db.py`): a crawl replaces only the
  sources it returned (+ purges `seed`), so a big one-time sweep survives later
  small crawls. `labels_from_history` is source-aware to match.
- **Marketcheck Free full sweep, measured:** a fast 96-slice / ~520-call burst
  yielded only **~3,300 distinct** — the Free tier **rate-limits hard (429)**, so
  most calls fail after retries. The "~15–20k one-time" estimate was optimistic;
  realistic free burst ≈ 3–4k (throttle via `ALR_MC_DELAY` / lower
  `ALR_MC_CONCURRENCY` to spread a slower sweep toward the 500-call/25k cap). The
  ~3.3k is now **served live** (`/stats active≈3305`); compose runs leasehackr-only
  for the burn month so source-scoped preserves it. Restore `leasehackr,marketcheck`
  next month / on Standard. Owner declined paid (Standard/proxies) for now.
- **UI/filter fix:** Stage 1 was hiding ~all of the swept inventory two ways — the
  dashboard hardcoded only 3 body chips (SUV/Sedan/EV) and `Prefs.max_months=48`
  excluded every financed used car (72mo term). Now: body chips render
  **dynamically from `/stats.by_body`** (all on by default; empty `bodies` = no
  filter) and `max_months` defaults to 120. Result: default dashboard
  `FILTERED 7 → 3124`, "Ranked deals" 7 → 100, Pickup/Wagon/Van/etc. now visible.

**New sources + used-car/lease split + CPO round:**
- **Cars.com works** (`adapters/cars.py` rewrite): parses the embedded
  `srp_results` JSON (vin/price/mileage/body/drivetrain/`cpoIndicator`), waits for
  the cards, paginates. ~72 listings/3 pages, with real price/odometer/CPO. The
  old `div.vehicle-card` selector was stale + it used `networkidle`. Dealer
  city/state are best-effort (JSON has only the dealer zip; DOM↔JSON id join is
  unreliable) — price/odometer/cpo are solid.
- **Playwright stability:** Chromium's default `headless_shell` **segfaults** in
  this Docker/Apple-Silicon container → launch with `channel="chromium"`. And a
  `BROWSER_LOCK` serializes concurrent browser launches (3 at once crash). Added
  `playwright-stealth`. Swapalease/LeaseTrader are **reachable** (pages load) but
  their placeholder selectors are stale → honest emit 0 (NOT WAF-blocked; wiring
  them up = the same embedded-JSON reverse-engineering done for cars.com).
- **Used cars vs leases:** `listing_type` (all|lease|used; used = marketcheck/cars,
  derived from source so the pre-existing 3.3k classifies without re-crawl).
  Dashboard tabs + used-car terminology (sale price / est-finance label) + CPO
  badge + "CPO only" filter. schema gained `cpo/odometer/price/dealer_city`. The
  preserved marketcheck 3.3k predate price/odometer (show est-$/mo fallback); new
  cars listings have them. `/stats` adds `by_type` + `used_cpo`.
- Live now: `/stats` active ~3370, by_type used ~3363 / lease 7, used_cpo ~2;
  persistent `ALR_ADAPTERS=leasehackr,cars,swapalease,leasetrader` (marketcheck
  omitted, quota spent, preserved via source-scoped). PAID still owner-gated.

Env knobs added: `ALR_LH_CONCURRENCY/RETRIES`, `ALR_MC_CONCURRENCY/RETRIES`,
`ALR_VPIC_CONCURRENCY/BATCH_SIZE`, `ALR_LH_AUTODISCOVER`, `ALR_LH_MAX_PAGES`,
`ALR_MC_ZIPS/MAKES/PRICE_BANDS/PER_QUERY_CAP`, `ALR_FEATURE_LOG_KEEP`,
`ALR_LTR_MIN_HISTORY` — all documented in `.env.example`. The Marketcheck key is
kept in a gitignored `.env` (never committed). The rest of this doc is the
original handoff; §4 issues 1–4 are now fixed.

---

## 1. What this project is

A learning-to-rank market-intelligence platform for car lease/used-car deals.
Pipeline: adapters (plugin sources) → normalize → dedup → enrich → feature
engineering (effective-cost engine) → DuckDB → 4-stage ranking (hard filter →
Pareto frontier → **LightGBM LambdaMART** score → personalization) → FastAPI +
a no-build React dashboard.

Stack: Python 3.10, httpx, selectolax, Polars, DuckDB, LightGBM, FastAPI,
uvicorn, APScheduler. Runs in Docker (single container) or via launchd. Owner
currently runs it in Docker on a Mac.

There is **no LLM anywhere** in scoring — it is LightGBM LambdaMART with SHAP
(`pred_contrib`) explanations. Keep it that way.

## 2. Repo map (the files you'll touch most)

```
alr/
  schema.py              Pydantic contract: RawListing→Normalized→Enriched→Scored
  config.py              env-driven config (all ALR_* vars)
  seed.py                deterministic synthetic data (offline dev), 54 rows
  adapters/
    base.py              BaseAdapter (sync httpx.Client) + @adapter registry  ← CONCURRENCY WORK STARTS HERE
    leasehackr.py        Discourse .json, body-sheet parser. WORKS on real data.
    marketcheck.py       Marketcheck API (used-car inventory). Structured. Key-gated.
    swapalease.py        Swapalease + LeaseTrader, Playwright. UNVERIFIED (WAF/JS).
    cars.py              Cars.com Playwright stub + `seed` adapter.
  enrich/nhtsa.py        vPIC VIN decode (1 call/VIN) + small spec catalog
  pipeline/
    normalize.py  dedup.py  features.py(effective-cost engine)  run.py(orchestrator, SEQUENTIAL)
  rank/
    rules.py  pipeline.py(4-stage)  ltr.py(LambdaMART + SHAP)  labels.py(bootstrap+history labels)
  store/db.py            DuckDB: `current` snapshot + append-only `history`
  api/main.py            FastAPI + static dashboard mount + in-process scheduler
  scheduler.py           standalone scheduler (separate-store use)
scripts/                 seed_db.py, train_ltr.py, probe.py(per-adapter diagnostics)
web/index.html           dashboard (React UMD + Babel, hash-routed sub-page)
deploy/                  .env.prod.example, launchd plist, retrain.sh, (DEPLOY.md, SOURCES.md at root)
```

## 3. VERIFIED WORKING — do not rebuild

- Full offline pipeline (seed → normalize → enrich → features → DuckDB → rank → API → dashboard).
- LambdaMART training + SHAP score drivers; dashboard renders them; sub-page (`#/v/<key>`) + outbound `↗` links to real listing URLs.
- **Leasehackr on REAL data**: pulled 300 topics across 10 pages → 60 rankable listings, persisted, model retrained. Parser reads the post-body deal sheet (`MSRP:`, `Monthly payment:`, `Cash due:`, `Maturity date:`, `Transfer fee:`, `Effective miles per month:`), make/state from tags, months from maturity date.
- **NHTSA vPIC**: real VIN decode confirmed.
- **Marketcheck**: parsing + price→monthly amortization confirmed against a doc-shaped sample. Live calls need `ALR_MARKETCHECK_KEY` (owner has Free tier).

## 4. KNOWN ISSUES (fix these first, they're real)

1. **`hp=0` for real listings.** Real models (Equinox EV, Sierra, Mach-E) aren't in the tiny `enrich/nhtsa.py` spec catalog and the post body has no HP, so the Pareto hp-axis collapses. Fix by (a) **batch vPIC** (`DecodeVINValuesBatch`, ≤50 VINs/call) when a VIN exists, and (b) preferring adapter-provided build data (Marketcheck `build` already has body/drivetrain/fuel; add HP if available). Do NOT call vPIC once-per-VIN at scale — see §6.
2. **Title noise / sold deals (partially fixed, needs rebuild).** `leasehackr.py` now strips `[Transfer COMPLETE]`/`NC ONLY:` prefixes and skips sold deals (`RE_SOLD`). If you still see them, the Docker image wasn't rebuilt — `docker compose up -d --build`.
3. **Single Leasehackr category.** Only `c/private-transfers/12`. There are regional marketplace categories too (e.g. `c/marketplace/northeast/15`). Make `ALR_LH_CATEGORY` accept a comma-separated list and crawl them concurrently.
4. **brotli decode bug (fixed in code).** `base.py` Accept-Encoding must NOT include `br` unless the `brotli` package is installed, or httpx throws `utf-8 codec can't decode 0xc1`. Currently set to `gzip, deflate`. Keep it, or add `brotli` to deps.

## 5. THE GOAL & THE HONEST CONSTRAINT (read carefully)

The owner wants **10k–100k+ listings, all sources concurrent**. Concurrency is
the easy part. The hard truth about VOLUME:

| Source | Realistic active volume | Ceiling reason |
|---|---|---|
| Leasehackr private transfers | ~a few hundred | It's a forum; that's all the inventory that exists |
| Leasehackr + all regional marketplace categories | ~1–2k | Same — community size |
| **Marketcheck (used-car inventory)** | **10k–1M+** | This is the only realistic path to big numbers |
| Swapalease / LeaseTrader | ~10–25k listed, but WAF/JS-walled | Need Playwright + proxies; fragile |
| Cars.com / Autotrader / CarGurus | huge, but hard anti-bot | Need a Playwright cluster + rotating residential proxies |

**Conclusion to convey to the owner:** you cannot reach tens of thousands from
the lease-transfer sources — that inventory doesn't exist. Big numbers come from
**used-car inventory APIs (Marketcheck) or scraping marketplaces at scale with
anti-bot infrastructure.** And Marketcheck's tiers gate it hard:

- **Free**: 500 calls/month, 1500-row pagination cap/query, 100mi radius. → at most ~1500 rows/query; quota dies fast. Good for a demo, not for 100k.
- **Basic $299/mo**: 5000 calls/mo, still 1500-row cap/query, 100mi.
- **Standard $749/mo**: unlimited calls, 500mi radius. → realistic 100k path via many queries.

To get 100k from Marketcheck you must **sweep many sub-queries** (per-zip,
per-make, per-price-band) because each query is capped at 1500 rows, then dedup
by VIN. That requires a paid tier and a query-planner. **Flag the cost to the
owner before building** — this is a budget decision, not just code.

## 6. WORK PLAN (prioritized)

### P0 — make it correct & multi-category (cheap, high value)
- Rebuild image so §4.2 fixes take effect; confirm sold deals gone.
- `leasehackr.py`: `ALR_LH_CATEGORY` → comma-separated list; crawl each category, all pages, concurrently. Add a few regional categories to the default.
- Fix `hp=0`: batch vPIC (`POST /vehicles/DecodeVINValuesBatch/`, format `vin1;model_year1|vin2|...`, ≤50/call) in `enrich/nhtsa.py`; only call for listings that have a VIN and no HP from build data. Cache decoded VINs in DuckDB to avoid re-decoding.

### P1 — concurrency (the owner's explicit ask)
- Convert adapters to **async**: `BaseAdapter` → `httpx.AsyncClient`; `fetch()` → `async def` yielding via `async for` or returning a list. Keep the sync registry.
- `pipeline/run.py`: run all enabled adapters with `asyncio.gather`, each adapter fetching its pages/topics concurrently under a **per-source `asyncio.Semaphore`** (politeness + rate limits: Leasehackr modest e.g. 5–8 concurrent w/ small delay; Marketcheck respect tier req/s — Free 5/s, Standard 40/s).
- Add **retry + exponential backoff** (e.g. `tenacity`) per request; treat 429/503 as backoff, not failure.
- Marketcheck adapter: concurrent pagination across `start` offsets up to the 1500 cap, then a **query sweep** (list of zips/filters from env) to exceed it; dedup by VIN downstream.

### P1 — enrichment & DB at scale
- vPIC: batch + cache (above). Skip entirely for listings whose adapter already provides body/hp/drivetrain (Marketcheck does).
- `store/db.py`: batch inserts (you already use executemany — make sure crawl writes once per run, not per listing). DuckDB handles 100k–1M rows fine for analytics; keep it. **Do NOT add a second writer process** (DuckDB is single-writer; the in-process scheduler is the only writer by design).
- `dedup.py`: current O(n) dict dedup is fine at 100k. Add cross-source VIN dedup (same car on Cars.com + Marketcheck).

### P2 — more real volume (only if owner funds it)
- Marketcheck paid tier + query-planner sweep → the realistic road to 100k.
- Playwright-based adapters for Swapalease/LeaseTrader/Cars.com **with a proxy pool** (e.g. residential proxies) and anti-bot handling. This is real infra; scope and price it explicitly. Owner has stated personal use — confirm before building a scraping farm.

### P2 — model quality
- Once `history` table has multiple crawls, switch LTR labels from `rank/labels.py:bootstrap` (rules-derived) to outcome labels (sold-fast = relevant) via a new `labels_from_history`. This is the real upgrade from "model copies the rules" to "model learns the market."

## 7. Concurrency design notes (so you don't fight the framework)

- DuckDB is embedded/single-process: **one writer only**. Run crawl as the sole writer; the API opens short read connections. Never `uvicorn --workers >1`.
- If the owner ever needs concurrent multi-process writes (they likely won't), that's the trigger to migrate `store/db.py` to Postgres — but at 100k analytical rows DuckDB is the right tool; don't migrate prematurely.
- Politeness is non-negotiable for forum/scrape sources — bound concurrency per host, set delays, honor robots.txt. Marketcheck is an API; just respect its documented req/s for the tier.

## 8. Verify like this

```bash
python scripts/probe.py leasehackr      # fill-rate table per field
python scripts/probe.py marketcheck      # needs ALR_MARKETCHECK_KEY
python -m alr.pipeline.run               # full crawl→store
python scripts/train_ltr.py              # retrain
uvicorn alr.api.main:app --port 8000     # dashboard at /
```
In Docker: `docker compose exec autoleaserank env ALR_ADAPTERS=... python -m alr.pipeline.run`.

## 9. Gotchas / don'ts

- Don't add `br` to Accept-Encoding without the brotli package.
- Don't call vPIC once per VIN at scale (rate-limited, slow). Batch + cache.
- Don't run multiple DB writers / multiple uvicorn workers.
- Don't reintroduce title-based parsing as the primary field source for Leasehackr — the body deal-sheet is the structured source; title is fallback only.
- Don't promise the owner 100k records from lease-transfer sources — it doesn't exist there. Be honest that volume = Marketcheck paid tier and/or scraping infra.
- Keep scoring on LightGBM + SHAP. No LLM scoring.

— end of handoff —
