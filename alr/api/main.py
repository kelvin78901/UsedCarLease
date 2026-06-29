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
from ..rank.pipeline import Prefs, rank
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
    S.scorer = LTRScorer.load()


@app.on_event("startup")
def startup():
    _load()
    # DuckDB is single-process, so the crawl scheduler runs inside the API
    # process rather than a second container fighting over the file lock.
    import os
    if os.getenv("ALR_INPROCESS_SCHEDULER", "0") == "1":
        from apscheduler.schedulers.background import BackgroundScheduler
        from ..config import CRAWL_INTERVAL_MIN
        from ..pipeline.run import crawl

        def _job():
            try:
                crawl()       # uses ENABLED_ADAPTERS
                _load()       # refresh in-memory snapshot + model
                print("[scheduler] snapshot refreshed")
            except Exception as e:
                print(f"[scheduler] crawl failed: {e}")

        sched = BackgroundScheduler(timezone="UTC")
        sched.add_job(_job, "interval", minutes=CRAWL_INTERVAL_MIN,
                      id="crawl", max_instances=1, coalesce=True)
        sched.start()
        print(f"[scheduler] in-process crawl every {CRAWL_INTERVAL_MIN} min")


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
    for l in S.listings:
        bodies[l.body] = bodies.get(l.body, 0) + 1
    return {
        "active": len(S.listings),
        "median_effective": median(effs),
        "min_effective": min(effs),
        "by_body": bodies,
        "ranker": "ltr" if S.scorer else "rules",
    }


class PrefBody(BaseModel):
    budget: float = 1400
    bodies: list[str] = ["SUV", "Sedan", "EV"]
    want_awd: bool = False
    want_lux: bool = False
    min_mpm: int = 0
    max_months: int = 48
    pref_states: list[str] = []
    top_k: int = 100


def _do_rank(p: Prefs):
    res = rank(S.listings, p, ltr_scorer=S.scorer)
    return {"counts": res.counts,
            "deals": [d.model_dump() for d in res.ranked]}


@app.get("/top_deals")
def top_deals(
    budget: float = Query(1400),
    bodies: str = Query("SUV,Sedan,EV"),
    want_awd: bool = Query(False),
    want_lux: bool = Query(False),
    min_mpm: int = Query(0),
    pref_states: str = Query(""),
    top_k: int = Query(100),
):
    p = Prefs(
        budget=budget,
        bodies=set(b for b in bodies.split(",") if b) | {"Unknown"},
        want_awd=want_awd, want_lux=want_lux, min_mpm=min_mpm,
        pref_states=set(s for s in pref_states.split(",") if s),
        top_k=top_k,
    )
    return _do_rank(p)


@app.post("/recommend")
def recommend(body: PrefBody):
    p = Prefs(
        budget=body.budget, bodies=set(body.bodies) | {"Unknown"},
        want_awd=body.want_awd, want_lux=body.want_lux, min_mpm=body.min_mpm,
        max_months=body.max_months, pref_states=set(body.pref_states),
        top_k=body.top_k,
    )
    return _do_rank(p)


@app.get("/listing/{key:path}")
def listing(key: str):
    match = next((l for l in S.listings if l.listing_key == key), None)
    if not match:
        raise HTTPException(404, f"no listing {key}")
    res = rank([match], Prefs(), ltr_scorer=S.scorer)
    return res.ranked[0].model_dump() if res.ranked else match.model_dump()


@app.get("/vehicle/{vin}")
def vehicle(vin: str):
    con = _db.connect()
    l = _db.get_by_vin(con, vin)
    con.close()
    if not l:
        raise HTTPException(404, f"no listing with vin {vin}")
    res = rank([l], Prefs(), ltr_scorer=S.scorer)
    return res.ranked[0].model_dump() if res.ranked else l.model_dump()


# serve the dashboard at / (mounted last so /api routes above take precedence)
_web = ROOT / "web"
if _web.exists():
    app.mount("/", StaticFiles(directory=str(_web), html=True), name="web")
