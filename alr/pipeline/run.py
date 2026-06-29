"""End-to-end crawl orchestration. One function, one job: turn whatever the
enabled adapters can fetch into a clean, enriched, stored snapshot the API can
serve. Designed to be called by the scheduler every N minutes or by hand."""
from __future__ import annotations

import httpx

from ..config import ENABLED_ADAPTERS, HTTP_TIMEOUT, USER_AGENT
from ..adapters.base import get_adapters
# import adapter modules so @adapter decorators register them
from ..adapters import leasehackr, swapalease, cars, marketcheck  # noqa: F401
from ..enrich.nhtsa import enrich
from ..schema import EnrichedListing
from . import normalize as _norm
from . import dedup as _dedup
from . import features as _feat
from ..store import db as _db


def crawl(adapters: list[str] | None = None, persist: bool = True) -> list[EnrichedListing]:
    names = adapters or ENABLED_ADAPTERS
    insts = get_adapters(names)

    raw = []
    for a in insts:
        try:
            got = list(a.fetch())
            print(f"[crawl] {a.name}: {len(got)} listings")
            raw.extend(got)
        except Exception as e:
            print(f"[crawl] {a.name} crashed: {e}")
        finally:
            a.close()

    normalized = [n for n in (_norm.normalize(r) for r in raw) if n]
    deduped = _dedup.dedup(normalized)

    with httpx.Client(timeout=HTTP_TIMEOUT,
                      headers={"User-Agent": USER_AGENT}) as client:
        enriched_norm = [enrich(l, client) for l in deduped]

    enriched = _feat.build_features(enriched_norm)
    print(f"[crawl] {len(raw)} raw -> {len(normalized)} normalized "
          f"-> {len(deduped)} unique -> {len(enriched)} enriched")

    if persist and enriched:
        con = _db.connect()
        _db.save_snapshot(con, enriched)
        con.close()
        print("[crawl] snapshot persisted")
    return enriched


if __name__ == "__main__":
    crawl()
