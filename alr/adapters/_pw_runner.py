"""Run ONE Playwright adapter's blocking scrape in a fresh subprocess and print
its RawListings as JSON on stdout.

Why a subprocess: this container can run exactly one Playwright (sync) browser
lifecycle per process reliably; a 2nd/3rd in the same crawl process hangs or
crashes the browser (verified). Isolating each scrape in its own short-lived
process sidesteps that entirely. Adapter progress prints go to stderr so stdout
carries only the JSON payload.

Usage:  python -m alr.adapters._pw_runner <adapter-name>
"""
from __future__ import annotations

import contextlib
import json
import os
import sys

from .base import REGISTRY
from . import cars as _cars        # noqa: F401  (register cars/seed)
from . import swapalease as _sa    # noqa: F401  (register swapalease/leasetrader)


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    cls = REGISTRY.get(name)
    if cls is None:
        print(f"[_pw_runner] unknown adapter {name!r}", file=sys.stderr)
        json.dump([], sys.stdout)
        return 1
    real_stdout = sys.stdout
    rows = []
    try:
        # adapter prints (progress/diagnostics) -> stderr; stdout stays clean JSON
        with contextlib.redirect_stdout(sys.stderr):
            rows = cls()._fetch_blocking()
    except Exception as e:  # never crash the parent crawl
        print(f"[_pw_runner {name}] {type(e).__name__}: {e}", file=sys.stderr)
    json.dump([r.model_dump(mode="json") for r in rows], real_stdout)
    real_stdout.flush()
    sys.stderr.flush()
    # Force immediate exit: a lingering Playwright/chromium child can keep the
    # inherited stdout pipe open, which hangs the parent's communicate() on EOF.
    os._exit(0)


if __name__ == "__main__":
    main()
