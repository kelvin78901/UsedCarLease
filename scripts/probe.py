"""Real-machine adapter probe. Run one adapter against the live site and see
exactly what comes back, plus how often the fields that matter actually parse.

    python scripts/probe.py leasehackr
    python scripts/probe.py swapalease --limit 8
    python scripts/probe.py vin 5YJ3E1EA7KF317712     # test NHTSA vPIC directly

The fill-rate table is the real signal: an adapter that returns 50 listings but
parses `monthly` on 3 of them is barely working - normalize will drop the rest.
"""
import sys
import httpx

from alr.adapters.base import get_adapters
from alr.adapters import leasehackr, swapalease, cars, marketcheck  # noqa: F401

FIELDS = ["make", "model", "monthly", "msrp", "months_remaining",
          "miles_per_year", "drive_off", "transfer_fee", "seller_incentive", "state"]


def probe_adapter(name, limit):
    insts = get_adapters([name])
    if not insts:
        print(f"no adapter named '{name}'")
        return
    a = insts[0]
    print(f"running adapter: {name} ...")
    rows = a.fetch_sync()

    n = len(rows)
    print(f"\n{n} raw listings returned\n")
    if n == 0:
        print("Zero results. Usual causes:")
        print("  - selectors/category id are stale -> open the site, inspect, fix")
        print("    leasehackr: check the CATEGORY slug/id in alr/adapters/leasehackr.py")
        print("    swapalease/leasetrader: fix the SEL dict in alr/adapters/swapalease.py")
        print("  - blocked/redirected -> check status, User-Agent, robots.txt")
        return

    print("field fill rate (non-empty / total):")
    for f in FIELDS:
        c = sum(1 for r in rows if getattr(r, f) not in (None, "", 0))
        bar = "#" * round(20 * c / n)
        print(f"  {f:18} {c:3}/{n:<3} {bar}")

    print(f"\nfirst {min(limit, n)} listings:")
    for r in rows[:limit]:
        print(f"  [{r.source_id}] {r.make} {r.model} | "
              f"${r.monthly}/mo | {r.months_remaining}mo | msrp={r.msrp} | {r.url}")

    rankable = sum(1 for r in rows if r.make and r.monthly)
    print(f"\n{rankable}/{n} have make+monthly and will survive normalize "
          f"({'good' if rankable > n * 0.5 else 'LOW - tune extraction'})")


def probe_vin(vin):
    from alr.enrich.nhtsa import decode_vin
    with httpx.Client(timeout=20, headers={"User-Agent": "AutoLeaseRank/0.4"}) as c:
        print(f"vPIC decode for {vin}:")
        print(" ", decode_vin(vin, c))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/probe.py <adapter|vin> [arg] [--limit N]")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "vin":
        probe_vin(sys.argv[2])
    else:
        limit = 5
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        probe_adapter(cmd, limit)
