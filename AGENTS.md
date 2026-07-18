# AGENTS.md — Rules for AI coding agents working on USInv

Read this first, then `docs/BUILD_GUIDE.md`, then `docs/CODEX_TASKS.md` for your
current task and the spec/source-register rows that task references. Do not
read MobileInv repos for context; everything needed is specified here (D012:
zero coupling).

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
   never filter on it in PIT context (see DATA_SPEC §1.3).
3. **Raw prices are immutable and event-time-correct.** Historical price floors,
   ADV, market cap and T-1 collars use contemporaneous raw prices. Stops use a
   split-continuous position basis anchored only through the current as-of
   session; momentum/benchmarks use total return. A future split must never
   rewrite an old eligibility or stop decision. Vendor adjusted close is never
   primary raw data; the explicitly labeled `vendor_frozen` research fallback
   is permitted only under DATA_SPEC §0/§4.2.
4. **Entity, security and ticker are different.** Price/fundamental joins use
   `security_id` and non-overlapping ticker+exchange validity intervals. A CIK
   or ticker alone is never a historical security key. Ambiguous/unmapped rows
   are quarantined and counted.
5. **Data evidence mode is explicit.** `research` and `audit` modes are defined
   in DATA_SPEC §0. The Phase-0 feasibility gate and user choice happen before
   historical implementation. Never call research-mode output audit-grade.
   yfinance remains banned from scheduled/cron paths (D010).
6. **No performance claims without artifacts.** Any number quoted in a doc or
   commit message must come from a committed, reproducible run (config + code
   SHA + data-manifest hash + output hash). Static holdout/search gates in
   EXPERIMENT_PLAN.md are mandatory
   before any parameter becomes a default.
7. **Small PRs, acceptance gates.** One task from CODEX_TASKS.md per PR. Do not
   start the next phase before the current phase's gate is met and the user has
   approved. Wrong changes are reverted by revert PR, never force-push.
8. **Full test suite must pass** (`pytest -q`) on every PR. New modules ship
   with tests. Tests never write into production data directories (use
   `tmp_path`); an artifact-guard test enforces this.
9. **Timezones:** every schedule pins to `America/New_York`; every stored
   timestamp is tz-aware (exchange time or UTC). Istanbul (UTC+3, no DST) is a
   display concern only.
10. **All dates from the exchange calendar** (`exchange_calendars` /
   `pandas_market_calendars`, XNYS). Never derive trading days from weekday
   arithmetic or federal holidays.
11. **Secrets** (Alpaca/Tiingo/FRED keys, Telegram token) live in GitHub Actions
    secrets / local `.env`, never in the repo.

## Working conventions

- Python 3.12, `pyproject.toml`, `ruff` + `pytest` from Phase 0 (unlike
  MobileInv, quality gates exist from day one).
- Storage: DuckDB + Parquet under `data/` (gitignored). Licensed/multi-GB state
  lives in user-controlled storage with checksummed manifests; Git stores no
  bulk vendor payload or mutable database (OPS_SPEC §4).
- Config-driven: every tunable in YAML under `usinv/config/`; defaults =
  documented blueprint values; experimental values only via explicit override.
- Update `docs/PROGRESS.md` (create it on first task) after each merged PR:
  commit SHA, what was done, what remains, how it was verified.
- If reality contradicts this blueprint (an API changed, a field is missing),
  do not silently improvise: implement the closest safe behavior, mark it
  `BLUEPRINT-DEVIATION` in PROGRESS.md, and surface it for user review.
