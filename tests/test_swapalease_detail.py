"""Offline test of the swapalease detail-page parser against a real captured
sample (2024 BMW i4 eDrive35, SAL ID 1709670). No network — uses the fixture."""
from pathlib import Path

from alr.adapters.swapalease import parse_detail

FIXTURE = Path(__file__).parent / "fixtures" / "swapalease_i4.html"


def test_parse_swapalease_i4_detail():
    d = parse_detail(FIXTURE.read_text())
    assert d["effective"] == 520.0
    assert d["actual"] == 570.0
    assert d["incentive"] == 500.0
    assert d["vin"] == "WBY43AW08RFS58118"
    assert d["current_miles"] == 12000
    assert d["remaining_miles"] == 18018
    assert d["miles_per_month"] == 1802
    assert d["months"] == 10
    assert d["end_date"] == "4/3/2027"
    assert d["style"] == "i4 eDrive35"
    assert "RWD 5dr Sedan" in d["trim"]
    assert d["year"] == 2024
    assert d["leasing_company"] == "BMW Financial Services"
    assert d["exterior"] == "Black"


def test_effective_cost_identity():
    """The effective-cost engine: actual − incentive/term must equal the page's
    effective. 570 − 500/10 = 520."""
    d = parse_detail(FIXTURE.read_text())
    computed = round(d["actual"] - d["incentive"] / d["months"])
    assert computed == 520 == round(d["effective"])
