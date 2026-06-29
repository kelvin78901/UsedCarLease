"""FastAPI surface. Serves the ranked deals to the dashboard and exposes the
pipeline as a clean REST API. Loads the current snapshot from DuckDB and the LTR
model if one is trained; otherwise ranks with the interpretable rules.

Endpoints:
  GET /health
  GET /stats                      market pulse: counts, median eff $/mo
  GET /top_deals                  full 4-stage rank with query-param prefs
  POST /recommend                 same, body = full Prefs JSON
  GET /vehicle/{vin}              one decoded + scored listing
  POST /reload                    re-read snapshot + model from disk
"""
from __future__ import annotations

from statistics import median

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import ROOT
from ..rank.pipeline import Prefs, rank, is_used
from ..rank.ltr import LTRScorer
from ..store import db as _db

app = FastAPI(title="AutoLeaseRank", version="0.4")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class _State:
    listings = []
    scorer = None


S = _State()


def _load():
    con = _db.connect()
    S.listings = _db.load_current(con)
    con.close()
    # Backfill used cars crawled before the price/MSRP-gate fixes: recover the
    # sale price from the (price-derived) monthly, and drop junk MSRP/discount.
    from ..adapters.marketcheck import price_from_monthly, _valid_msrp
    for l in S.listings:
        if is_used(l):
            if l.price <= 0 and l.monthly > 0:
                l.price = price_from_monthly(l.monthly)
            if _valid_msrp(l.msrp, l.price):       # real used discount off MSRP
                l.msrp_discount_pct = round((l.msrp - l.price) / l.msrp * 100)
            else:                                   # junk MSRP -> hide it
                l.msrp = 0.0
                l.msrp_discount_pct = 0.0
    S.scorer = LTRScorer.load()


@app.on_event("startup")
def startup():
    _load()
    # DuckDB is single-process, so the crawl scheduler runs inside the API
    # process rather than a second container fighting over the file lock.
    import os
    if os.getenv("ALR_INPROCESS_SCHEDULER", "0") == "1":
        from datetime import datetime, timedelta, timezone
        from apscheduler.schedulers.background import BackgroundScheduler
        from ..config import CRAWL_INTERVAL_MIN
        from ..pipeline.run import crawl

        def _job():
            try:
                crawl()       # uses ENABLED_ADAPTERS (the real sources, not seed)
                _load()       # refresh in-memory snapshot + model
                print("[scheduler] snapshot refreshed")
            except Exception as e:
                print(f"[scheduler] crawl failed: {e}")

        def _retrain_job():
            try:
                from ..rank.retrain import retrain
                source, y, _ = retrain()
                _load()       # serve the new model
                print(f"[scheduler] retrained on {source} ({len(y)} rows) + reloaded")
            except Exception as e:
                print(f"[scheduler] retrain failed: {e}")

        now = datetime.now(timezone.utc)
        sched = BackgroundScheduler(timezone="UTC")
        # initial real crawl ~immediately on boot, then every interval -- so the
        # seed snapshot from first-boot is replaced by live data right away.
        sched.add_job(_job, "interval", minutes=CRAWL_INTERVAL_MIN,
                      id="crawl", max_instances=1, coalesce=True, next_run_time=now)
        # daily retrain; auto-switches to outcome labels once history accrues.
        sched.add_job(_retrain_job, "interval", hours=24, id="retrain",
                      max_instances=1, coalesce=True,
                      next_run_time=now + timedelta(minutes=3))
        sched.start()
        print(f"[scheduler] in-process crawl every {CRAWL_INTERVAL_MIN} min, "
              f"retrain daily (initial crawl now)")


@app.post("/reload")
def reload():
    _load()
    return {"loaded": len(S.listings), "ltr": S.scorer is not None}


@app.get("/health")
def health():
    return {"ok": True, "listings": len(S.listings), "ltr": S.scorer is not None}


@app.get("/stats")
def stats():
    if not S.listings:
        return {"active": 0}
    effs = [l.effective_monthly for l in S.listings]
    bodies: dict[str, int] = {}
    makes: dict[str, int] = {}
    for l in S.listings:
        bodies[l.body] = bodies.get(l.body, 0) + 1
        makes[l.make] = makes.get(l.make, 0) + 1
    used = [l for l in S.listings if is_used(l)]
    years = [l.year for l in used if l.year > 0]
    return {
        "active": len(S.listings),
        "median_effective": median(effs),
        "min_effective": min(effs),
        "by_body": bodies,
        "by_make": dict(sorted(makes.items(), key=lambda kv: -kv[1])),
        "year_range": [min(years), max(years)] if years else [0, 0],
        "by_type": {"used": len(used), "lease": len(S.listings) - len(used)},
        "used_cpo": sum(1 for l in used if l.cpo),
        "ranker": "ltr" if S.scorer else "rules",
    }


class PrefBody(BaseModel):
    budget: float = 1400
    bodies: list[str] = []          # empty = all body types (no filter)
    listing_type: str = "all"       # all | lease | used
    cpo_only: bool = False
    want_awd: bool = False
    want_lux: bool = False
    min_mpm: int = 0
    max_months: int = 120
    states: list[str] = []          # hard filter: only these states
    pref_states: list[str] = []     # soft +8 bonus
    sort: str = "score"             # score | price_asc | price_desc | newest | distance
    near: str = ""                  # reference state for sort=distance
    q: str = ""                     # free-text search
    makes: list[str] = []           # hard filter: only these makes
    year_min: int = 0
    year_max: int = 0
    odo_max: int = 0
    price_min: float = 0
    price_max: float = 0
    awd_only: bool = False
    top_k: int = 100


def _do_rank(p: Prefs):
    res = rank(S.listings, p, ltr_scorer=S.scorer)
    return {"counts": res.counts,
            "deals": [d.model_dump() for d in res.ranked]}


@app.get("/top_deals")
def top_deals(
    budget: float = Query(1400),
    bodies: str = Query(""),           # empty = all body types (no filter)
    listing_type: str = Query("all"),  # all | lease | used
    cpo_only: bool = Query(False),
    want_awd: bool = Query(False),
    want_lux: bool = Query(False),
    min_mpm: int = Query(0),
    max_months: int = Query(120),
    states: str = Query(""),           # hard filter: only these states
    pref_states: str = Query(""),      # soft +8 bonus
    sort: str = Query("score"),        # score|price_asc|price_desc|newest|distance
    near: str = Query(""),             # reference state for sort=distance
    q: str = Query(""),                # free-text search
    makes: str = Query(""),            # hard filter: only these makes
    year_min: int = Query(0),
    year_max: int = Query(0),
    odo_max: int = Query(0),
    price_min: float = Query(0),
    price_max: float = Query(0),
    awd_only: bool = Query(False),
    top_k: int = Query(100),
):
    p = Prefs(
        budget=budget,
        bodies=set(b for b in bodies.split(",") if b),
        listing_type=listing_type, cpo_only=cpo_only,
        want_awd=want_awd, want_lux=want_lux, min_mpm=min_mpm,
        max_months=max_months,
        states=set(s for s in states.split(",") if s),
        pref_states=set(s for s in pref_states.split(",") if s),
        sort_by=sort, near=near,
        q=q, makes=set(m for m in makes.split(",") if m),
        year_min=year_min, year_max=year_max, odo_max=odo_max,
        price_min=price_min, price_max=price_max, awd_only=awd_only,
        top_k=top_k,
    )
    return _do_rank(p)


@app.post("/recommend")
def recommend(body: PrefBody):
    p = Prefs(
        budget=body.budget, bodies=set(body.bodies),
        listing_type=body.listing_type, cpo_only=body.cpo_only,
        want_awd=body.want_awd, want_lux=body.want_lux, min_mpm=body.min_mpm,
        max_months=body.max_months, states=set(body.states),
        pref_states=set(body.pref_states), sort_by=body.sort, near=body.near,
        q=body.q, makes=set(body.makes), year_min=body.year_min,
        year_max=body.year_max, odo_max=body.odo_max, price_min=body.price_min,
        price_max=body.price_max, awd_only=body.awd_only,
        top_k=body.top_k,
    )
    return _do_rank(p)


def _scored_in_context(key=None, match=None):
    """Score one listing within the FULL snapshot so its percentile score/rank is
    meaningful (ranking it alone would always yield the bottom percentile)."""
    res = rank(S.listings, Prefs(budget=10**9, max_months=10**6,
                                 top_k=len(S.listings) or 1), ltr_scorer=S.scorer)
    hit = next((d for d in res.ranked
                if (key and d.listing_key == key) or (match and d.vin and d.vin == match.vin)), None)
    return (hit or match).model_dump() if (hit or match) else None


@app.get("/listing/{key:path}")
def listing(key: str):
    match = next((l for l in S.listings if l.listing_key == key), None)
    if not match:
        raise HTTPException(404, f"no listing {key}")
    return _scored_in_context(key=key, match=match)


@app.get("/vehicle/{vin}")
def vehicle(vin: str):
    con = _db.connect()
    l = _db.get_by_vin(con, vin)
    con.close()
    if not l:
        raise HTTPException(404, f"no listing with vin {vin}")
    return _scored_in_context(key=l.listing_key, match=l)


# serve the dashboard at / (mounted last so /api routes above take precedence)
_web = ROOT / "web"
if _web.exists():
    app.mount("/", StaticFiles(directory=str(_web), html=True), name="web")
