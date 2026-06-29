"""End-to-end crawl orchestration. One function, one job: turn whatever the
enabled adapters can fetch into a clean, enriched, stored snapshot the API can
serve. Designed to be called by the scheduler every N minutes or by hand.

All adapters fetch concurrently (asyncio.gather); each is isolated so one dead
source never kills the crawl. Enrichment (batched vPIC) is async too. The single
DuckDB write happens once at the end -- one writer process, by design."""
from __future__ import annotations

import asyncio
import time

import httpx

from ..config import ENABLED_ADAPTERS, HTTP_TIMEOUT, USER_AGENT
from ..adapters.base import get_adapters
# import adapter modules so @adapter decorators register them
from ..adapters import leasehackr, swapalease, cars, marketcheck  # noqa: F401
from ..enrich.nhtsa import enrich_all
from ..schema import EnrichedListing
from . import normalize as _norm
from . import dedup as _dedup
from . import features as _feat
from ..store import db as _db


async def crawl_async(adapters: list[str] | None = None,
                      persist: bool = True) -> list[EnrichedListing]:
    names = adapters or ENABLED_ADAPTERS
    insts = get_adapters(names)

    async def run_adapter(a) -> list:
        try:
            async with a:                       # aopen() -> client+sem in this loop
                got = await a.fetch()
            print(f"[crawl] {a.name}: {len(got)} listings")
            return got
        except Exception as e:                  # one dead source can't kill the crawl
            print(f"[crawl] {a.name} crashed: {e}")
            return []

    t0 = time.monotonic()
    batches = await asyncio.gather(*(run_adapter(a) for a in insts))
    fetch_s = time.monotonic() - t0
    raw = [r for b in batches for r in b]

    normalized = [n for n in (_norm.normalize(r) for r in raw) if n]
    deduped = _dedup.dedup(normalized)

    # One DuckDB connection for the whole tail: vin_cache read/write during
    # enrichment AND the final snapshot write. Single writer, by design.
    con = _db.connect() if persist else None
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                     headers={"User-Agent": USER_AGENT}) as client:
            enriched_norm = await enrich_all(deduped, client, con=con)

        enriched = _feat.build_features(enriched_norm)
        total_s = time.monotonic() - t0
        print(f"[crawl] {len(raw)} raw -> {len(normalized)} normalized "
              f"-> {len(deduped)} unique -> {len(enriched)} enriched "
              f"| fetch {fetch_s:.1f}s, total {total_s:.1f}s")

        if persist and enriched:
            _db.save_snapshot(con, enriched)
            print("[crawl] snapshot persisted")
    finally:
        if con is not None:
            con.close()
    return enriched


def crawl(adapters: list[str] | None = None, persist: bool = True) -> list[EnrichedListing]:
    """Synchronous entry point (scheduler threads, scripts). Spins a fresh event
    loop per call -- safe from APScheduler worker threads; never call this from
    inside a running loop (e.g. a FastAPI async route)."""
    return asyncio.run(crawl_async(adapters, persist))


if __name__ == "__main__":
    crawl()
