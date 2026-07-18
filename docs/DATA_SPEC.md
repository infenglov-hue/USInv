# DATA_SPEC — free-data pipeline, fine detail

All facts below verified against primary sources 2026-07-17. Items marked
`[verify]` must be re-checked at implementation time.

## 1. Fundamentals — SEC Financial Statement Data Sets (historical spine)

Source: sec.gov → Data → Financial Statement Data Sets. Quarterly ZIPs,
2009Q2 → present, tab-delimited UTF-8, four files each:

### 1.1 File/field map

- **SUB** (one row per submission): key `adsh` (20-char accession
  `nnnnnnnnnn-nn-nnnnnn`). Fields we consume: `cik`, `name`, `sic`,
  `former`+`changed` (most recent former name), `afs` (filer status
  LAF/ACC/SRA/NON/SML), `fye` (mmdd), `form`, `period` (balance date rounded to
  nearest month-end), `fy`, `fp` (FY/Q1–Q4), `filed` (yyyymmdd),
  **`accepted`** (datetime, seconds precision — THE PIT timestamp),
  `prevrpt`, `instance` (XBRL instance filename — often begins with the ticker,
  used for delisted ticker recovery), `nciks`, `aciks`, `countryinc`.
- **NUM** (one row per numeric fact): composite key `adsh+tag+version+ddate+
  qtrs+uom+segments+coreg`. `value` NUMERIC(28,4) unscaled. Semantics:
  - `qtrs=0` instant (balance sheet), `qtrs=1` one quarter, `qtrs=4` annual,
    `qtrs=3` YTD-3Q etc.
  - `ddate` = period END **rounded to nearest month-end** (this makes fiscal
    alignment tractable incl. 52/53-week filers).
  - `version=adsh` ⇒ custom extension tag.
  - **Always filter `coreg IS NULL`** (consolidated entity) and, since the Dec
    2024 reprocessing, **`segments IS NULL`** for consolidated-only facts.
- **TAG**: `tag+version` key; `custom` flag, `abstract`, `datatype`,
  `iord` (I=instant/D=duration), `crdr`.
- **PRE**: `adsh+report+line`; `stmt` ∈ {BS,IS,CF,EQ,CI,SI,UN}, `plabel`,
  `negating`. Used for extension-tag recovery (§1.4).

Coverage: 10-K, 10-Q, 20-F, 40-F primary financial statements only (not note-
level detail). ~500k reports, 120M+ facts. All data is **as filed**.

### 1.2 Ingestion tooling

Use **secfsdstools** (HansjoergW, Apache-2.0, v2.4.3 Sep-2025 `[verify]`
Python 3.12 compat — tested to 3.11) for zip management, parquet conversion,
filters, Q4 derivation and its Balance/Income/CashFlow standardizers. Vendor
(copy in-repo with attribution) the standardizer rule tables and Q4 logic we
depend on — single-maintainer bus-factor. Use **edgartools** (dgunning, MIT,
active) for the live edge. Do NOT use python-xbrl (discontinued) or OpenEDGAR
(abandoned 2022). We build only the thin layer: PIT dedup, tag chains beyond
the standardizers, factor tables, ticker map (~1-2k lines).

### 1.3 The PIT rule (as-first-filed)

- Dedup on `(cik, tag, ddate, qtrs, uom)` keeping the row with **MIN(accepted)**.
  Insert-only. Amendments (10-K/A, 10-Q/A) arrive as new `adsh` → automatically
  ignored for PIT, still feed `facts_latest`.
- **`prevrpt` is retroactive lookahead** (set when an amendment later arrives).
  Never filter on it in PIT paths.
- **Unified availability rule** (identical in MODEL_SPEC §7 and AGENTS.md
  rule 1): a fact is usable by any signal whose timestamp is **≥ the fact's
  `accepted` datetime**. The rotation signal is timestamped at the T-1 official
  session close (16:00 ET; 13:00 ET half-days), so it admits facts with
  `accepted ≤ T-1 close`. (Commentary only: EDGAR assigns filings accepted
  after 17:30 ET the next business day as official *filing date* — irrelevant
  here because we key on `accepted`, never on `filed`.)

### 1.4 Standardization & tag fallback chains

Never query single tags. Per-concept ordered chains, e.g.:

- revenue: `RevenueFromContractWithCustomerExcludingAssessedTax` →
  `...IncludingAssessedTax` → `Revenues` → `SalesRevenueNet` (pre-2018) →
  goods/services variants → industry variants.
- net income: `NetIncomeLoss` → `ProfitLoss` →
  `NetIncomeLossAvailableToCommonStockholdersBasic`.

Chains live in `tag_chains.py`, **versioned** (chain version stamped into
derived tables). Where revenue exists only as a custom tag: recover via PRE —
IS-statement top line whose `plabel` matches `/revenue|sales/i`. Expected
standardized coverage ~90-95% of the universe, never 100%; ship a per-quarter
**coverage report** and gate scoring on it.

**Complete concept list required by MODEL_SPEC §1** (every one needs a chain;
primary tag below, Codex extends fallbacks from secfsdstools standardizer
tables; coverage gate: core concepts ≥90%, secondary ≥75%):

| Concept | Primary us-gaap tag (fallbacks →) | Core? |
|---|---|---|
| Revenue | (chain above) | core |
| Net income | (chain above) | core |
| Gross profit | `GrossProfit` (→ Revenue − `CostOfRevenue`/`CostOfGoodsAndServicesSold`) | core |
| Operating income (EBIT) | `OperatingIncomeLoss` | core |
| Total assets | `Assets` | core |
| Stockholders' equity | `StockholdersEquity` → `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` | core |
| CFO | `NetCashProvidedByUsedInOperatingActivities` → `...ContinuingOperations` | core |
| Capex | `PaymentsToAcquirePropertyPlantAndEquipment` → `PaymentsToAcquireProductiveAssets` | core |
| Cash & equivalents | `CashAndCashEquivalentsAtCarryingValue` → `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` | core |
| Long-term debt | `LongTermDebtNoncurrent` → `LongTermDebt` | core |
| Current portion of debt + ST borrowings | `LongTermDebtCurrent`, `ShortTermBorrowings` → `DebtCurrent` | core |
| Current assets / liabilities | `AssetsCurrent` / `LiabilitiesCurrent` | core |
| Shares outstanding | `dei:EntityCommonStockSharesOutstanding` (sum classes) → `CommonStockSharesOutstanding` → `WeightedAverageNumberOfSharesOutstandingBasic` | core |
| Preferred equity | `PreferredStockValue` | secondary |
| Minority interest | `MinorityInterest` | secondary |
| D&A | `DepreciationDepletionAndAmortization` → `DepreciationAndAmortization` | secondary |
| Interest expense | `InterestExpense` → `InterestExpenseDebt` | secondary |
| Income tax expense | `IncomeTaxExpenseBenefit` | secondary |

Derived (no own chain): TEV = mktcap + total debt + preferred + minority −
cash; FCF = CFO − capex; all nine Piotroski inputs derive from the rows above
(ROA, CFO, ΔROA, accrual = CFO−NI, Δleverage, Δcurrent ratio, share issuance,
Δgross margin, Δasset turnover).

### 1.5 Quarterly derivation

- Q1–Q3 directly from 10-Q rows `qtrs=1`; fall back to YTD differencing
  (`qtrs=3` − `qtrs=2` etc.) when a `qtrs=1` row is missing.
- **Q4 = (10-K `qtrs=4`) − Σ(three `qtrs=1` rows in the same fiscal year)**,
  matched on `cik+tag+uom`, fiscal year bounded by 10-K `ddate` − ~12mo.
- Sanity quarantine (publish nothing, flag for review) when: quarter count ≠ 3,
  |derived Q4| > |FY|, fiscal-year change detected (10-KT), or IPO year with
  missing quarters.
- **Exclude `form IN ('20-F','40-F')` filers from the quarterly model** — no
  10-Qs (semiannual 6-K only); they are excluded from the v1 universe anyway.
- TTM = last 4 fiscal quarters, `available_from` = max(accepted) of inputs.

## 2. Fundamentals — EDGAR APIs (live edge)

- `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` — per-CIK facts.
  **Traps encoded as tests:** (a) `fy`/`fp` describe the FILING's fiscal focus,
  not the fact's period — NEVER group by fy/fp; derive periodicity from
  `(start,end)` duration (~90d=Q, ~365d=FY) against our own fiscal calendar.
  (b) companyfacts has **no per-fact acceptance timestamp and `filed` is
  date-only — `filed` is NEVER the PIT timestamp.** Join each fact's `accn` to
  `submissions.acceptanceDateTime` to obtain `accepted`; normalize `(start,
  end)` to the FSDS convention (`ddate` = period end rounded to nearest
  month-end, `qtrs` from duration); then apply the SAME dedup as the spine —
  MIN(accepted) on `(cik, tag, ddate, qtrs, uom)` — so edge rows and spine
  rows deduplicate against each other. (c) Non-dimensional facts only — fine.
- `data.sec.gov/submissions/CIK##########.json` — `filings.recent` parallel
  arrays (accessionNumber, form, filingDate, **acceptanceDateTime**,
  primaryDocument…), capped at 1,000 recent + paginated older files; top level:
  `tickers[]`, `exchanges[]`, `formerNames[]` (with dates), `sic`,
  `stateOfIncorporation`, addresses.
- Bulk bootstrap: `companyfacts.zip` (~1.2 GB `[verify]`) and
  `submissions.zip`, recompiled nightly ~3 a.m. ET.
- **`frames` API is BANNED for the historical store** (last-filed wins,
  restatement history destroyed = lookahead; drops YTD facts; misaligns
  non-calendar fiscal years). Cross-sectional sanity checks only.
- Etiquette: ≤10 req/s hard limit — self-throttle to ~5-8; User-Agent MUST be
  `"USInv/<ver> contact@email"`; 403+~10-min IP block on violation; exponential
  backoff; cache everything; prefer nightly bulk zips over per-CIK loops. Note:
  some datacenter IPs get 403 regardless `[verify from the runner you use]`.

## 3. CIK ↔ ticker mapping (incl. delisted)

Layered, materialized as `ticker_map(cik, ticker, exchange, valid_from,
valid_to, source)`:

1. `company_tickers.json` + `company_tickers_exchange.json` (current filers).
2. `submissions.json` `tickers[]`/`exchanges[]` (retains many delisted) +
   `formerNames[]` for renames.
3. FSDS `SUB.instance` filename prefix (conventionally `tick-yyyymmdd.xml`).
4. `dei:TradingSymbol` from companyfacts where present.
5. Wayback Machine snapshots of `company_tickers.json` to date validity windows.

Expect ~2-5% of dead small-caps unmappable: **log, never silently drop**
(silent drops re-introduce survivorship through the back door).

## 4. Prices

### 4.0 Canonical schema and the two adjustments

Raw (immutable): `(ticker, date, open, high, low, close, volume, provider,
batch_id)`. Adjustments (recomputable, separate): `(ticker, date,
split_factor, tr_factor)` where `split_factor` is cumulative **splits only**
and `tr_factor` is **splits + dividends** (total return). Consumers are fixed:

- `split_factor` (price-level semantics preserved): universe price filter
  ($2), delisting-clock rule ($1.50), market cap, trailing stops, LOO collars,
  the >|25%| action detector.
- `tr_factor` (return semantics): momentum sleeve, benchmark series, NAV.

Using a dividend-adjusted close for price-level rules silently deflates
historical prices and misclassifies the $2/$1.50 thresholds — tests must cover
this distinction.

### 4.1 Live/operational layer (active universe) — $0

- **Primary: Alpaca Market Data Basic** (free key, no funded account needed).
  Historical daily bars ~2016+, multi-symbol endpoint. Rate limit: size the
  fetch loop for **200 req/min** (free-tier figure `[verify]` — the 10k/min
  number is the paid tier; multi-symbol batching makes 200/min ample). Crucial
  detail: queries with `end ≤ now − 15min` return **full consolidated SIP**
  data on the free tier (IEX-only restriction applies to real-time). Fetch both
  `adjustment=raw` and `adjustment=all` so we own raw and can verify adj.
  Never use IEX real-time last price for anything.
- **Cross-check: Stooq bulk DB** (`stooq.com/db/h`, `d_us_txt.zip`, ~8,300 US
  stocks + ETFs). Adjusted-only (no unadjusted exists!) and its adjustment
  basis (splits-only vs splits+dividends) is undocumented — **determine it
  empirically on a dividend payer before enabling the §5 drift check**
  `[verify]`; history silently rewritten on each action ⇒ full re-download,
  never append; weekly cadence.
- **Spot-check: Tiingo free** — per-row `divCash`/`splitFactor` are genuinely
  useful; symbol-capped (~500-1000 uniq/mo) ⇒ only for names flagged by the
  action detector.
- **yfinance: BANNED from cron** (D010) — 429 waves, curl_cffi breakage, ToS
  gray. Manual prototyping only.
- Not viable at universe scale (do NOT wire into the main loop): Polygon free
  (5/min, 2yr), Alpha Vantage (25/day — but its free `LISTING_STATUS` delisted
  list IS used as an audit input, §6).

### 4.2 Honest-backtest layer — the one paid item (D001)

The pure-$0 stack **cannot** produce a survivorship-bias-free 10-year small-cap
backtest (free sources drop delisted names; small-cap delistings ~5%/yr,
concentrated exactly in what a value factor buys). Sanctioned fix: subscribe
**one month** to EODHD "All World" ($19.99 `[verify]`), snapshot to Parquet
(`eodhd_snapshot.py`), cancel. The snapshot becomes the immutable backtest
price archive. **The snapshot MUST capture, per ticker:** raw OHLCV, EODHD's
`adjusted_close`, AND the **splits and dividends endpoints** — without the
action series, momentum/total-return and benchmarks cannot be built pre-2016.
Adjustment factors: 2000→2016 derived from EODHD actions; 2016+ from the §5
three-source reconcile (EODHD actions join the reconcile as a fourth source
where they overlap). ~26k US tickers incl. delisted (mostly 2000+); budget
calls accordingly (~1-2 days at 100k calls/day incl. actions endpoints).

**EODHD-symbol → CIK mapping:** snapshot symbols are `.US`-suffixed tickers,
and tickers get RECYCLED across a 26-year history. Join price rows to
fundamentals via `ticker_map` on **ticker AND date ∈ [valid_from, valid_to]**
— never on ticker alone. Rows matching no validity window are quarantined and
counted in the unmappable log (§3). A collision test (one recycled ticker,
two CIKs) is part of the Phase 5.1 acceptance gate.

**Benchmarks (per MODEL_SPEC §8):** built from the same archives — IWM
(Russell 2000 proxy) and SPY dividend-adjusted (TR) series from the EODHD
snapshot pre-2016, Alpaca `adjustment=all` thereafter. Alpaca/Stooq serve as
2016+ robustness cross-checks for the whole price layer.

### 4.3 Official close definition

T-1 close = **official closing-auction price** (Nasdaq NOCP / NYSE auction).
Never a vendor's overnight "last trade" (after-hours prints move it on thin
names but do not change the official close). Verify the chosen provider uses
official closes `[verify per provider]`.

## 5. Corporate actions

Three independent signals, reconciled nightly; disagreement ⇒ quarantine ticker:

1. Tiingo `splitFactor`/`divCash` for detector-flagged names.
2. Alpaca corporate-actions endpoint (`/v1beta1/corporate-actions`: splits,
   dividends, mergers, spinoffs). Free-tier availability undocumented —
   **verify empirically on day one** `[verify]`.
3. Derived detector (BIST-ported): |overnight return| > 25% on raw close →
   test ratio against rational split grid {2, 3, 4, 3/2, 1/2, 1/5, 1/10, 1/20…}
   ±2% tolerance (reverse splits are ENDEMIC in US small caps) → require no
   same-day 8-K earnings event → confirm via volume spike + raw-vs-adjusted
   divergence.

Cross-vendor reconciliation: recompute Stooq's adjusted close from Alpaca raw +
detected actions; drift > 1% ⇒ missed/wrong action. Dividends recoverable from
adj/raw ratio jumps on non-split days. **T+1 era rule (post 2024-05-28):
ex-date generally EQUALS record date** — any `ex = record − 1` assumption
corrupts modern data.

## 6. Universe construction (rank-based, own — no index licensing)

Rebuilt point-in-time per rotation date from our own data; every filter's
pass/fail persisted (`universe_snapshots`) for auditability:

1. Exchange-listed only: NYSE / NYSE American / Nasdaq. **No OTC.**
2. US domestic filers only: exclude FPIs/ADRs (form history contains
   20-F/6-K/F-1) — v1 decision, see MODEL_SPEC §2.
3. Common stock only (single class per CIK: keep the most liquid line;
   sum share classes for market cap).
4. Price > $2 (split-adjusted; the $1-2 zone is delisting-clock territory).
5. Median 21-day dollar volume ≥ $1M (fixed constant — NOT a grid axis; a
   one-off sensitivity check at 0.5/2M is reported in the Phase 5 audit).
6. Market cap $100M – $10B (small/mid tilt; fixed constant, same one-off
   sensitivity treatment), computed from
   `dei:EntityCommonStockSharesOutstanding` (sum classes; gate >50% jumps
   without a matching split — SEC-documented scaling errors) × price.
7. Hygiene hard gates (MODEL_SPEC §3) applied last, every exclusion logged.

Index membership is NOT used for the universe (Russell history is not free at
any quality). For benchmarking only: Russell 2000 TR via ETF total-return
series; `fja05680/sp500` GitHub lists for S&P sanity checks (good ≥2019,
degrading before).

Delisted-coverage audit: reconcile our universe/price coverage against Alpha
Vantage's free `LISTING_STATUS` delisted list; unexplained gaps are a red
CI failure, not a warning (the BIST frozen-data lesson).

## 7. Macro / regime series (vintage-correct)

| Signal | Source / series | Cadence, lag | PIT rule |
|---|---|---|---|
| HY credit stress | FRED `BAMLH0A0HYM2` (ICE BofA HY OAS) | daily, ~1bd lag | archive full history NOW (reported FRED truncation to rolling ~3yr `[verify]`); fallback proxy: HYG/LQD TR ratio z-score (license-free forever) |
| Curve slope | FRED `T10Y3M`, `T10Y2Y` | daily, same-evening | slow gate only |
| Financial conditions | `NFCI` (weekly, Wed 08:30 ET, covers through prior Fri) | 5-day lag | **history revised weekly ⇒ backtests MUST use ALFRED vintages** |
| Volatility | CBOE `VIX_History.csv` (primary), FRED `VIXCLS` mirror | daily | same-evening CSV |
| Labor regime | FRED `SAHMREALTIME` (NOT SAHMCURRENT — revised) | monthly, jobs-Friday | slow structural gate |

Istanbul timing bonus: all US daily series are final during the Istanbul
morning, hours before the order deadline (OPS_SPEC §2).

## 8. Freshness gates (BIST lesson, promoted to CI)

Blueprint defaults (config-tunable, these ARE the initial values):

- Fundamentals: expected next report = quarter end + due window by filer
  status (10-Q: 40d LAF/ACC, 45d others; 10-K: 60d LAF, 75d ACC, 90d others)
  + **grace 5 business days**. If **>10%** of the scored universe is past
  expected+grace, the nightly job exits red and delivery is blocked.
- Prices: any active-universe ticker whose last bar is > 3 sessions old ⇒ red.
- Macro: per-series max age = 2× its publication cadence.

All three gates are hard CI failures with Telegram alert (Phase 7), not log
lines.
