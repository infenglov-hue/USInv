# CODEX_TASKS — build plan (PR-sized, gated)

Rules of engagement: one task per PR; acceptance gate met + user approval
before the next phase; full pytest on every PR; update `docs/PROGRESS.md`
after each merge (SHA, done, remaining, verification). Deviations from the
blueprint are marked `BLUEPRINT-DEVIATION` and surfaced, never silent.

Spec references: A=ARCHITECTURE, D=DATA_SPEC, M=MODEL_SPEC, E=EXPERIMENT_PLAN,
O=OPS_SPEC.

## Phase 0 — Scaffold (2-3 PRs)

- 0.1 Repo scaffold: `pyproject.toml` (py3.12), package tree stubs, ruff +
  pytest + pre-commit, `ci.yml`, `.gitignore` (data/, .env), config loader
  (YAML → typed dataclasses; every default from the blueprint tables).
- 0.2 Calendar module (`calendar.py`): XNYS sessions, half-day detection,
  `prev_session/next_session`, T-1/T mapping, rotation generator implementing
  the **shift rule of O§1** (fixed anchor sequence, shift to next eligible
  full session, never re-base).
  **Gate:** unit tests pass for the documented traps — Good Friday, Fri
  2026-07-03 full closure, a 13:00 half-day, Juneteenth pre/post-2022, DST
  boundary weeks, MLK-Monday rotation shift, Thanksgiving-week signal-date
  rule (O§1).
- 0.3 EDGAR client (`data/edgar/client.py`): throttle ≤8 req/s, UA header from
  config, retry/backoff on 403/429, local response cache.
  **Gate:** live smoke test fetches one submissions JSON + one companyfacts
  JSON politely; recorded fixtures for offline tests.

## Phase 1 — Fundamentals spine (4-5 PRs)

- 1.1 FSDS bulk download + versioned archive (`bulk.py`): all quarterly zips,
  checksums, immutability ledger (SEC reprocess detection) (D§1).
- 1.2 FSDS ingestion via secfsdstools → `filings` + raw facts parquet; vendor
  the Q4-derivation + standardizer rule tables we depend on (D§1.2).
- 1.3 PIT store (`pit_store.py`): MIN(accepted) dedup → `facts_pit`
  (insert-only) + `facts_latest`.
  **Gate:** property tests — amendment rows never mutate PIT; a synthetic
  restatement is visible in latest and invisible in PIT; `prevrpt` unused in
  any PIT code path (grep-test).
- 1.4 Tag chains + quarterly + TTM (`tag_chains.py`, `quarterly.py`, `ttm.py`):
  fallback chains (versioned), Q4=FY−3Q with quarantine rules, 20-F exclusion,
  TTM with `available_from` = max(accepted) (D§1.4-1.5).
  **Gate:** golden tests on 5 hand-verified companies (one calendar FY, one
  offset FY, one restater, one custom-tag-revenue small cap, one quarantine
  case); coverage report over the FULL concept table of D§1.4 on a sample
  quarter (core concepts ≥90%, secondary ≥75%).
- 1.5 Ticker map (`tickers.py`): 5-layer resolution incl. delisted recovery;
  unmapped-CIK log (D§3). Submissions live-edge ingest (`submissions.py`,
  `companyfacts.py`) with fy/fp-trap-safe period derivation (D§2).
  **Gate:** retrospective parity test — ingest an ALREADY-PUBLISHED past
  quarter via the API path and diff against that quarter's FSDS data
  (mismatches logged + explained); do NOT wait for the next quarterly SEC
  drop. PIT-leak test suite green.

## Phase 2 — Prices & universe (4 PRs)

- 2.1 Price provider ABC + Alpaca (`base.py`, `alpaca.py`): raw+all bars,
  end≤now−15min, canonical parquet, insert-only `prices_raw` (D§4.1, §4.3).
  **Day-one empirical check:** Alpaca corporate-actions endpoint availability
  on the free tier — record the answer in PROGRESS.md (D§5).
- 2.2 Stooq bulk + Tiingo spot-check providers; action detector + 3-source
  reconciliation + `adjust.py` (raw never touched) (D§5).
  **Gate:** synthetic split/dividend fixtures; a real recent reverse-split
  name reconciles across sources; ex-date=record-date convention encoded.
- 2.3 Universe builder (`universe.py`) + `universe_snapshots` with per-filter
  audit trail (D§6); FF12/FF49 SIC mapping (`scoring/sectors.py`). Note:
  hygiene filter (D§6 step 7) is a **pass-through stub** in this phase — wired
  for real in 3.1; the `universe_snapshots` schema reserves its audit columns
  now.
- 2.4 Freshness + coverage gates (`freshness.py`) incl. the Alpha Vantage
  LISTING_STATUS delisted-audit; wire as red CI failures in `nightly-data.yml`
  (D§6, §8; O§3).
  **Gate:** kill-switch demo — artificially stale fixture makes the job exit
  red and block delivery.

## Phase 3 — Hygiene & signals (4 PRs)

- 3.1 Hygiene gates (`hygiene/*`): shells (SIC-ever + cover-page flag), ATM
  dilution chain, going-concern full-text (NOTE: the raw phrase query matches
  ASU 2014-15 boilerplate and negations — "no substantial doubt", "alleviated"
  — a negation/alleviation heuristic is REQUIRED, not optional), delisting-
  clock, suspensions (M§3). **Gate:** each gate has a known-positive and
  known-negative real fixture company (incl. an "alleviated going concern"
  negative); every exclusion writes an evidence pointer.
- 3.2 PIT scoring context (`scoring/context.py`): the single `as_of(date)` API
  (fundamentals TTM, prices, macro, universe) — used by BOTH backtest and
  live. **Gate:** leak test — shifting all `available_from` +7d changes
  scores; accessing anything newer than as-of raises.
- 3.3 Factor sleeves + composite (`value.py`, `quality.py`, `momentum.py`,
  `composite.py`) per M§1; Piotroski veto; sector-relative option.
- 3.4 Regime signals + overlay + weights (`regime/*`) per M§4 with
  vintage-correct macro (ALFRED NFCI, SAHMREALTIME, HY OAS archive + HYG/LQD
  fallback). **Gate:** regime series reproduce known historical states
  (2020-03 risk-off, 2022 bear, 2023 chop) from vintage data only.

## Phase 4 — Portfolio & backtest (4 PRs)

- 4.1 Selector + bands + rotation + continuity + sizing (`portfolio/*`) per
  M§5; turnover hard cap.
- 4.2 Exits (`exits.py`): trailing stop EOD-only, thesis-break on gate firing
  (M§6).
- 4.3 Backtest engine + cost model (`backtest/engine.py`, `costs.py`)
  honoring the execution contract incl. LOO-collar fill rule, unfilled-order
  handling, AND the **position-termination rule for delisted holdings**
  (M§6 "Position termination" — merger vs involuntary, −30% haircut,
  sensitivity reporting) (M§7); metrics module (E§2).
  **Gate:** a hand-computable 3-stock toy backtest matches a spreadsheet to
  the cent; cost model applied inside the loop, not post-hoc; a synthetic
  delisting fixture exercises both termination branches.
- 4.4 Walk-forward + fragility + grid runner (`walkforward.py`,
  `fragility.py`, `experiments.py`) reading `experiment_grid.yaml` (E§1-4).
  **Gate:** purge/embargo verified by construction tests; grid runner resumes
  after interruption; every cell output carries config hash + code SHA.

## Phase 5 — Honest data + first real experiments (GATED: user approval)

- 5.1 EODHD one-month snapshot (~$20 — the sanctioned paid item, D001): bulk
  pull of raw OHLCV + adjusted_close + **splits + dividends endpoints**,
  parquet archive into state repo, cancel checklist (D§4.2).
  **Gate:** ticker↔CIK join uses ticker_map validity windows (never bare
  ticker); a recycled-ticker collision fixture (one ticker, two CIKs, two
  eras) resolves correctly; unmatched price rows quarantined + counted.
- 5.2 US factor attribution from scratch (NOT BIST weights): per-sleeve IC and
  portfolio-level contribution on TRAIN only → propose `factors.yaml`
  candidates (M§1).
- 5.3 Run the pre-registered grid (E§3) on TRAIN/VALIDATION; plateau
  selection; controls comparison; write the full report.
- 5.4 ONE TEST run of the chosen config (E§4.5, §5); pass/fail verdict per the
  fixed criteria; overfit checks if too good.
  **Gate:** user reads the report and explicitly approves proceeding.

## Phase 6 — Live pipeline & delivery (3 PRs)

- 6.1 `nightly-data.yml` + `rotation.yml` end-to-end on GitHub Actions with
  state repo, health lines, stale-artifact banner (O§2-3).
- 6.2 Snapshot artifact + minimal static `web/` viewer (O§4).
- 6.3 Telegram notifications + weekly audit workflow (O§5).
  **Gate:** five consecutive nightly runs green; a simulated rotation Monday
  produces a correct, fresh artifact before 09:00 ET.

## Phase 7 — Paper-forward window (no code, discipline)

- Freeze config (hash pinned); run ≥6 rotations per E§6; weekly integrity job;
  amendment log for anything that happens. Capital decisions out of scope
  (D009).

## Deliberately NOT in scope (v1)

Broker API auto-execution; intraday anything; FPIs/ADRs; financials/REITs
scoring models; ML models; options; short side; the MobileInv PWA.
