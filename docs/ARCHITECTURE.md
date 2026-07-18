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
      │ walk-forward │      │ bands, exits,│       │ static page, │
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
      fsds.py                # SUB/NUM/PRE/TAG ingestion (via secfsdstools)
      companyfacts.py        # live-edge facts w/ fy-fp-trap-safe period logic
      submissions.py         # filing index, acceptanceDateTime, formerNames
      pit_store.py           # MIN(accepted) dedup; PIT vs latest views
      tag_chains.py          # per-concept us-gaap fallback chains (versioned)
      quarterly.py           # Q1-Q3 direct, Q4 = FY − 3×Q1, sanity quarantine
      ttm.py                 # trailing-twelve-month assembly
      tickers.py             # layered CIK↔ticker map incl. delisted recovery
    prices/
      base.py                # provider ABC + canonical parquet schema
      alpaca.py              # primary EOD (raw + all adjustments)
      stooq.py               # bulk cross-check (adjusted-only!)
      tiingo.py              # splitFactor/divCash spot-checks
      eodhd_snapshot.py      # one-time delisted-inclusive archive loader
      actions.py             # split/dividend detection + 3-source reconcile
      adjust.py              # adj factors; raw close never touched
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
    walkforward.py           # purged, embargoed splits
    fragility.py             # parameter-plateau / neighborhood analysis
    experiments.py           # grid runner for experiment_grid.yaml
  ledger/
    nav.py                   # canonical NAV/positions/trades tables
    forward.py               # paper-forward record (frozen-ruleset window)
  delivery/
    snapshot.py              # versioned JSON artifact (schema documented)
    notify.py                # Telegram push (optional, Phase 7)
web/                         # minimal static viewer for the JSON snapshot
                             #   (plain HTML+JS; NOT the MobileInv PWA)
tests/                       # mirrors package; PIT-leak tests mandatory
.github/workflows/           # ci.yml, nightly-data.yml, rotation.yml
docs/                        # this blueprint + PROGRESS.md
data/                        # local DuckDB/Parquet (gitignored)
```

## 3. Storage layout

DuckDB database `data/usinv.duckdb` + Parquet archives. Main tables:

| Table | Key | Notes |
|---|---|---|
| `filings` | `adsh` | from SUB/submissions: cik, form, period, fy, fp, filed, **accepted**, sic, afs |
| `facts_pit` | `(cik, tag, ddate, qtrs, uom)` | value, adsh, filed, accepted — first-accepted only |
| `facts_latest` | same | latest-known view (restatements ok; never used by signals) |
| `fundamentals_q` | `(cik, fiscal_q)` | standardized quarterly concepts + `available_from` (=accepted) |
| `fundamentals_ttm` | `(cik, as_of_q)` | TTM assemblies + `available_from` |
| `ticker_map` | `(cik, ticker, valid_from, valid_to)` | layered sources, `source` column |
| `prices_raw` | `(ticker, date)` | o,h,l,c,v — immutable, provider + ingest batch tagged |
| `prices_adj` | `(ticker, date)` | `split_factor` (splits only) + `tr_factor` (splits+dividends), action_id refs — consumers per DATA_SPEC §4.0 |
| `corporate_actions` | `(ticker, date, type)` | ratio, source(s), reconciliation status |
| `macro_series` | `(series, date, vintage_date)` | vintage-aware (ALFRED where needed) |
| `universe_snapshots` | `(date, cik)` | inclusion + every filter's pass/fail (auditable) |
| `red_flags` | `(date, cik, flag)` | evidence pointer (adsh / detection rule) |
| `scores` | `(date, cik)` | per-factor + composite + regime block used |
| `selections` | `(cycle, position_id)` | continuity model: one row per held position |
| `nav_ledger` | `(date)` | canonical daily NAV + benchmark columns |
| `trades` | `(trade_id)` | model ref price, assumed fill, costs |

Rules: `facts_pit` insert-only; `prices_raw` insert-only; every derived table
carries the code version (`git describe`) that produced it.

## 4. Two-speed data pipeline

- **Historical spine (rare, versioned):** FSDS quarterly ZIPs → parquet →
  `facts_pit`. Archive the ZIPs locally/into state repo — SEC has reprocessed
  the whole archive before (Dec 2024); never assume immutability.
- **Live edge (nightly):** submissions delta → detect new 10-K/10-Q by
  `acceptanceDateTime` → companyfacts fetch for those CIKs only → same dedup
  insert. When the next FSDS drop arrives it verifies/supersedes the edge rows;
  mismatches are logged (they are almost always our own period-derivation bugs).
- **Prices (nightly):** Alpaca bars (raw + all) for the active universe with
  `end ≤ now−15min` (SIP-quality on free tier); weekly Stooq bulk cross-check;
  action detection + reconciliation; then factor computation.

A biweekly-to-monthly rotation needs ~40h of slack at most — nightly batch is
comfortably sufficient; there is deliberately **no intraday infrastructure**.

## 5. Relationship to MobileInv (BIST)

Zero runtime coupling (D012). What we port is *lessons*, listed in
DECISIONS.md, and the general shape (PIT context, canonical ledger, hard gates,
walk-forward discipline). Code is ported by copy-with-tests only if genuinely
identical in semantics; expected for: trailing-stop logic shape, plateau/
fragility analysis shape, snapshot-manifest hygiene. Everything data-touching is
written fresh against US contracts.
