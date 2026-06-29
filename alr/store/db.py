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

from ..config import DB_PATH, FEATURE_LOG_KEEP
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
        CREATE TABLE IF NOT EXISTS vin_cache (
            vin VARCHAR PRIMARY KEY,
            body VARCHAR, hp INTEGER, ev BOOLEAN, awd BOOLEAN,
            decoded_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS feature_log (
            crawl_ts TIMESTAMP, listing_key VARCHAR, data JSON
        );
    """)
    return con


def save_snapshot(con: duckdb.DuckDBPyConnection, listings: list[EnrichedListing]) -> None:
    if not listings:
        return  # a fully-failed crawl shouldn't wipe the last good snapshot
    ts = datetime.now(timezone.utc)
    # Source-scoped replace: only swap out the sources this crawl actually
    # returned, so a quota-exhausted source (e.g. Marketcheck after a Free full
    # sweep, now returning 0) keeps its last listings instead of being wiped by a
    # leasehackr-only crawl. A real crawl also purges leftover `seed` rows.
    sources = {l.source for l in listings}
    delete_sources = set(sources)
    if "seed" not in sources:
        delete_sources.add("seed")
    cur_rows = [(l.listing_key, l.body, l.effective_monthly,
                 json.dumps(l.model_dump(), default=str)) for l in listings]
    ph = ",".join("?" * len(delete_sources))
    con.execute(f"DELETE FROM current WHERE json_extract_string(data, '$.source') "
                f"IN ({ph})", list(delete_sources))
    con.executemany("INSERT OR REPLACE INTO current VALUES (?, ?, ?, ?)", cur_rows)
    hist_rows = [(ts, l.listing_key, l.effective_monthly, l.days_on_market,
                  l.price_drops, l.favorites) for l in listings]
    con.executemany("INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)", hist_rows)

    # Retain the full feature vector per crawl so listings that later disappear
    # (sold) are still trainable -> outcome labels (rank.labels.labels_from_history).
    # `cur_rows` already holds the serialized JSON; reuse it. Prune to the most
    # recent FEATURE_LOG_KEEP crawls to bound growth.
    con.executemany("INSERT INTO feature_log VALUES (?, ?, ?)",
                    [(ts, r[0], r[3]) for r in cur_rows])
    con.execute(
        "DELETE FROM feature_log WHERE crawl_ts NOT IN "
        "(SELECT DISTINCT crawl_ts FROM feature_log ORDER BY crawl_ts DESC LIMIT ?)",
        [FEATURE_LOG_KEEP])


def vin_cache_get(con: duckdb.DuckDBPyConnection | None, vins) -> dict[str, dict]:
    """Look up already-decoded VINs (uppercased keys) -> {body, hp, ev, awd}.
    Lets a crawl skip re-hitting vPIC for VINs it decoded on an earlier run."""
    vins = [v.upper() for v in vins]
    if not con or not vins:
        return {}
    ph = ",".join("?" * len(vins))
    rows = con.execute(
        f"SELECT vin, body, hp, ev, awd FROM vin_cache WHERE vin IN ({ph})",
        vins).fetchall()
    return {r[0]: {"body": r[1], "hp": r[2], "ev": r[3], "awd": r[4]} for r in rows}


def vin_cache_put(con: duckdb.DuckDBPyConnection | None, decoded: dict[str, dict]) -> None:
    """Persist freshly decoded VINs. Empty decodes (no body and no hp) are skipped
    so a later run can retry them rather than caching a useless miss forever."""
    if not con or not decoded:
        return
    ts = datetime.now(timezone.utc)
    rows = [(v.upper(), d.get("body"), int(d.get("hp") or 0),
             bool(d.get("ev")), bool(d.get("awd")), ts)
            for v, d in decoded.items()
            if d.get("body") or d.get("hp")]
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO vin_cache VALUES (?, ?, ?, ?, ?, ?)", rows)


def load_current(con: duckdb.DuckDBPyConnection) -> list[EnrichedListing]:
    rows = con.execute("SELECT data FROM current").fetchall()
    return [EnrichedListing(**json.loads(r[0])) for r in rows]


def load_feature_log(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Retained per-crawl feature snapshots as (crawl_ts, listing_key, data_json),
    oldest crawl first. Substrate for outcome-based LTR labels."""
    return con.execute(
        "SELECT crawl_ts, listing_key, data FROM feature_log ORDER BY crawl_ts"
    ).fetchall()


def get_by_vin(con: duckdb.DuckDBPyConnection, vin: str) -> EnrichedListing | None:
    rows = con.execute(
        "SELECT data FROM current WHERE json_extract_string(data, '$.vin') = ?",
        [vin]).fetchall()
    return EnrichedListing(**json.loads(rows[0][0])) if rows else None
