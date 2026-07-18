# USInv — Systematic US Equity Picker

**Status: BLUEPRINT ONLY — no code yet.** This repository currently contains the
complete design specification for a free-data, point-in-time-correct, factor-based
US stock selection system. The build-up is executed by an AI coding agent (Codex)
following [docs/CODEX_TASKS.md](docs/CODEX_TASKS.md).

Created 2026-07-17. All external facts (API shapes, prices, rules, evidence) were
verified against primary sources on that date; re-verify anything marked
`[verify]` at implementation time.

## What this system is

A long-only, small/mid-cap tilted, factor-composite stock picker for US equities
(NYSE / NYSE American / Nasdaq), producing a ranked portfolio recommendation on a
fixed rotation calendar. It fetches **free data only** (SEC EDGAR fundamentals,
free price feeds), applies **strict point-in-time discipline**, and targets
**steady risk-adjusted returns** (drawdown control valued over headline CAGR).

It is the US sibling of the MobileInv BIST system — same methodology, zero shared
runtime. See [docs/DECISIONS.md](docs/DECISIONS.md) D012.

## What this system is NOT

- Not an autotrader. It produces signals/artifacts; order placement is manual.
- Not a promise of returns. Success criteria are defined *before* experiments in
  [docs/EXPERIMENT_PLAN.md](docs/EXPERIMENT_PLAN.md); anything that looks too good
  is treated as a bug (presumed overfit) until proven otherwise.
- Not financial advice. It is a personal research system.

## Document map

| Doc | Contents |
|---|---|
| [AGENTS.md](AGENTS.md) | Entry point + hard rules for AI coding agents |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System overview, module tree, data flow, storage |
| [docs/DATA_SPEC.md](docs/DATA_SPEC.md) | EDGAR/FSDS PIT store, price layer, corporate actions, universe, macro data — fine detail |
| [docs/MODEL_SPEC.md](docs/MODEL_SPEC.md) | Factors, red-flag hard gates, composite, regime overlay, portfolio construction, exits |
| [docs/EXPERIMENT_PLAN.md](docs/EXPERIMENT_PLAN.md) | Pre-registered parameter grid, walk-forward protocol, success/failure criteria |
| [docs/OPS_SPEC.md](docs/OPS_SPEC.md) | Schedules (Istanbul-based), CI/CD, delivery artifact, monitoring |
| [docs/CODEX_TASKS.md](docs/CODEX_TASKS.md) | Build phases as PR-sized tasks with acceptance gates |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Standing decisions and their rationale |

## The one-paragraph summary of the design

A two-speed data pipeline builds an as-first-filed point-in-time fundamentals
store from SEC Financial Statement Data Sets (historical spine, 2009Q2+) plus
nightly EDGAR `submissions`/`companyfacts` deltas (live edge), keyed on filing
**acceptance timestamps**; a free price layer (Alpaca SIP-quality EOD history +
Stooq/Tiingo cross-checks + a one-time ~$20 EODHD delisted-inclusive snapshot for
the honest backtest) feeds a canonical raw+adjusted price store. A quality + value
+ momentum composite (Piotroski as junk veto), guarded by EDGAR-derived hard red
flags (shells, ATM dilution, going concern, delisting-risk), selects ~15 equal-
weight names monthly with a buy/hold rank band, 20-25% trailing stops, and a
200-day SMA trend overlay — all parameters chosen on walk-forward plateaus from a
pre-registered grid, with a 20-40bp/side cost model inside the objective, then
proven in a paper-forward window before any capital decision.
