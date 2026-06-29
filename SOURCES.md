# Data sources - honest status

What actually works, verified against the live structure (not aspirational).

| Source | Type | Status | How it's pulled |
|---|---|---|---|
| **Leasehackr Private Transfers** | lease takeover | ✅ verified, structured | Discourse `.json`, paginated. Parses the labeled deal sheet in each post body (`MSRP:`, `Monthly payment:`, `Cash due:`, `Maturity date:`, `Transfer fee:`, `Effective miles per month:`). make/state from tags. httpx, no key. |
| **Marketcheck** | used-car inventory (whole US) | ✅ structured API | `GET /v2/search/car/active`. Returns vin/price/miles/msrp/**dom**/build{body,drivetrain,fuel}/dealer. Aggregates ~every US dealer + marketplaces. Needs free `ALR_MARKETCHECK_KEY`. Price→monthly via amortization (estimate). |
| **NHTSA vPIC** | enrichment | ✅ verified | VIN decode for body/hp/ev/awd. Keyless. |
| **Swapalease** | lease takeover | ⚠️ Playwright, unverified | JS-rendered + WAF 403 on httpx. Playwright adapter provided; selectors are placeholders - inspect & fix on your machine. May hit anti-bot. |
| **LeaseTrader** | lease takeover | ⚠️ Playwright, unverified | Same as Swapalease. |
| **Cars.com** | used-car inventory | ⚠️ best-effort | SSR HTML has listings but the site WAF-blocks non-browser clients. Use Marketcheck instead (it ingests Cars.com among others), or a Playwright adapter. |
| Facebook Marketplace | private party | ❌ not supported | ToS prohibits scraping; heavy anti-bot. |
| KBB / Edmunds / AutoTrader | pricing / inventory | ❌ not free | API-gated or hard anti-bot. Marketcheck covers the inventory need. |

## Recommended live setup

```bash
# 1) lease transfers (no key) + used-car market (free Marketcheck key)
export ALR_MARKETCHECK_KEY=...          # from marketcheck.com/apis
export ALR_ADAPTERS=leasehackr,marketcheck
python scripts/probe.py leasehackr      # check fill rates
python scripts/probe.py marketcheck     # check fill rates
python -m alr.pipeline.run              # crawl both into the snapshot
python scripts/train_ltr.py             # retrain on real data
```

## Note on mixing leases and purchases

Lease transfers have a real monthly + fees → true effective $/mo. Used-car
purchases have a price, not a payment, so Marketcheck listings get an **estimated**
financed monthly (price amortized at `ALR_MC_APR`/`ALR_MC_TERM`). This lets both
rank on one axis; the estimate flag lives in each listing's `raw.monthly_is_estimate`.
For a stricter design, split ranking by `listing_type` (lease vs purchase).
