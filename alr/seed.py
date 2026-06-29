"""Deterministic seed data. Mirrors the JS generator in the dashboard so the
backend and frontend agree on numbers. Lets the entire pipeline run end-to-end
(normalize -> enrich -> rank -> serve -> train) before a single scraper fires."""
from __future__ import annotations

from .schema import RawListing

# make, model, body, msrp, hp, luxury, ev, awd_capable
CATALOG = [
    ("BMW", "i4 M50", "EV", 70300, 536, 1, 1, 1),
    ("BMW", "X3 xDrive30i", "SUV", 51900, 248, 1, 0, 1),
    ("Audi", "Q5 45 Prestige", "SUV", 53400, 261, 1, 0, 1),
    ("Audi", "e-tron GT", "EV", 107100, 522, 1, 1, 1),
    ("Tesla", "Model 3 LR", "EV", 47240, 394, 0, 1, 1),
    ("Tesla", "Model Y LR", "EV", 51490, 384, 0, 1, 1),
    ("Genesis", "GV70 3.5T", "SUV", 55100, 300, 1, 0, 1),
    ("Genesis", "G70 3.3T", "Sedan", 45200, 300, 1, 0, 1),
    ("Mercedes", "C300 4MATIC", "Sedan", 50050, 255, 1, 0, 1),
    ("Mercedes", "GLC300", "SUV", 54300, 255, 1, 0, 1),
    ("Toyota", "RAV4 XLE", "SUV", 33150, 203, 0, 0, 0),
    ("Toyota", "Camry XSE", "Sedan", 31600, 208, 0, 0, 0),
    ("Honda", "Accord Sport", "Sedan", 31300, 192, 0, 0, 0),
    ("Honda", "CR-V EX-L", "SUV", 35900, 190, 0, 0, 0),
    ("Hyundai", "Ioniq 5 SEL", "EV", 47100, 320, 0, 1, 1),
    ("Kia", "EV6 Wind", "EV", 52600, 320, 0, 1, 1),
    ("Volvo", "XC60 B5", "SUV", 49900, 247, 1, 0, 1),
    ("Lexus", "RX 350", "SUV", 52050, 275, 1, 0, 1),
    ("Acura", "MDX Tech", "SUV", 52200, 290, 1, 0, 1),
    ("Porsche", "Macan", "SUV", 62000, 261, 1, 0, 1),
    ("Ford", "Mustang Mach-E", "EV", 48400, 346, 0, 1, 1),
    ("Subaru", "Outback Premium", "SUV", 32400, 182, 0, 0, 1),
]
STATES = ["CA", "NY", "NJ", "TX", "FL", "WA", "MA", "IL", "GA", "PA", "CT", "VA", "MD", "AZ", "CO"]
SOURCES = ["Leasehackr", "Swapalease", "LeaseTrader", "Cars.com", "DealerInv"]


def _mulberry32(a: int):
    """Faithful port of the JS mulberry32 used by the dashboard, so backend and
    frontend generate identical seed data. Math.imul -> (x*y) mod 2^32."""
    a &= 0xFFFFFFFF

    def imul(x: int, y: int) -> int:
        return (x * y) & 0xFFFFFFFF

    def rnd() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = imul(a ^ (a >> 15), 1 | a)
        t = ((t + imul(t ^ (t >> 7), 61 | t)) & 0xFFFFFFFF) ^ t
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296
    return rnd


def generate(n: int = 54, seed: int = 20260628) -> list[RawListing]:
    r = _mulberry32(seed)
    out: list[RawListing] = []
    for i in range(n):
        make, model, body, msrp, hp, lux, ev, awd_cap = CATALOG[int(r() * len(CATALOG))]
        months = 6 + int(r() * 30)
        mpm = [625, 833, 1000][int(r() * 3)]
        rem_miles = round(mpm * months + r() * 4000)
        deal = 0.0102 + r() * 0.0072
        monthly = round((msrp * deal) / 5) * 5
        drive_off = round((r() * 3400) / 50) * 50
        transfer_fee = [0, 500, 595, 700][int(r() * 4)]
        acq_fee = 695 if r() > 0.6 else 0
        disp_fee = 350 + int(r() * 250)
        incentive = round((r() * 4500) / 100) * 100 if r() > 0.5 else 0
        dom = int(r() * 38)
        drops = int(r() * 3) + 1 if r() > 0.7 else 0
        awd = 1 if (awd_cap and r() > 0.25) else 0
        state = STATES[int(r() * len(STATES))]
        source = SOURCES[int(r() * len(SOURCES))]
        out.append(RawListing(
            source=source, source_id=f"AL{1000 + i}",
            url=f"https://example.invalid/{source.lower()}/{1000 + i}",
            title=f"{make} {model} lease takeover",
            make=make, model=model,
            vin=f"SEED{1000 + i:08d}",
            msrp=msrp, monthly=monthly, months_remaining=months,
            miles_per_year=mpm * 12, remaining_miles=rem_miles,
            drive_off=drive_off, transfer_fee=transfer_fee,
            acquisition_fee=acq_fee, disposition_fee=disp_fee,
            seller_incentive=incentive, state=state,
            days_on_market=dom, price_drops=drops,
            favorites=max(0, round((40 - dom) * (1.4 if incentive > 0 else 1) + r() * 10)),
            raw={"body": body, "hp": hp, "luxury": bool(lux), "ev": bool(ev), "awd": bool(awd)},
        ))
    return out
