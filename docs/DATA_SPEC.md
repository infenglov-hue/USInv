# DATA_SPEC — mostly-free data pipeline, fine detail

External contracts are tracked in `SOURCE_REGISTER.md` with an as-of date and
evidence grade. Items marked `[verify]` must be re-checked at implementation
time; an unchecked claim is never treated as an API contract.

## 0. Evidence modes and the data-feasibility gate

The system has two explicitly different evidence modes. Reports MUST display
the mode; code may never silently promote one to the other.

- **Research mode:** an affordable, delisted-inclusive licensed archive plus
  the free sources below. EODHD remains a candidate only if its final written
  contract permits the required retention period; its public terms checked on
  2026-07-18 do not permit the old one-month-then-keep plan. Research mode may
  use a retained vendor adjusted-close series for a delisted security when the
  underlying split/dividend event history is unavailable.
  Such rows carry `adjustment_quality='vendor_frozen'`. Research-mode results
  can reject a strategy or justify continued paper testing; they are not
  audit-grade evidence for committing capital.
- **Audit mode:** delisted-inclusive prices, historical listing/security
  identity, and corporate actions are independently reconstructable to the
  required coverage thresholds. This normally needs a professional security
  master/corporate-action source (or an equivalently verified archive) and a
  separate user budget decision. No vendor is pre-approved merely by name.

Before Phase 1, run a bounded **data-feasibility spike** over at least 30
securities: active, acquired, bankrupt, exchange-to-OTC, ticker-recycled,
reverse-split, pre-2018 delisted, and post-2018 delisted examples. Freeze the
raw responses and publish a coverage matrix for raw OHLCV, adjusted close,
splits, dividends, listing dates, delisting reason/date, exchange, security
type, CIK mapping, and ticker validity intervals. The user selects research or
audit mode only after reading that matrix. Failure to meet a field is a design
input, not an implementation surprise.

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

Pin **secfsdstools 2.4.3** (HansjoergW, Apache-2.0, released Sep-2025; Python
≥3.10 with a Python 3.12 classifier) as the upstream schema and standardizer
reference. Do not use its generic ZIP-to-Parquet transformer for the canonical
raw store: the pinned implementation converts only SUB/PRE/NUM, omits TAG and
casts NUM `value` to `float64`. USInv therefore validates its SUB/NUM/PRE column
contract, owns the SEC TAG contract, streams all four tables to raw Parquet as
strings, and creates a separate typed facts table with NUM
`Decimal128(28,4)`. This preserves the official unscaled values exactly.

The exact upstream Balance/Income/CashFlow standardizer source used for future
mapping work is vendored with its Apache license, upstream commit and per-file
SHA-256 hashes under
`usinv/data/edgar/vendor/secfsdstools_2_4_3/`. The package contains no Q4
derivation matching this blueprint, so the versioned executable contract is
owned in `usinv/data/edgar/rules/quarterly_v1.yaml`; Phase 1.4 will implement it
against golden fixtures. Use **edgartools** (dgunning, MIT, active) for the
filing-centric live edge only after fixture validation. We own PIT dedup, tag
chains beyond the standardizers, factor tables and the historical security
master.

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
- Materialize PIT/latest as immutable snapshots keyed by the sorted normalized-
  batch manifest, not mutable upserts. Existing snapshots never change.
  `facts_pit` chooses minimum `accepted`; `facts_latest` chooses maximum
  `accepted`. Exact repeated observations from versioned source archives are
  coalesced deterministically; logically conflicting rows at the same key and
  acceptance timestamp fail closed. Only a schema-marked PIT artifact can be
  opened through the safe timezone-aware `as_of` reader.

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
**coverage report**. Phase 1.4 reports strict observed presence over every
issuer-by-concept cell and does not relabel a threshold miss. Phase 1.5 owns
the D030 fail-closed applicability engine; per D032, Phase 2.3 applies its
enforceable 90%/75% gate immediately after constructing the date-valid v1
NYSE/Nasdaq/NYSE American domestic-common-stock universe and the existing
financial, REIT, biotech and shell exclusions.

The final denominator is versioned and applicability-aware. A concept cell
is applicable when a direct/fallback filing fact is presented or an explicit,
tested filing fact or accounting identity proves a structural zero. Missing
alone is never evidence of zero or non-applicability and may not be used to
game the denominator. Mandatory scoring inputs remain fail-closed.

**Complete concept list required by MODEL_SPEC §1** (every one needs a chain;
primary tag below, Codex extends fallbacks from secfsdstools standardizer
tables; final D030 applicability gate: core concepts ≥90%, secondary ≥75%):

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
| Shares outstanding | `dei:EntityCommonStockSharesOutstanding` (preserve class dimensions) → instant `CommonStockSharesOutstanding`; weighted-average shares are retained only for per-share diagnostics, never market cap | core |
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
  fiscal-year change detected (10-KT), IPO year with missing quarters, units or
  standardized concepts do not align, or the derived value fails concept-
  specific checks. Do **not** apply a blanket `|Q4| > |FY|` rule to signed
  concepts such as net income/CFO; a legitimate loss in Q1-Q3 can make Q4's
  magnitude exceed FY. Nonnegative concepts (for example revenue) and signed
  concepts have separate tolerances and accounting-identity checks.
- **Exclude `form IN ('20-F','40-F')` filers from the quarterly model** — no
  10-Qs (semiannual 6-K only); they are excluded from the v1 universe anyway.
- TTM = last 4 fiscal quarters, `available_from` = max(accepted) of inputs.

## 2. Fundamentals — EDGAR APIs (live edge)

The primary live edge is **filing-centric**, not companyfacts-centric:
submissions detects a new 10-K/10-Q, then the pipeline archives and parses that
filing's as-filed Inline XBRL/XBRL instance plus presentation metadata. This is
required for custom extension tags, multiple security-class dimensions and the
same evidence pointers used by FSDS. The filing acceptance timestamp from
submissions is attached to every parsed fact. `companyfacts` is a convenient
standard-taxonomy cross-check/backfill, not a complete replacement for the
filing instance.

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
  rows deduplicate against each other. (c) It does not provide the full custom-
  extension/dimensional filing surface required by this design; never use it
  as the sole live ingestion path.
- `data.sec.gov/submissions/CIK##########.json` — `filings.recent` parallel
  arrays (accessionNumber, form, filingDate, **acceptanceDateTime**,
  primaryDocument…), capped at 1,000 recent + paginated older files; top level:
  `tickers[]`, `exchanges[]`, `formerNames[]` (with dates), `sic`,
  `stateOfIncorporation`, addresses.
- Bulk bootstrap: `companyfacts.zip` (~1.2 GB `[verify]`) and
  `submissions.zip`, recompiled nightly ~3 a.m. ET.
- Filing archive path: accession index → primary Inline XBRL document and
  filing data files (`.xml`, presentation/linkbase files where available),
  stored by accession hash. Parse through a pinned library plus golden filings;
  do not scrape rendered HTML tables into numeric facts.
- Preserve every parsed filing fact, including dimensions and declared XBRL
  precision, in the full accession artifact. For the dimensionless canonical
  PIT input, duplicate values at the same filing key coalesce only when they
  agree at their declared `decimals` precision; retain the most precise value.
  A genuine conflict permanently quarantines that key for the filing (D031).
  The resulting numeric rows must use the exact FSDS `facts_raw` schema and
  enter the existing immutable `PitInputBatch`/PIT builder, never a parallel
  live-only latest store.
- **`frames` API is BANNED for the historical store** (last-filed wins,
  restatement history destroyed = lookahead; drops YTD facts; misaligns
  non-calendar fiscal years). Cross-sectional sanity checks only.
- Etiquette: ≤10 req/s hard limit — self-throttle to ~5-8; User-Agent MUST be
  `"USInv/<ver> contact@email"`; 403+~10-min IP block on violation; exponential
  backoff; cache everything; prefer nightly bulk zips over per-CIK loops. Note:
  some datacenter IPs get 403 regardless `[verify from the runner you use]`.

## 3. Historical security master (entity ≠ security ≠ ticker)

A CIK identifies a filing entity, not a tradeable share class, and a ticker is
recyclable. Therefore `ticker_map(cik,ticker)` is not a sufficient primary key.
Materialize two event-sourced tables:

- `securities(security_id, cik, class_title, security_type, domestic_flag)`
- `security_symbols(security_id, ticker, exchange, valid_from, valid_to,
  source, confidence, evidence_pointer)`

`security_id` is an internal immutable identifier. A price row joins to a
security only when `(ticker, exchange, date)` falls inside exactly one validity
interval. Zero or multiple matches are quarantined.

Evidence layers, strongest first:

1. **Historical listing membership:** Alpha Vantage `LISTING_STATUS` queried
   with the rotation date (supported after 2010-01-01) supplies the as-of list,
   exchange, asset type, IPO date and delisting date. It is a primary universe
   input, not merely an audit list. Raw CSV responses are archived by date.
2. **Vendor symbol lifecycle:** the frozen EODHD active+`delisted=1` exchange
   lists, including its `_old` convention for recycled tickers and symbol-
   change history. Use its CIK/ISIN mapping endpoint only if the selected plan
   actually includes it; verify in the feasibility spike.
3. **Filing-time cover facts:** `dei:TradingSymbol`, class title and exchange
   facts from each 10-K/10-Q, keyed by filing acceptance time. These can create
   or corroborate validity intervals; preserve dimensions for multiple share
   classes rather than summing symbols.
4. `company_tickers*.json` and the top-level `submissions` tickers/exchanges
   are **current-state live-edge inputs only**. Former company names are not
   ticker history and never create symbol intervals by themselves.
5. FSDS `SUB.instance` filename prefixes and Wayback snapshots are low-
   confidence recovery aids. They require independent corroboration before a
   price-to-CIK join becomes eligible for scoring.

Coverage gates, measured on every rotation snapshot:

- 100% of selected/held securities must have a unique high-confidence mapping.
- ≥99% of universe market capitalization and ≥98% of eligible security count
  must map uniquely in audit mode; research mode may proceed below that only
  with the missing-count/market-cap table attached to every report.
- Every ticker collision, gap and overlap is a hard data-quality record.
  Silent drops are forbidden because they reintroduce survivorship bias.

## 4. Prices

### 4.0 Canonical schema and event-time price semantics

Raw is immutable and security-keyed:
`(security_id, session, open, high, low, close, volume, provider, batch_id,
bar_definition)`. Vendor symbol is retained as evidence, not used as identity.
The raw values are the contemporaneous nominal prices observed on that session.

Corporate actions are immutable events:
`(security_id, effective_session, type, ratio_or_cash, currency, source,
known_at, quality, batch_id)`. Derived factors are versioned by an explicit
anchor: `split_factor(date, anchor_session)` and
`tr_factor(date, anchor_session)`. No factor may incorporate an action whose
`known_at` or effective session is after the simulation's as-of context.

Consumers are fixed:

- **Raw contemporaneous price:** historical universe price floor, delisting
  clock, ADV dollars, market cap (`raw close × contemporaneous shares`), and
  the T-1 LOO reference. Adjusting an old $1 price for a future reverse split
  would be look-ahead and would corrupt the historical eligibility test.
- **Split-continuous position series:** trailing stops and position continuity.
  Implement by adjusting position quantity/cost basis on the effective session
  or by anchoring past prices only through that session—never through future
  actions.
- **Total-return series:** momentum, benchmark proxy returns and performance
  attribution. Dividends are reinvested for signals/benchmarks but are explicit
  cash flows in the portfolio ledger.
- **Action detector:** raw overnight discontinuity compared with the frozen
  vendor-adjusted series and declared events; it never treats vendor adjusted
  close as primary raw data.

Every derived series carries `adjustment_quality`:
`reconstructed`, `vendor_frozen`, `inferred_split`, or `unresolved`.
`unresolved` rows cannot enter a stop calculation or an audit-mode backtest.
Using dividend-adjusted prices for price-level rules, or future-anchored split
prices for historical filters, is a tested hard failure.

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
- Not viable for universe-scale price polling (do NOT wire into the main price
  loop): Polygon free (5/min, 2yr), Alpha Vantage (25/day). Alpha Vantage's
  separately paced `LISTING_STATUS` snapshots remain the post-2010 dated
  membership input in §6; Phase 0.4 measured 24/36 sample matches and confirmed
  that ticker identity must be resolved independently.

### 4.2 Historical research layer — licensed archive with declared limitations

The pure-$0 stack cannot produce a trustworthy delisted-inclusive small-cap
backtest. EODHD "EOD Historical Data — All World" is an affordable candidate
(displayed at $19.99/month on 2026-07-18), but it is no longer the approved
default: the public terms checked the same day permit local storage during an
active subscription and require deletion within one month after expiry. The
previous plan to subscribe for one month, freeze to Parquet and cancel is
therefore prohibited unless EODHD grants written post-termination retention
rights. Phase 0.4 may instead select a different licensed archive.

Whichever research source is selected must permit the required storage period
and cover active/delisted symbol lists, raw OHLCV, adjusted close and every
available split/dividend event. The raw payload, request parameters, retrieval
time, response hash and license note are archived only for as long as the
selected license permits; vendor data are never committed to a public repo.

**Load-bearing EODHD limitation (if selected):** EODHD's own coverage table says securities
delisted before 2018 have EOD data only; split/dividend/fundamental coverage is
available for post-2018 delistings. Therefore the old statement "2000→2016
adjustments are derived from EODHD actions" is false and is removed.

Research-mode treatment for pre-2018 delisted rows:

1. Raw contemporaneous OHLCV remains the price-level source.
2. Freeze the vendor `adjusted_close` and derive a vendor total-return factor;
   label it `adjustment_quality='vendor_frozen'`.
3. Infer only high-confidence split discontinuities from raw/adjusted factor
   jumps and corroborating filings. Never infer cash dividends merely to make
   two vendor series agree.
4. If a held interval crosses an unresolved action, the primary research run
   quarantines the trade and reports both conservative-loss and exclusion
   sensitivities. Audit mode rejects the run instead.

This can be reproducible only under a license that permits retention, and it is
not independently reconstructed; all research-mode performance pages must say
so. If the feasibility spike shows
material coverage loss or unstable adjusted data, stop and ask the user to
choose: shorten the historical window, accept research-only evidence, or fund
an audit-grade source. Do not market the $20 route as fully audit-grade.

**Price-to-security mapping:** join EODHD symbols (including `_old` recycled
symbols) through `security_symbols` on ticker, exchange and session validity.
Never join on ticker or CIK alone. Zero/multiple matches are quarantined and
counted; a one-ticker/two-issuer/two-era fixture is mandatory.

**Benchmark proxies (per MODEL_SPEC §8):** use IWM and SPY frozen total-return
series from the same archive, plus BIL total return for the non-selecting cash-
yield sensitivity. They are investable ETF proxies, not licensed index total-
return series. Report ETF expense and tracking differences as part of benchmark
interpretation.

### 4.3 Official close definition

T-1 close = **official closing-auction price** (Nasdaq NOCP / NYSE auction).
Never a vendor's overnight "last trade" (after-hours prints move it on thin
names but do not change the official close). Verify the chosen provider uses
official closes `[verify per provider]`.

## 5. Corporate actions

Three independent signals, reconciled nightly; disagreement ⇒ quarantine ticker:

1. Tiingo `splitFactor`/`divCash` for detector-flagged names.
2. Alpaca corporate-actions endpoint (`/v1/corporate-actions`: splits,
   dividends, mergers, spinoffs). Free-tier availability undocumented —
   **verify empirically on day one** `[verify]`.
3. Derived detector (BIST-ported): |overnight return| > 25% on raw close →
   test ratio against rational split grid {2, 3, 4, 3/2, 1/2, 1/5, 1/10, 1/20…}
   ±2% tolerance (reverse splits are ENDEMIC in US small caps) → require no
   same-day 8-K earnings event → confirm via volume spike + raw-vs-adjusted
   divergence.

Cross-vendor reconciliation (2016+ where source coverage overlaps): recompute
the candidate adjusted series from Alpaca raw + declared/detected actions;
drift > 1% ⇒ missed/wrong action. Adj/raw jumps can flag a possible dividend
but never establish its cash amount without a declared event source. Store and
use the provider/exchange-declared **ex-date**. In the T+1 era regular cash
dividends often have ex-date equal to record date, but special distributions,
weekends and other exceptions mean no formula from record date is allowed.

## 6. Universe construction (rank-based, own — no index licensing)

Rebuilt point-in-time per rotation date from our own data; every filter's
pass/fail persisted (`universe_snapshots`) for auditability:

1. Candidate membership comes from the archived date-specific Alpha Vantage
   `LISTING_STATUS` active list joined uniquely through §3, then corroborated
   with filing/vendor evidence. Exchange-listed only: NYSE / NYSE American /
   Nasdaq. **No OTC.** Current SEC ticker lists are never backfilled into old
   dates.
2. US domestic filers only: exclude FPIs/ADRs (form history contains
   20-F/6-K/F-1) — v1 decision, see MODEL_SPEC §2.
3. Common stock only; retain each class as a security, but admit only the most
   liquid eligible line per filing entity into v1 selection. Aggregate issuer
   market cap only across explicitly linked classes with valid share counts.
4. Contemporaneous raw close > $2 (not adjusted through future splits; the
   $1-2 zone is a risk zone, not a universal exchange-rule threshold).
5. Median 21-day dollar volume ≥ $1M (fixed constant — NOT a grid axis; a
   one-off sensitivity check at 0.5/2M is reported in the Phase 5 audit).
6. Market cap is computed per security class from a point-in-time
   outstanding-share fact × contemporaneous raw close, then aggregated only
   when the security-master relationship between classes is explicit.
   Candidates are assigned to two non-overlapping size buckets: **core alpha**
   `$100M ≤ market cap ≤ $10B`, and **large-cap extension** `market cap > $10B`
   with no forced allocation. The portfolio config permits at most {0, 3, 5}
   large-cap slots and ranks that bucket separately (MODEL_SPEC §5). The
   $100M core floor and $10B boundary receive the same named one-off edge
   sensitivity treatment; they are not silently optimized.
   `WeightedAverageNumberOfSharesOutstandingBasic` is a flow denominator and
   is **not** a valid fallback for point-in-time market cap. Missing or
   dimensionally ambiguous share counts quarantine the name; >50% jumps
   without a matching split/action are a hard data-quality flag.
7. Hygiene hard gates (MODEL_SPEC §3) applied last, every exclusion logged.

Index membership is NOT used for the universe (Russell history is not free at
any quality). For benchmarking only: IWM total-return as the Russell 2000
investable proxy; `fja05680/sp500` GitHub lists for S&P sanity checks (good ≥2019,
degrading before).

Delisted-coverage audit: query both active and delisted date-specific Alpha
Vantage lists and reconcile against the frozen vendor archive and security
master. Unexplained gaps are a red pipeline failure, not a warning. Coverage
thresholds come from §3 and are included in the experiment data manifest.

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
