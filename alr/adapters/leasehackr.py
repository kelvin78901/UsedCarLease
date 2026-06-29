"""Leasehackr Private Transfers adapter.

Leasehackr runs Discourse (.json on every page). The Private Transfers category
(id 12) is the real source for lease takeovers.

The deal lives in the POST BODY, not the title. The community posts a labeled
deal sheet that is consistent enough to parse as structured key:value:

    MSRP: $99,390
    Monthly payment: $799 (includes NJ tax)
    Cash due: $3,000
    Current mileage: 11,190
    Maturity mileage: 43,040
    Effective miles per month: 884
    Maturity date: 02/28/2029
    Transfer fee: $500

So we fetch each topic's first post and parse those fields. make/state come from
the topic tags (reliable). months_remaining is computed from the maturity date.
Titles are used only as a last-resort fallback - they are free text and unreliable.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import parse_qs, urlparse

from .base import BaseAdapter, adapter
from ..schema import RawListing

BASE = "https://forum.leasehackr.com"
CATEGORY = os.getenv("ALR_LH_CATEGORY", "c/private-transfers/12")
MAX_TOPICS = int(os.getenv("ALR_LH_MAX_TOPICS", "60"))
REQ_DELAY = float(os.getenv("ALR_LH_DELAY", "0.4"))   # politeness between topic fetches

MAKES = {
    "acura","alfa-romeo","audi","bmw","buick","cadillac","chevrolet","chrysler",
    "dodge","fiat","ford","genesis","gmc","honda","hyundai","infiniti","jaguar",
    "jeep","kia","land-rover","lexus","lincoln","lucid","maserati","mazda",
    "mercedes-benz","mini","mitsubishi","nissan","polestar","porsche","ram",
    "rivian","subaru","tesla","toyota","volkswagen","volvo",
}
STATES = {s.lower() for s in (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()}

# labeled-field patterns over the stripped post body
def _num(pat, text, default=None):
    m = re.search(pat, text, re.I)
    if not m:
        return default
    try:
        return float(m.group(1).replace(",", ""))
    except (ValueError, IndexError):
        return default

FIELDS = {
    "msrp": r"msrp[:\s]*\$?\s*([\d,]+)",
    "monthly": r"(?:monthly(?:\s*payment)?|payment|/mo)[:\s]*\$?\s*([\d,]+)",
    "drive_off": r"(?:cash due|due at signing|das|drive[\s-]?off|down payment)[:\s]*\$?\s*([\d,]+)",
    "transfer_fee": r"transfer fee[:\s]*\$?\s*([\d,]+)",
    "mpm": r"(?:effective )?miles? per month[:\s]*([\d,]+)",
    "cur_miles": r"current mileage[:\s]*([\d,]+)",
    "mat_miles": r"maturity mileage[:\s]*([\d,]+)",
    "incentive": r"(?:incentive|will pay|seller (?:will )?(?:pay|contribut)|cash to you)[:\s]*\$?\s*([\d,]+)",
}
RE_MAT_DATE = re.compile(r"maturity date[:\s]*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", re.I)
RE_CALC = re.compile(r"https?://(?:www\.)?leasehackr\.com/calculator\?([^\s)\"']+)", re.I)
# a deal that's already gone shouldn't be ranked
RE_SOLD = re.compile(r"\b(sold|transfer complete|completed|no longer available|"
                     r"deal done|gone|taken|claimed)\b", re.I)


def _clean_title(t: str) -> str:
    """Strip the junk lessees prepend: [Transfer COMPLETE], (lease transfer),
    'NC ONLY:', 'FT:', 'Lease Transfer -', leading dashes/pipes."""
    s = re.sub(r"\[[^\]]*\]", " ", t)                          # [Transfer], [COMPLETE]
    s = re.sub(r"\((?:transfer|lease)[^)]*\)", " ", s, flags=re.I)
    s = re.sub(r"\b[A-Z]{2}\s+only\s*:?\s*", " ", s, flags=re.I)  # "NC ONLY:"
    s = re.sub(r"^[\s\-–|>]*", "", s)
    s = re.sub(r"^(?:lease\s+transfer|transfer|takeover|ft|wtt|iso)\s*[:\-–]?\s*",
               " ", s, flags=re.I)
    return s.strip()


@adapter("leasehackr")
class LeasehackrAdapter(BaseAdapter):
    def fetch(self) -> Iterable[RawListing]:
        count = 0
        page = 0
        seen = 0
        while count < MAX_TOPICS and page < 12:
            try:
                r = self.client.get(f"{BASE}/{CATEGORY}.json?page={page}")
                r.raise_for_status()
                topics = r.json().get("topic_list", {}).get("topics", [])
            except Exception as e:
                print(f"[leasehackr] category page {page} failed: {e}")
                break
            if not topics:
                break  # past the last page
            seen += len(topics)

            for t in topics:
                if count >= MAX_TOPICS:
                    break
                tid = t.get("id")
                title = (t.get("title") or "")
                if tid is None or title.lower().startswith("about the"):
                    continue
                if RE_SOLD.search(title):     # deal already gone
                    continue
                tags = [str(x).lower() for x in (t.get("tags") or [])]
                make = next((tg for tg in tags if tg in MAKES), None)
                state = next((tg.upper() for tg in tags if tg in STATES), None)

                body = self._body(tid)
                time.sleep(REQ_DELAY)
                if body is None:
                    continue
                rl = self._parse(tid, title, tags, make, state, body, t)
                if rl:
                    count += 1
                    yield rl
            page += 1
        print(f"[leasehackr] scanned {seen} topics across {page} page(s), "
              f"emitted {count} rankable listings")

    def _body(self, tid):
        try:
            r = self.client.get(f"{BASE}/t/{tid}.json")
            r.raise_for_status()
            cooked = r.json()["post_stream"]["posts"][0].get("cooked", "")
            return re.sub(r"<[^>]+>", " ", cooked)
        except Exception as e:
            print(f"[leasehackr] topic {tid} body failed: {e}")
            return None

    def _parse(self, tid, title, tags, make, state, body, topic):
        vals = {k: _num(p, body) for k, p in FIELDS.items()}

        # calculator link params override/fill where present (structured)
        calc = RE_CALC.search(body)
        if calc:
            q = parse_qs(urlparse("?" + calc.group(1)).query)
            g = lambda k: float(q[k][0]) if k in q and q[k][0].replace(".", "").isdigit() else None
            vals["msrp"] = vals["msrp"] or g("msrp")
            vals["monthly"] = vals["monthly"] or g("monthlyPayment") or g("targetPayment")

        # months remaining from maturity date (preferred) else title
        months = None
        md = RE_MAT_DATE.search(body)
        if md:
            mm, dd, yy = md.groups()
            yy = int(yy) + (2000 if len(yy) == 2 else 0)
            try:
                mat = datetime(yy, int(mm), int(dd), tzinfo=timezone.utc)
                months = max(0, round((mat - datetime.now(timezone.utc)).days / 30.44))
            except ValueError:
                months = None
        if not months:
            tm = re.search(r"(\d{1,2})\s*(?:months?|mo)\s*(?:remaining|left)", title, re.I)
            months = int(tm.group(1)) if tm else None

        # remaining miles from maturity - current, else mpm * months
        rem_miles = None
        if vals["mat_miles"] and vals["cur_miles"]:
            rem_miles = max(0, int(vals["mat_miles"] - vals["cur_miles"]))

        if not make:
            make = self._make_from_title(title)
        model = self._model_from_title(title, make)

        if not (make and vals["monthly"]):
            return None  # not rankable; skip rather than emit garbage

        return RawListing(
            source="leasehackr", source_id=str(tid), url=f"{BASE}/t/{tid}",
            title=title,
            make=make.replace("-", " ").title() if make else None,
            model=model,
            msrp=vals["msrp"], monthly=vals["monthly"],
            months_remaining=months,
            miles_per_year=int(vals["mpm"] * 12) if vals["mpm"] else None,
            remaining_miles=rem_miles,
            drive_off=vals["drive_off"], transfer_fee=vals["transfer_fee"],
            seller_incentive=vals["incentive"], state=state,
            days_on_market=self._age_days(topic.get("created_at")),
            price_drops=self._count_drops(body),
            favorites=topic.get("like_count", 0),
            raw={"tags": tags, "views": topic.get("views"),
                 "replies": topic.get("reply_count"), "had_calc": bool(calc)},
        )

    @staticmethod
    def _count_drops(body):
        return len(re.findall(r"\b(?:dropp?ed|reduc(?:ed|ing)|lowered|price drop)\b", body, re.I))

    @staticmethod
    def _make_from_title(title):
        s = re.sub(r"^\s*\d{4}\s*", "", _clean_title(title))
        for tok in s.split():
            lw = re.sub(r"[^a-z\-]", "", tok.lower())
            if lw in MAKES:
                return lw
        toks = s.split()
        return toks[0] if toks else None

    @staticmethod
    def _model_from_title(title, make):
        s = re.sub(r"^\s*\d{4}\s*", "", _clean_title(title))
        if make:
            s = re.sub(rf"^\s*{re.escape(make.split('-')[0])}\w*\s*", "", s, flags=re.I)
        s = re.split(r"[-,(]|\$|\d+\s*/?\s*mo", s)[0]
        return (s.strip()[:40] or "Unknown")

    @staticmethod
    def _age_days(created_at):
        if not created_at:
            return 0
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            return max(0, (datetime.now(timezone.utc) - dt).days)
        except Exception:
            return 0
