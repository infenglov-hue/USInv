# AGENTS.md — Rules for AI coding agents working on USInv

Read this first, then `docs/CODEX_TASKS.md` for your current task, then the spec
doc that task references. Do not read MobileInv repos for context; everything you
need is specified here (D012: zero coupling).

## Hard rules (non-negotiable)

1. **Point-in-time above all.** Unified availability rule (identical in
   DATA_SPEC §1.3 and MODEL_SPEC §7): a fact is usable by any signal whose
   timestamp is **≥ the fact's availability instant** — EDGAR
   `acceptanceDateTime` for fundamentals, official publication time for macro
   series, session close for prices. The rotation signal is timestamped at the
   T-1 official session close (16:00 ET; 13:00 ET on half-days), so it admits
   fundamentals with `accepted ≤ T-1 close`. Every PR that touches data flow
   must include at least one test that would fail on look-ahead leakage.
2. **As-first-filed.** The PIT store keeps the first-accepted value per
   `(cik, tag, ddate, qtrs, uom)`. Restated/amended values go to the separate
   `latest` view, never overwrite PIT rows. `prevrpt` is retroactive lookahead —
   never filter on it in PIT context (see DATA_SPEC §2.3).
3. **Raw prices are immutable.** Adjustments live in separate columns/tables,
   and there are TWO of them with different consumers (DATA_SPEC §4.0):
   `split_factor` (splits only — used by price filters, stops, collars, market
   cap) and `tr_factor` (splits+dividends — used by momentum and benchmarks).
   Never store a vendor's adjusted close as the primary price (vendors rewrite
   history on every dividend).
4. **Free-data policy.** The only sanctioned paid item is the one-month EODHD
   snapshot (DECISIONS D001). yfinance is banned from any scheduled/cron path
   (D010) — manual/prototype use only.
5. **No performance claims without artifacts.** Any number quoted in a doc or
   commit message must come from a committed, reproducible run (config + code
   SHA + output hash). Walk-forward gates in EXPERIMENT_PLAN.md are mandatory
   before any parameter becomes a default.
6. **Small PRs, acceptance gates.** One task from CODEX_TASKS.md per PR. Do not
   start the next phase before the current phase's gate is met and the user has
   approved. Wrong changes are reverted by revert PR, never force-push.
7. **Full test suite must pass** (`pytest -q`) on every PR. New modules ship
   with tests. Tests never write into production data directories (use
   `tmp_path`); an artifact-guard test enforces this.
8. **Timezones:** every schedule pins to `America/New_York`; every stored
   timestamp is tz-aware (exchange time or UTC). Istanbul (UTC+3, no DST) is a
   display concern only.
9. **All dates from the exchange calendar** (`exchange_calendars` /
   `pandas_market_calendars`, XNYS). Never derive trading days from weekday
   arithmetic or federal holidays.
10. **Secrets** (Alpaca/Tiingo/FRED keys, Telegram token) live in GitHub Actions
    secrets / local `.env`, never in the repo.

## Working conventions

- Python 3.12, `pyproject.toml`, `ruff` + `pytest` from Phase 0 (unlike
  MobileInv, quality gates exist from day one).
- Storage: DuckDB + Parquet under `data/` (gitignored). Large state lives in the
  private `USInv-state` repo once it exists (OPS_SPEC §4).
- Config-driven: every tunable in YAML under `usinv/config/`; defaults =
  documented blueprint values; experimental values only via explicit override.
- Update `docs/PROGRESS.md` (create it on first task) after each merged PR:
  commit SHA, what was done, what remains, how it was verified.
- If reality contradicts this blueprint (an API changed, a field is missing),
  do not silently improvise: implement the closest safe behavior, mark it
  `BLUEPRINT-DEVIATION` in PROGRESS.md, and surface it for user review.
