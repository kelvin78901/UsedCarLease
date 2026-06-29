"""Scheduler. Runs the crawl every CRAWL_INTERVAL_MIN minutes and refreshes the
snapshot the API serves. Run as its own process/container alongside the API.
Both read the same DuckDB file; the API's /reload (or its next restart) picks up
fresh snapshots. For a single-process setup, call run_once() from the API too."""
from __future__ import annotations

import time

from apscheduler.schedulers.background import BackgroundScheduler

from .config import CRAWL_INTERVAL_MIN
from .pipeline.run import crawl


def run_once():
    crawl()


def main():
    run_once()  # crawl immediately on boot
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(run_once, "interval", minutes=CRAWL_INTERVAL_MIN,
                  id="crawl", max_instances=1, coalesce=True)
    sched.start()
    print(f"[scheduler] crawling every {CRAWL_INTERVAL_MIN} min. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()


if __name__ == "__main__":
    main()
