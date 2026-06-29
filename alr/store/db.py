"""DuckDB storage. Single-file analytical DB - no server to run, fast group-bys
for market features, and an append-only history table that is the substrate for
self-supervised LTR labels (a listing that vanishes fast was a good deal).

Two tables:
  current  - latest enriched snapshot the API serves from
  history  - one row per (crawl, listing): when we last saw it, its effective
             cost and stability signals. Diffing crawls tells us what sold.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb

from ..config import DB_PATH
from ..schema import EnrichedListing


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS current (
            listing_key VARCHAR PRIMARY KEY,
            body VARCHAR, effective_monthly DOUBLE, data JSON
        );
        CREATE TABLE IF NOT EXISTS history (
            crawl_ts TIMESTAMP, listing_key VARCHAR,
            effective_monthly DOUBLE, days_on_market INTEGER,
            price_drops INTEGER, favorites INTEGER
        );
    """)
    return con


def save_snapshot(con: duckdb.DuckDBPyConnection, listings: list[EnrichedListing]) -> None:
    ts = datetime.now(timezone.utc)
    con.execute("DELETE FROM current")
    cur_rows = [(l.listing_key, l.body, l.effective_monthly,
                 json.dumps(l.model_dump(), default=str)) for l in listings]
    con.executemany("INSERT INTO current VALUES (?, ?, ?, ?)", cur_rows)
    hist_rows = [(ts, l.listing_key, l.effective_monthly, l.days_on_market,
                  l.price_drops, l.favorites) for l in listings]
    con.executemany("INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)", hist_rows)


def load_current(con: duckdb.DuckDBPyConnection) -> list[EnrichedListing]:
    rows = con.execute("SELECT data FROM current").fetchall()
    return [EnrichedListing(**json.loads(r[0])) for r in rows]


def get_by_vin(con: duckdb.DuckDBPyConnection, vin: str) -> EnrichedListing | None:
    rows = con.execute(
        "SELECT data FROM current WHERE json_extract_string(data, '$.vin') = ?",
        [vin]).fetchall()
    return EnrichedListing(**json.loads(rows[0][0])) if rows else None
