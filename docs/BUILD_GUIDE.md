# BUILD_GUIDE — how to construct USInv without fooling ourselves

This guide explains the system as an engineering product. `CODEX_TASKS.md` is
the PR checklist; this file explains why the pieces exist, how they connect,
and what a correct result looks like.

## 1. What we are actually building

USInv is not primarily a stock-ranking formula. It is an **evidence pipeline**
that must answer the same question at any historical or live timestamp:

> Given only information genuinely available at this instant, which securities
> were tradeable, which passed our safety rules, how were they ranked, what
> orders could we have placed, and what happened after realistic costs?

The factor formula is perhaps 10% of the difficulty. The hard 90% is identity,
availability time, corporate actions, delistings, reproducible execution and
experiment discipline.

The system has seven contracts:

1. **Time contract:** exchange sessions, half-days, T-1 signal and T fill.
2. **Information contract:** every datum has `available_from`; future rows are
   impossible to read through the scoring API.
3. **Identity contract:** entity, security class and ticker are separate.
4. **Price contract:** raw price, split continuity and total return have
   different consumers and never borrow future actions.
5. **Execution contract:** the backtest can fill only what the automated
   paper/live workflow could have filled, using the same collar, costs and
   cash funding.
6. **Experiment contract:** data/config/code are frozen before the one-shot
   holdout; every tried configuration is counted.
7. **Evidence contract:** `research` and `audit` modes are visibly different.

If one contract is violated, a beautiful Sharpe ratio is meaningless.

## 2. The end-to-end flow

```text
provider payloads
    ↓ archive unchanged + hash
normalized event tables
    ↓ availability/identity/quality gates
as_of(timestamp) context
    ↓ universe → hygiene → factors → composite
portfolio transition
    ↓ orders under cash/collar/cost rules
canonical ledger
    ↓ metrics, experiment artifacts, live snapshot
```

There is no special backtest shortcut. Historical and live scoring both call
the same `as_of(timestamp)` context. Only the timestamp and frozen manifest
differ.

## 3. Decisions required before serious coding

Phase 0 exposes these choices instead of burying them:

- EDGAR contact identity for the required User-Agent.
- Provider accounts/tokens (Alpaca, Alpha Vantage, optional Tiingo/FRED).
- Historical evidence mode:
  - `research`: affordable delisted-inclusive archive with explicit retention
    rights; EODHD is only a candidate because its public expiry terms conflict
    with the former one-month snapshot plan; old delisted adjustments may be
    retained vendor values and are labeled as such;
  - `audit`: separately approved source with reconstructable actions and
    security history.
- Storage/backup target for licensed and multi-gigabyte data.
- Broker account type and whether buys use settled cash only.
- Whether the eventual derived snapshot may be publicly hosted.

Do not create provider accounts, spend money or publish data silently. Each
choice is written into `docs/PROGRESS.md` and typed configuration.

## 4. Build sequence

### Step A — create a boring, strict skeleton

Set up Python 3.12, typed configuration, logging, pytest, ruff and CI before any
financial logic. Every command writes into an explicitly supplied workspace;
tests use temporary directories. Add an artifact guard that fails if tests or
experiments write into production `data/`.

Configuration is divided by meaning:

- `settings.yaml`: paths, providers, timezones and evidence mode;
- `universe.yaml`: exchange/security/liquidity/cap filters;
- `factors.yaml`: concept chains, transformations and weights;
- `portfolio.yaml`: N, band, caps, stops, cash reserve and rotation;
- `regime.yaml`: overlay definitions;
- `experiment_grid.yaml`: an exact copy of EXPERIMENT_PLAN §3.

Typed loaders reject unknown keys. A misspelled threshold must fail, not fall
back to a default.

### Step B — make time deterministic

Wrap an XNYS exchange calendar. Required functions include:

- `session(date)`, `previous_session`, `next_session`;
- official open/close timestamps and half-day detection;
- T-1 signal/T fill mapping;
- fixed-anchor 2w/4w/6w/13w rotation generation;
- shift to the next eligible full session without rebasing later anchors.

Tests use known traps: Good Friday, Juneteenth before/after 2022, observed July
4th, Thanksgiving half-day, MLK/Memorial/Labor Monday and DST transitions.
No financial module may perform weekday arithmetic directly.

### Step C — prove data feasibility before building adapters

Probe at least 30 deliberately awkward securities. The sample must include
active, acquired, bankrupt, OTC-moved, ticker-recycled, multi-class,
reverse-split, pre-2018 delisted and post-2018 delisted cases.

For every candidate source, record whether it actually supplies:

- raw OHLCV and the meaning of `open`/`close`;
- adjusted close and its adjustment method;
- splits, dividends and their effective/ex-dates;
- IPO/delisting dates and reason;
- historical exchange/security type;
- CIK/ISIN/FIGI mapping and validity dates.

The output is a coverage matrix, raw fixture hashes and a recommended evidence
mode. This spike is successful even when it proves a cheap data route
insufficient; discovering that before thousands of lines of code is the point.

### Step D — build the point-in-time fundamentals spine

1. Download every SEC FSDS quarterly ZIP once; store source URL, check time and
   checksum. Keep the official headers-only `2009q1` package for archive
   completeness, while treating `2009q2` as the first quarter with submissions.
   Normal sync skips a hash-verified existing object; an explicit refresh uses
   HTTP validators when available and appends unchanged/reprocessed evidence.
   Never overwrite a prior hash or assume an old ZIP remains unchanged.
2. Validate the pinned SUB/NUM/PRE schema plus the official TAG schema, then
   stream all four tables into raw Parquet as strings. The canonical path does
   not call secfsdstools' generic converter because it omits TAG and casts NUM
   values to floating point.
3. Join numeric facts to filing `accepted` timestamps, converting the SEC
   Eastern timestamp to UTC and NUM values to exact `Decimal128(28,4)` in a
   separate typed table. Keep raw values alongside it as immutable evidence.
4. Filter consolidated statement facts according to the current FSDS contract
   (`coreg` and `segments` empty), always through a timezone-aware `as_of`
   boundary. Never filter retroactive `prevrpt`.
5. Create:
   - `facts_pit`: first-accepted value for the as-first-filed research view;
   - `facts_latest`: latest-known restated view, never accessible to signals.
   Build both as deterministic, content-addressed Parquet snapshots over an
   exact normalized-input manifest. Existing snapshots are immutable; a new
   amendment creates a new snapshot in which the old PIT key still selects the
   minimum acceptance time while latest selects the maximum. Equal-time rows
   with conflicting logical facts are quarantined rather than broken by an
   arbitrary row order. The safe reader verifies `facts_pit` view metadata and
   rejects a latest artifact even when a caller supplies its path directly.
6. Standardize concepts through versioned tag chains. Record source tag,
   accession and chain version for every derived value.
7. Derive fiscal quarters with concept/unit alignment and quarantine, then TTM
   with `available_from=max(input accepted)`.

For the nightly edge, detect a new filing through submissions, archive and
parse that accession's as-filed XBRL instance/presentation files, then use
companyfacts as a standard-tag cross-check. Companyfacts alone omits the custom
and dimensional surface needed for tag recovery and multi-class identity.

The central API behaves conceptually like:

```python
ctx = scoring_context.as_of("2024-05-06T16:00:00-04:00")
ttm = ctx.fundamentals(security_id)
assert (ttm.available_from <= ctx.timestamp).all()
```

The caller cannot request `latest=True`. If newer data are encountered, the
context raises rather than filtering silently; this makes leakage visible in
tests.

Golden fixtures should include a calendar-year filer, offset fiscal year,
restatement, custom-tag revenue, multiple share classes and invalid Q4
derivation. Hand-check them against archived filings.

### Step E — build the historical security master

Create an immutable internal `security_id`. Link it to CIK (entity), class
title/security type, and a sequence of symbol intervals:

```text
security_id | ticker | exchange | valid_from | valid_to | source | confidence
```

Build date-specific candidate membership from archived Alpha Vantage listing
CSV, then corroborate identities with filing-time `TradingSymbol`/exchange
facts and the selected vendor's lifecycle/identifier data.

Required invariants:

- symbol intervals for the same exchange cannot overlap;
- one `(ticker, exchange, session)` maps to zero or one security;
- zero and multiple matches are quarantined, never guessed;
- current SEC ticker arrays never populate historical dates;
- former company names do not create ticker changes;
- multiple share classes remain separate securities.

This component is the bridge between EDGAR fundamentals and market prices. It
should be completed before pretending the price universe is survivorship-safe.

### Step F — build prices and corporate actions

Archive raw provider responses, then normalize to security-keyed daily bars.
Keep three different concepts explicit:

1. **Raw contemporaneous price** for historical price floor, ADV, market cap
   and T-1 order reference.
2. **Split-continuous position basis** for stop/high-water continuity.
3. **Total return** for momentum and benchmark proxies.

Corporate actions are events with `effective_session` and `known_at`. A factor
is always anchored to an as-of session. A unit test should create a reverse
split in 2025 and prove that a 2024 $1.20 price remains $1.20 for the 2024
universe test.

For research-mode old delistings, freeze vendor adjusted close separately and
label quality `vendor_frozen`. Never manufacture a cash dividend to force a
reconciliation. Any held interval crossing an unresolved event is visible in
the report and handled by the declared sensitivity rules.

Validate daily `open` as an auction proxy on a stratified 2016+ sample. Until
validated, execution artifacts carry `fill_quality='daily_open_proxy'`.

### Step G — create a point-in-time universe

For each rotation timestamp:

1. load securities active on that date and approved exchanges;
2. retain domestic common stock and the chosen share class;
3. apply raw close, dollar-volume and market-cap filters;
4. exclude unsupported sectors/carve-outs;
5. apply hygiene gates;
6. persist every candidate with every pass/fail reason.

Market cap uses a point-in-time instant share count and raw price. Weighted-
average diluted/basic shares are earnings denominators, not a replacement for
outstanding shares.

The saved universe snapshot is as important as the selected portfolio. It lets
us answer, years later, why a company was missing.

### Step H — implement safety gates before factor ranking

Each gate returns a structured result:

```text
security_id, as_of, gate, severity, passed, evidence_pointer, rule_version
```

Shell, ATM/dilution, going-concern, listing-compliance, suspension and data-
integrity rules need real positive and negative fixtures. Text search alone is
not enough: handle negation, alleviation and boilerplate. Exchange listing
rules are encoded by venue; the portfolio's conservative $1.50 policy is
labeled as policy, not law.

If a hard gate lacks data coverage, the security is quarantined. "No flag found"
must never mean "safe" when the search never ran.

### Step I — calculate factor sleeves

Calculate value, quality and 12-1 momentum independently. For every score save:

- raw inputs and their availability timestamps;
- transformations/winsorization;
- peer group and percentile;
- missing-data decision;
- tag-chain/rule/config version.

Rank only inside the eligible universe. Fit/winsorization boundaries use the
current cross-section without future observations. Piotroski is a veto, not a
separate optimized return sleeve in v1.

Start with declared weights; do not port BIST weights. Phase 5 attribution uses
TRAIN only to propose the pre-registered candidate weight sets.

### Step J — turn scores into a stateful portfolio

Portfolio selection is a transition from previous holdings, not a fresh top-N
list:

1. process forced exits;
2. retain held names still inside the hold band;
3. rank new candidates;
4. apply entry band, sector cap and correlation constraint deterministically;
5. enforce one-way turnover budget;
6. size from settled cash and leave unfilled slots in cash;
7. preserve position ID, cost basis and high-water mark through rotations and
   splits.

Tie-breaks must be deterministic. Running the same context/config twice should
produce byte-identical selection output.

### Step K — implement one execution engine and ledger

An order contains signal timestamp, reference price, side, quantity, collar,
funding source and expiry behavior. The engine fills at the historical opening
proxy only when the price satisfies the LOO collar; otherwise it follows the
registered skip/retry rule. Costs are applied per trade inside the event loop.

The ledger is the source of truth:

- positions and cash;
- orders/fills and assumed vs actual price;
- dividends/splits;
- costs and turnover;
- daily NAV and benchmark-proxy NAV;
- code/config/data hashes.

Headline experiments charge 40bp one-way inside each fill. Cash earns zero in
the primary backtest; a BIL total-return sensitivity is reported separately so
an assumed sweep yield cannot rescue an overlay during model selection.

Backtest, paper-forward and delivery read this ledger. No UI calculates its own
return.

Termination uses evidence, not a missing-price guess: documented acquisition
consideration, executable pre-OTC exit, documented bankruptcy recovery, or the
conservative unknown rule in MODEL_SPEC §6.

### Step L — validate on tiny hand-computable systems

Before a 14-year run, build a three-security toy history with:

- one ordinary buy/hold;
- one split and dividend;
- one unfilled opening collar;
- one reverse split across a stop;
- one acquisition and one unknown termination;
- one filing accepted just after the signal cutoff.

Calculate the expected ledger in a small checked spreadsheet/fixture and match
cash, quantity, costs and NAV to the cent. Large backtests cannot diagnose a
broken accounting identity.

### Step M — run the registered experiment

1. Freeze `data_manifest.json`, code SHA and raw config.
2. Explore only TRAIN.
3. Run 29 Stage-1 OFAT cells on VALIDATION.
4. Run at most 96 reduced-factorial cells.
5. Run missing original-grid neighbors/categories for at most three finalists.
6. Apply explicit plateau tolerances, ablations, cost stresses and simplicity
   tie-breaks.
7. Register one config hash.
8. Unlock and run TEST once through 2026-06-30.
9. Publish every metric, confidence interval, termination/action-quality table
   and failure—not only favorable charts.

The system logs every attempted configuration. Renaming a run or deleting a
bad output does not reduce the multiple-testing count.

### Step N — operate unattended without pretending cron is guaranteed

Nightly work fetches only incremental data, verifies freshness/coverage,
computes scores and updates the compact runtime ledger. GitHub Actions schedules
pin directly to `America/New_York`; primary and later retry triggers share a
remote idempotency key, so either can safely complete the same business-date
operation exactly once. A stale/missing job blocks new orders and delivery and
shows a stale banner; it never silently republishes yesterday's picks as fresh.

Production must be reconstructable from a fresh GitHub-hosted runner. Put the
canonical runtime ledger and object pointers in durable remote storage, not on
the developer PC or in an Actions artifact. Every successful stage sends a
heartbeat to an independent monitor. If the primary cron is delayed or
dropped, the retry schedule—or an external `workflow_dispatch` trigger—runs
the same idempotent operation. Paper mode writes intended orders; live mode,
enabled only after its explicit capital gate, submits the same orders with
deterministic broker client-order IDs and later reconciles actual fills.

Historical licensed archives stay out of GitHub. Publish only small derived
artifacts allowed by the data licenses.

After a TEST pass, freeze the rules for at least 12 rotations and 12 months.
Record actual staged LOO results/slippage. Any model change resets the forward
clock. This stage tests the system and operator behavior, not merely code.

## 5. Testing strategy

Use four layers:

1. **Pure unit tests:** calendars, transformations, adjustment math, costs.
2. **Property tests:** no read past `as_of`; symbol intervals never overlap;
   future actions never change past decisions; ledger identities always hold.
3. **Golden fixtures:** archived real filings/providers with human-checked
   expected concepts, mappings and gate outcomes.
4. **Pipeline kill-switch tests:** stale prices, poor concept coverage,
   unmapped selected security, unresolved action or config drift blocks output.

Live smoke tests are separate and optional in ordinary CI so provider outages
do not make unit tests nondeterministic. Their recorded fixtures are refreshed
deliberately with source-contract review.

## 6. What every artifact must say

At minimum:

- evidence mode and adjustment/fill quality;
- as-of signal/fill timestamps;
- data-manifest, config and code hashes;
- universe/mapping/action coverage;
- costs and cash-funding model;
- benchmark explicitly called IWM/SPY proxy;
- termination assumptions and sensitivities;
- whether the result is TRAIN, VALIDATION, TEST or paper-forward;
- actual number of tried configurations.

This metadata is what turns a number into evidence.

## 7. Practical first milestone

The first valuable milestone is not a stock recommendation. It is a vertical
slice that can answer one historical rotation date for 20-30 fixture companies:

1. correct calendar cutoff;
2. accepted filings only;
3. unique security/ticker mapping;
4. raw and total-return prices with quality labels;
5. auditable universe exclusions;
6. one deterministic factor ranking;
7. one portfolio transition and ledger day.

Once that slice is hand-verified, scale breadth and history. Scaling first only
makes subtle errors faster.
