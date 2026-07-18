# ARCHITECTURE

## 1. System overview

```
                    ┌─────────────────────────────────────────────┐
                    │                DATA LAYER                    │
                    │                                             │
  SEC FSDS (qtrly) ─┤ historical spine ─┐                         │
  EDGAR APIs (nightly)─ live edge ──────┼─► PIT fundamentals store │
  Alpaca / Stooq / ─┤ price providers ──┼─► raw+adjusted prices    │
  Tiingo / EODHD    │ corporate actions ┘                         │
  FRED/ALFRED/CBOE ─┤ macro/regime series (vintage-correct)       │
                    └───────────────┬─────────────────────────────┘
                                    │  freshness + coverage gates
                    ┌───────────────▼─────────────────────────────┐
                    │              SIGNAL LAYER                    │
                    │ universe builder ─► hygiene hard gates ─►    │
                    │ factor scores ─► composite ─► regime overlay │
                    └───────────────┬─────────────────────────────┘
                                    │
              ┌─────────────────────┼──────────────────────┐
              ▼                     ▼                      ▼
      ┌──────────────┐      ┌──────────────┐       ┌──────────────┐
      │   BACKTEST   │      │  PORTFOLIO   │       │   DELIVERY   │
      │ engine+costs │      │ selector,    │       │ JSON snapshot│
      │ staged search│      │ bands, exits,│       │ static page, │
      │ experiments  │      │ continuity   │       │ notifications│
      └──────────────┘      └──────┬───────┘       └──────────────┘
                                   ▼
                            ┌──────────────┐
                            │ NAV / LEDGER │  (single source of truth,
                            │ paper-forward│   backtest = live contract)
                            └──────────────┘
```

Three invariants tie the boxes together:

- **One PIT context.** Backtest and live selection consume the identical
  `as_of(date)` API — there is no separate "backtest data path".
- **One execution contract.** Signal from T-1 official close + fundamentals
  accepted before T-1 session end; fill at T opening auction (LOO w/ collar).
  Backtest fills and the forward ledger use the same contract (MODEL_SPEC §7).
- **One ledger.** Live NAV, backtest NAV, and the delivery artifact all read the
  same canonical ledger tables; no surface computes its own performance.

## 2. Target package tree (built incrementally per CODEX_TASKS.md)

```
usinv/
  cli.py                     # single entrypoint: `usinv <command>`
  config/
    settings.yaml            # paths, providers, api etiquette, tz
    universe.yaml            # filters, exclusions, carve-outs
    factors.yaml             # factor definitions, tag chains version, weights
    portfolio.yaml           # N, bands, caps, stops, rotation cadence
    regime.yaml              # overlay + regime-weight parameters
    experiment_grid.yaml     # THE pre-registered grid (EXPERIMENT_PLAN §3)
  calendar.py                # XNYS wrapper: sessions, half-days, T-1/T mapping
  data/
    edgar/
      client.py              # throttled (≤8 req/s), UA header, retry/backoff
      bulk.py                # FSDS zips + companyfacts.zip/submissions.zip,
                             #   versioned local archive (SEC reprocesses!)
      fsds.py                # lossless SUB/NUM/PRE/TAG + typed filing/fact ingest
      rules/                 # versioned quarterly derivation contracts
      vendor/                # hash-locked upstream standardizer source snapshot
      filing_xbrl.py         # primary live edge incl. custom/dimensional facts
      companyfacts.py        # standard-taxonomy cross-check/backfill
      submissions.py         # filing index, acceptanceDateTime, formerNames
      pit_store.py           # MIN(accepted) dedup; PIT vs latest views
      tag_chains.py          # per-concept us-gaap fallback chains (versioned)
      quarterly.py           # Q1-Q3 direct, Q4 = FY − 3×Q1, sanity quarantine
      ttm.py                 # trailing-twelve-month assembly
      securities.py          # entity/security/symbol lifecycle + collisions
    prices/
      base.py                # provider ABC + canonical parquet schema
      alpaca.py              # primary EOD (raw + all adjustments)
      stooq.py               # bulk cross-check (adjusted-only!)
      tiingo.py              # splitFactor/divCash spot-checks
      historical_archive.py # Phase-0-selected, retention-permitted research loader
      actions.py             # split/dividend detection + 3-source reconcile
      adjust.py              # as-of-anchored factors + quality provenance
    macro/
      fred.py                # BAMLH0A0HYM2, T10Y3M, T10Y2Y, VIXCLS
      alfred.py              # vintage series (NFCI, SAHMREALTIME)
      cboe.py                # VIX_History.csv primary
      proxies.py             # HYG/LQD ratio z-score (HY OAS fallback)
    universe.py              # tradable universe as-of date (rank-based, own)
    freshness.py             # staleness gates (the BIST frozen-data lesson)
  hygiene/
    shells.py                # SIC 6770 now-or-ever, dei:EntityShellCompany
    dilution.py              # S-3 → ATM 8-K → 424B5/424B3 cluster detector
    going_concern.py         # efts.sec.gov full-text screen
    delisting_risk.py        # reverse-split×price-floor rules (2025 rules)
    red_flags.py             # aggregation → HARD selection gate
  scoring/
    context.py               # as_of(date): the only data access for signals
    value.py                 # composite value (EBIT/TEV-style, multi-ratio)
    quality.py               # profitability/accruals; Piotroski veto
    momentum.py              # 12-1 momentum
    composite.py             # rank aggregation, sector-relative option
    sectors.py               # SIC → FF12/FF49; carve-out routing
  regime/
    signals.py               # SMA trend state, HY OAS state, NFCI, vol state
    overlay.py               # exposure scaling (cash %) from trend gate
    weights.py               # regime-conditional factor weights (mom de-weight)
  portfolio/
    selector.py              # constrained selection (caps, correlation)
    bands.py                 # buy top-decile / hold-until-out-of-top-quartile
    rotation.py              # rotation calendar (anchored)
    continuity.py            # position identity, entry anchoring (no re-anchor)
    exits.py                 # trailing stop, red-flag thesis-break, EOD-only
    sizing.py                # equal weight + position cap
  backtest/
    engine.py                # event loop honoring the execution contract
    costs.py                 # 20-40bp/side spread+impact model (in objective)
    metrics.py               # CAGR, Sharpe, Sortino, MaxDD, Ulcer,
                             #   rolling-12m win rate, benchmark-relative
    splits.py                # locked static split + TRAIN-only diagnostics
    fragility.py             # parameter-plateau / neighborhood analysis
    experiments.py           # grid runner for experiment_grid.yaml
  ledger/
    nav.py                   # canonical NAV/positions/trades tables
    forward.py               # paper-forward record (frozen-ruleset window)
  delivery/
    snapshot.py              # versioned JSON artifact (schema documented)
    notify.py                # Telegram push (optional, Phase 7)
web/                         # independent installable PWA; snapshot consumer
                             #   (no MobileInv imports, no scoring logic)
tests/                       # mirrors package; PIT-leak tests mandatory
.github/workflows/           # ci, nightly, decision, reconcile, audit
docs/                        # this blueprint + PROGRESS.md
data/                        # local DuckDB/Parquet (gitignored)
```

## 3. Storage layout

DuckDB database `data/usinv.duckdb` + Parquet archives. Main tables:

| Table | Key | Notes |
|---|---|---|
| `ingest_batches` | `batch_id` | provider, request/response hashes, retrieval time, license/storage pointer, schema version |
| `filings` | `adsh` | from SUB/submissions: cik, form, period, fy, fp, filed, **accepted**, sic, afs |
| `facts_pit` | `(cik, tag, ddate, qtrs, uom)` | value, adsh, filed, accepted — first-accepted only |
| `facts_latest` | same | latest-known view (restatements ok; never used by signals) |
| `fundamentals_q` | `(cik, fiscal_q)` | entity fundamentals; standardized concepts + `available_from` (=accepted) |
| `fundamentals_ttm` | `(cik, as_of_q)` | entity TTM assemblies + `available_from` |
| `securities` | `security_id` | immutable tradeable-class identity; CIK is entity link, not key |
| `security_symbols` | `(security_id, ticker, exchange, valid_from)` | non-overlapping symbol validity intervals + confidence/evidence |
| `prices_raw` | `(security_id, session, provider, batch_id)` | contemporaneous o,h,l,c,v — immutable; vendor ticker retained as evidence |
| `price_factors` | `(security_id, session, anchor_session, method)` | split/TR factors, adjustment-quality and action refs; never future-anchored inside a backtest |
| `corporate_actions` | `(security_id, effective_session, type, source)` | ratio/cash, `known_at`, reconciliation and quality status |
| `macro_series` | `(series, date, vintage_date)` | vintage-aware (ALFRED where needed) |
| `universe_snapshots` | `(date, security_id)` | inclusion + every filter's pass/fail and mapping quality |
| `red_flags` | `(date, security_id, flag)` | evidence pointer (adsh / notice / detection rule) |
| `scores` | `(date, security_id)` | per-factor + composite + regime block used |
| `selections` | `(cycle, position_id)` | continuity model: one row per held position |
| `nav_ledger` | `(date)` | canonical daily NAV + benchmark columns |
| `trades` | `(trade_id)` | model ref price, assumed fill, costs |
| `data_quality_issues` | `issue_id` | mapping/action/coverage/freshness quarantine with resolution state |
| `experiment_runs` | `config_hash` | split, metrics, data/code hashes, status; append-only attempt ledger |

Rules: `facts_pit` insert-only; `prices_raw` insert-only; every derived table
carries the code version (`git describe`) that produced it.

Before PIT dedup exists, each exact FSDS ZIP version has an immutable staging
dataset at
`data/sec/fsds_parquet/YYYYqN/<source_sha256>/`. It contains raw
`sub/num/pre/tag.parquet`, `filings.parquet`, `facts_raw.parquet` and an ingest
manifest with source hash, adapter/dependency versions, row counts, Arrow
schemas and artifact hashes. Raw columns are strings; typed NUM facts are
`Decimal128(28,4)`. A second run verifies every artifact before declaring a
cache hit, while an SEC-reprocessed ZIP produces a different directory.

PIT materialization is also immutable rather than a mutable database upsert.
The sorted normalized-batch descriptors determine
`data/sec/pit_store/snapshots/<snapshot_id>/`, which contains
`facts_pit.parquet`, `facts_latest.parquet` and a hash/schema/count manifest.
DuckDB is an ephemeral out-of-core query engine for this build; it is not the
canonical state. Adding an amendment or a backfilled source creates a new
snapshot and leaves every prior snapshot byte-for-byte untouched. The safe
reader accepts only the `facts_pit` schema metadata and an explicit timezone-
aware cutoff, so accidentally passing the latest view fails structurally.

## 3.1 Evidence-mode boundary

The architecture supports two modes defined in DATA_SPEC §0. `research` mode
may consume a frozen vendor-adjusted return series where old delisted action
history is unavailable; `audit` mode requires independently reconstructable
actions and a high-coverage historical security master. Mode and coverage
statistics are first-class fields in the ledger, experiment manifest and every
delivery artifact. No code path may relabel research evidence as audit evidence.

## 4. Two-speed data pipeline

- **Historical spine (rare, versioned):** FSDS quarterly ZIPs → lossless raw
  Parquet + typed filing/fact Parquet →
  `facts_pit`. Archive the ZIPs in user-controlled storage — SEC has reprocessed
  the whole archive before (Dec 2024); never assume immutability.
  `data/sec/fsds/manifest.json` is an append-only logical ledger of checks;
  immutable payloads live at `objects/YYYYqN/<sha256>.zip`. An unchanged check
  appends provenance but reuses the object; a changed hash creates a new object
  linked to the prior hash. `checked_at` selects a reproducible archive version
  only — fact eligibility still comes from each filing's SEC acceptance time.
  The safe raw-fact reader admits only `accepted ≤ as_of` and consolidated
  facts (`coreg` and `segments` both empty); it deliberately never filters
  retroactive `prevrpt`. Phase 1.3 performs first-accepted PIT dedup.
- **Live edge (nightly):** submissions delta → detect new 10-K/10-Q by
  `acceptanceDateTime` → archive/parse the filing's as-filed XBRL instance and
  presentation metadata → companyfacts cross-check → same dedup insert. When
  the next FSDS drop arrives it verifies/supersedes the edge rows;
  mismatches are logged (they are almost always our own period-derivation bugs).
- **Prices (nightly):** Alpaca bars (raw + all) for the active universe with
  `end ≤ now−15min` (SIP-quality on free tier); weekly Stooq bulk cross-check;
  action detection + reconciliation; then factor computation.

A biweekly-to-monthly rotation needs ~40h of slack at most — nightly batch is
comfortably sufficient; there is deliberately **no intraday infrastructure**.

Historical experiments do not run in scheduled GitHub Actions. They run in a
controlled local/batch environment against a frozen data manifest during the
research/build stage. After deployment, production is fully unattended and
has zero dependency on the developer PC: GitHub Actions orchestrates
incremental live data, validation, scoring, paper/live order staging, fill
reconciliation, ledger updates and small delivery artifacts. Durable runtime
state lives in remote object/database storage reachable from a fresh runner;
multi-gigabyte licensed archives stay in separately controlled storage.

GitHub cron is treated as an unreliable trigger, not as state or proof of
execution. Primary and retry schedules use America/New_York time, acquire a
remote idempotency lock keyed by business date and operation, and exit quickly
when work is already complete. An independent heartbeat monitor alerts on a
missing success signal and may trigger the same idempotent workflow through
`workflow_dispatch`. Broker orders use deterministic client-order IDs so a
retry cannot create a duplicate position.

## 5. Relationship to MobileInv (BIST)

Zero runtime coupling (D012). What we port is *lessons*, listed in
DECISIONS.md, and the general shape (PIT context, canonical ledger, hard gates,
holdout/search discipline). Code is ported by copy-with-tests only if genuinely
identical in semantics; expected for: trailing-stop logic shape, plateau/
fragility analysis shape, snapshot-manifest hygiene. Everything data-touching is
written fresh against US contracts.
