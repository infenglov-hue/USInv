# CODEX_TASKS — build plan (PR-sized, gated)

Rules of engagement: one task per PR; acceptance gate met + user approval
before the next phase; full pytest on every PR; update `docs/PROGRESS.md`
after each merge (SHA, done, remaining, verification). Deviations from the
blueprint are marked `BLUEPRINT-DEVIATION` and surfaced, never silent.

Spec references: A=ARCHITECTURE, D=DATA_SPEC, M=MODEL_SPEC, E=EXPERIMENT_PLAN,
O=OPS_SPEC, B=BUILD_GUIDE, S=SOURCE_REGISTER.

## Phase 0 — Contracts, scaffold and feasibility (4 PRs)

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
- 0.4 **Historical-data feasibility spike (USER-GATED before any purchase):**
  implement a throwaway/isolated probe, not production ingestion, over the
  stratified ≥30-security sample in D§0. Archive provider capability metadata
  and permitted sample responses; build the coverage matrix for OHLCV,
  actions, listing lifecycle and identity. Verify the EODHD plan/price/license,
  Alpha Vantage historical listing output and any candidate audit-grade source.
  **Gate:** user selects `research` or `audit` mode and approves any spend;
  `docs/PROGRESS.md` records the decision, missing fields and consequences.
  No claim of an "honest backtest" is allowed before this gate.

## Phase 1 — Fundamentals spine (4-5 PRs)

- 1.1 FSDS bulk download + versioned archive (`bulk.py`): all quarterly zips,
  checksums, immutability ledger (SEC reprocess detection) (D§1).
- 1.2 FSDS ingestion against the pinned secfsdstools schema contract → lossless
  SUB/NUM/PRE/TAG, `filings` + raw facts parquet; vendor the exact standardizer
  source snapshot and register the Q4 rule contract we depend on (D§1.2).
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
  case); input-hashed strict observed-presence report over the FULL concept
  table of D§1.4 on a sample quarter. Threshold misses remain visible and
  missing facts are never converted to zero (D030).
- 1.5 Historical security master (`securities.py`): entity/security/symbol
  tables, validity intervals, collision quarantine and evidence confidence
  (D§3). Current SEC tickers are live-edge only. Filing-centric live-edge
  ingest (`submissions.py`, `filing_xbrl.py`) archives as-filed XBRL including
  custom/dimensional facts; `companyfacts.py` is a standard-taxonomy cross-
  check with fy/fp-trap-safe period derivation (D§2).
  **Gate:** retrospective parity test — ingest an ALREADY-PUBLISHED past
  quarter via the API path and diff against that quarter's FSDS data
  (mismatches logged + explained); do NOT wait for the next quarterly SEC
  drop. PIT-leak test suite green. Build the versioned applicability matrix
  over the final date-valid v1 exchange universe; enforce core ≥90% and
  secondary ≥75%, with explicit tested structural-zero evidence and mandatory
  scoring inputs fail-closed (D030).

## Phase 2 — Prices & universe (4 PRs)

- 2.1 Price provider ABC + Alpaca (`base.py`, `alpaca.py`): raw+all bars,
  end≤now−15min, security-keyed canonical parquet, insert-only `prices_raw`
  with bar-definition metadata (D§4.0-4.3).
  **Day-one empirical check:** Alpaca corporate-actions endpoint availability
  on the free tier — record the answer in PROGRESS.md (D§5).
- 2.2 Stooq bulk + Tiingo spot-check providers; action detector + 3-source
  reconciliation + as-of-anchored `adjust.py` (raw never touched) (D§4.0, §5).
  **Gate:** synthetic split/dividend fixtures; future reverse split cannot
  change a historical $2 filter or stop; a real recent reverse-split name
  reconciles across sources; declared ex-date is consumed without a record-
  date formula; unresolved adjustments are rejected in audit mode.
- 2.3 Date-specific Alpha Vantage listing ingest + universe builder
  (`universe.py`) + `universe_snapshots` with per-filter audit trail and §3
  mapping coverage gates (D§3, §6); FF12/FF49 SIC mapping
  (`scoring/sectors.py`). Note:
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
  dilution chain with issuance corroboration (a 424B5 alone never proves
  selling), going-concern full-text (NOTE: the raw phrase query matches
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
- 4.4 Locked static splits + optional TRAIN-only rolling diagnostics +
  fragility + grid runner (`splits.py`, `fragility.py`, `experiments.py`)
  reading `experiment_grid.yaml` (E§1-4). Implement all three search stages,
  exact run-count deduplication and metric-specific plateau tolerances.
  **Gate:** purge/embargo verified by construction tests; TEST runner refuses
  an unregistered config/unlock; grid runner resumes after interruption; every
  cell carries config hash, data-manifest hash and code SHA.

## Phase 5 — Frozen historical data + first real experiments (GATED)

- 5.1 Acquire the Phase-0-selected historical mode. In research mode, acquire
  the retention-permitted archive selected at Phase 0: active+delisted symbol
  lists, raw OHLCV, adjusted_close and every **available** split/dividend
  endpoint. If EODHD grants the required written retention rights and is
  selected, preserve its documented pre-2018 delisted-action gap and
  `vendor_frozen` quality labels.
  In audit mode, implement the separately approved source contract. Store
  licensed payloads in user-controlled storage, never the Git repo, then
  execute the license/retention/renewal checklist (D§0, §4.2; O§4).
  **Gate:** price rows join through security validity windows (never bare
  ticker/CIK); recycled-ticker fixture resolves; unmatched/action-unresolved
  rows are counted; audit thresholds pass or the report is stamped RESEARCH.
- 5.2 US factor attribution from scratch (NOT BIST weights): per-sleeve IC and
  portfolio-level contribution on TRAIN only → propose `factors.yaml`
  candidates (M§1).
- 5.3 Freeze `data_manifest.json`; run E§3 Stages 1-3 on TRAIN/VALIDATION;
  execute neighborhood/ablation controls, actual-trial deflated Sharpe and
  confidence intervals; register exactly one final config hash.
- 5.4 ONE TEST run of the chosen config (E§4.5, §5); pass/fail verdict per the
  fixed criteria; overfit checks if too good.
  **Gate:** user reads the report and explicitly approves proceeding.

## Phase 6 — Unattended pipeline, broker-paper parity & PWA (4 PRs)

- 6.1 `nightly-data.yml` + `decision.yml` + `fill-reconcile.yml` end-to-end on
  GitHub Actions with durable remote runtime state, primary/retry idempotency,
  external heartbeat and stale-artifact kill switch (O§2-5). No production
  dependency on a local computer or self-hosted desktop runner.
- 6.2 Versioned `snapshot.json` contract + delivery adapter. Golden contract
  tests prove the PWA cannot infer or recompute selection and that no licensed
  bulk/provider secret/broker credential field can enter the artifact.
- 6.3 Independent installable mobile-first PWA: portfolio and cash, ranked
  candidates with factor reasons, macro/regime, performance, order/fill state,
  data health, offline last-verified snapshot and explicit stale mode. It shares
  no runtime or source imports with MobileInv.
- 6.4 Telegram notifications + weekly audit + paper broker adapter using
  deterministic client-order IDs and the same preflight/reconciliation
  contract later used by live mode.
  **Gate:** five consecutive nightly runs green; a simulated rotation produces
  a correct fresh paper order set and PWA artifact before the broker cutoff;
  duplicate and dropped-schedule drills create no duplicate order.

## Phase 7 — Paper-forward window (no code, discipline)

- Freeze config (hash pinned); run ≥12 rotations and ≥12 months per E§6;
  weekly integrity job;
  amendment log for anything that happens. Capital decisions out of scope
  until the explicit D024 gate.

## Phase 8 — Explicit live-capital activation (USER-GATED)

- Verify broker/account/funding behavior, secret scopes, settled-cash policy,
  order collars, deterministic client-order IDs, kill switch and recovery runbook.
- User explicitly approves capital mode and initial capital limit. Enabling live
  mode changes execution destination only; it may not change model/config hash.
- Normal operation remains unattended. Any data-integrity or reconciliation
  ambiguity fails closed, preserves evidence and alerts.

## Deliberately NOT in scope (v1)

Intraday strategy signals; FPIs/ADRs; financials/REITs scoring models; ML
models; options; short side; reuse or coupling of the MobileInv PWA/runtime.
