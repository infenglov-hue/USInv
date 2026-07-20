# USInv — Systematic US Equity Picker

**Status: PHASE 2 BUILD — the point-in-time universe gate is in progress.** This repository contains the
design specification for a point-in-time-correct, factor-based US stock
selection research system. It is intentionally not called implementation-ready
until the Phase-0 historical-data feasibility gate is completed and the user
chooses research or audit evidence mode. Build work follows
[docs/CODEX_TASKS.md](docs/CODEX_TASKS.md).

Created 2026-07-17; backbone audit repaired 2026-07-18 before any code or
performance run. External contracts and evidence grades live in
[docs/SOURCE_REGISTER.md](docs/SOURCE_REGISTER.md); re-verify time-sensitive
items and anything marked `[verify]` at implementation time.

## Current build checks

The configuration and calendar checks are fully offline:

```powershell
python -m usinv config-check
pytest -q
```

The EDGAR smoke test makes exactly one submissions request and one companyfacts
request. It requires a real monitored contact address and never prints that
address or the returned payload:

```powershell
$env:USINV_EDGAR_EMAIL = "your-monitored-address@your-domain.tld"
python -m usinv edgar-smoke --refresh
```

For a fresh GitHub runner, store the same value as the repository secret
`USINV_EDGAR_EMAIL`, then manually dispatch the `EDGAR smoke` workflow. Normal
CI is offline and does not consume SEC requests.

The FSDS archive command downloads an inclusive quarter range into ignored,
user-controlled storage. Existing verified quarters are reused by default;
`--refresh` deliberately asks SEC again and records an unchanged or reprocessed
observation without overwriting any older raw ZIP. `2009q1` is retained because
it is part of the official archive, although it contains headers only; filing
rows begin in `2009q2`.

```powershell
$env:USINV_EDGAR_EMAIL = "your-monitored-address@your-domain.tld"
python -m usinv fsds-sync --start 2009q1 --end 2026q1
python -m usinv fsds-sync --start 2009q1 --end 2026q1 --refresh
```

The full range is large. Normal synchronization downloads only missing
quarters; use the refresh form intentionally when auditing SEC replacements.
The manually dispatched `FSDS smoke` workflow exercises only the small
headers-only `2009q1` package, then proves the local object can be reused without
a second request. Normal CI remains fixture-only.

After synchronization, convert an exact archived ZIP version into six
hash-verified Parquet artifacts. Raw SUB/NUM/PRE/TAG values remain strings;
typed facts preserve SEC NUM values as `Decimal128(28,4)` and attach the filing
acceptance timestamp in UTC. Re-running the command verifies and reuses the
same content-addressed result rather than silently rebuilding it:

```powershell
python -m usinv fsds-ingest --start 2009q1 --end 2026q1
```

Use `--archive-as-of <timezone-aware timestamp>` to reproduce the SEC archive
version known at an earlier observation time. This boundary chooses the source
ZIP only; fact availability is always enforced separately from each filing's
`accepted` timestamp.

Build an immutable, content-addressed first-filed/latest snapshot from those
exact normalized quarters:

```powershell
python -m usinv pit-build --start 2009q1 --end 2026q1
```

`facts_pit.parquet` keeps the minimum accepted fact per canonical key and is
the only view admitted by the safe `as_of` reader. Amendments can update the
separate `facts_latest.parquet` but cannot overwrite an older snapshot or a
first-filed row. A repeated command verifies and returns the same snapshot;
equal-time conflicting facts fail closed instead of being guessed.

Measure the complete versioned fundamental concept table against an exact PIT
snapshot and the matching lossless PRE/filings evidence:

```powershell
python -m usinv fundamentals-coverage `
  --facts-pit <snapshot>/facts_pit.parquet `
  --pre <normalized-quarter>/raw/pre.parquet `
  --filings <normalized-quarter>/filings.parquet `
  --sample-quarter 2025q4 `
  --output artifacts/phase-1-4/coverage-2025q4.json `
  --enforce
```

The JSON hashes every Parquet input, reports every core and secondary concept,
and measures strict observed issuer/concept cells over the provisional domestic
filer denominator after the documented financial, REIT, biotech and shell
exclusions. This Phase 1.4 result is an honest diagnostic: `--enforce` returns a
failing status while either threshold is missed, and missing debt, preferred
equity or minority interest is never silently converted to zero. Phase 1.5
implements the D030 fail-closed applicability engine. Per D032, its enforceable
90%/75% empirical gate runs in Phase 2.3 immediately after the date-valid
exchange universe is materialized; a miss blocks later phases.

Detect and ingest already-published periodic filings for one CIK through the
filing-centric live edge:

```powershell
$env:USINV_EDGAR_EMAIL = "your-monitored-address@your-domain.tld"
python -m usinv edgar-live-sync `
  --cik 320193 `
  --as-of 2025-11-01T00:00:00Z `
  --include-history
```

The command archives each selected accession, parses the as-filed XBRL and
presentation evidence, writes immutable full-filing and canonical
`facts_raw.parquet` artifacts, and reports Company Facts parity. Repeating a
seen accession can be prevented with `--seen-accession`; raw filings and
generated Parquet remain in ignored user-controlled storage. The SEC contact is
read only from the environment and is never printed or embedded in artifacts.

Capture one private, dated active+delisted Alpha Vantage membership snapshot:

```powershell
$env:ALPHA_VANTAGE_API_KEY = "your-personal-key"
python -m usinv alpha-listing-sync --as-of 2026-07-17
```

The two calls are paced and stored under ignored, content-addressed local
storage. The command reports only counts and hashes; raw CSV payloads are never
committed. Membership is not identity: universe selection still requires a
unique high-confidence security-master interval for the same ticker, exchange
and date.

Build a discovery-only CIK plan from that private snapshot, then archive
filing-time cover evidence in resumable CIK shards:

```powershell
$env:USINV_EDGAR_EMAIL = "your-monitored-address@your-domain.tld"
python -m usinv sec-filing-discovery `
  --listing-snapshot <listing-snapshot-directory>
python -m usinv sec-cover-bootstrap `
  --discovery-plan <discovery-plan-directory> `
  --as-of 2026-07-17T16:00:00-04:00 `
  --max-ciks 250
```

The current SEC ticker file locates candidate filings only; it never becomes
historical identity evidence. A share class enters the security master only
when an as-filed cover fact accepted by the cutoff supplies a matching ticker,
exchange and class title. Every accession is content-addressed as it is fetched,
so an interrupted shard can resume without publishing a partial security
master; mismatches remain explicit gaps rather than guessed joins.

The account-free historical feasibility checks validate the 36-security sample,
provider contracts and metadata-only evidence matrix; public demo responses are
written only to the ignored artifacts directory:

```powershell
python tools/historical_feasibility.py validate
python tools/historical_feasibility.py render
python tools/historical_feasibility.py live-demo --provider all `
  --output-dir artifacts/phase_0_4/public_demo
```

See [research/phase_0_4/README.md](research/phase_0_4/README.md) for the mixed
demo findings and the user-gated full-sample commands.

## What this system is

A long-only, small/mid-cap tilted, factor-composite stock picker for US equities
(NYSE / NYSE American / Nasdaq), producing a ranked portfolio recommendation on a
fixed rotation calendar. It uses mostly free data (SEC EDGAR fundamentals and
operational price feeds), applies **strict point-in-time discipline**, and targets
**steady risk-adjusted returns** (drawdown control valued over headline CAGR).

It is the US sibling of the MobileInv BIST system — same methodology, zero shared
runtime. See [docs/DECISIONS.md](docs/DECISIONS.md) D012.

The primary product surface will be an independent, mobile-first installable
PWA. It consumes a small versioned snapshot and shows portfolio state, factor
reasons, risk/cash regime, performance and data health without exposing bulk
licensed data or secrets.

## What this system is NOT

- Not a live autotrader during research. Paper orders are automatic; live
  broker submission remains disabled until the fixed paper-forward gate passes
  and the user explicitly enables capital mode. Once enabled, normal operation
  is unattended and does not require a local computer or daily approval.
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
| [docs/EXPERIMENT_PLAN.md](docs/EXPERIMENT_PLAN.md) | Pre-registered static holdout, staged search, success/failure criteria |
| [docs/OPS_SPEC.md](docs/OPS_SPEC.md) | Schedules (Istanbul-based), CI/CD, delivery artifact, monitoring |
| [docs/CODEX_TASKS.md](docs/CODEX_TASKS.md) | Build phases as PR-sized tasks with acceptance gates |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Standing decisions and their rationale |
| [docs/BUILD_GUIDE.md](docs/BUILD_GUIDE.md) | Detailed construction sequence and mental model |
| [docs/SOURCE_REGISTER.md](docs/SOURCE_REGISTER.md) | External contracts, primary links, evidence grade and recheck cadence |
| [docs/LIVE_SYSTEMS_EVIDENCE.md](docs/LIVE_SYSTEMS_EVIDENCE.md) | Cautious live/model evidence map; priors only |
| [docs/PROGRESS.md](docs/PROGRESS.md) | Current build state, verification and blockers |
| [research/phase_0_4/README.md](research/phase_0_4/README.md) | Historical-provider feasibility probe, evidence matrix and decision boundary |

## The one-paragraph summary of the design

A two-speed data pipeline builds an as-first-filed point-in-time fundamentals
store from SEC Financial Statement Data Sets (historical spine, 2009Q2+) plus
nightly EDGAR `submissions`/`companyfacts` deltas (live edge), keyed on filing
**acceptance timestamps**. A security master separates entity, share class and
time-bounded ticker identity. Alpaca/Stooq/Tiingo support operations; the
affordable EODHD route remains a **research** candidate only if written retention
rights are confirmed (the current public terms do not permit the old one-month-
then-keep plan), while audit-grade evidence additionally requires
reconstructable old corporate actions/security identity. A quality + value +
momentum composite (Piotroski as junk veto), guarded by EDGAR-derived hard red
flags (shells, ATM dilution, going concern, delisting-risk), selects ~15 equal-
weight names monthly with a buy/hold rank band, an optional separately ranked
0/3/5-slot large-cap extension, 20-25% trailing stops, and a
200-day SMA trend overlay — all parameters selected by the staged static-holdout
protocol, with a 40bp/side baseline cost inside the objective, then proven in a
minimum 12-month paper-forward window before any capital discussion. A
versioned PWA snapshot is produced by the same canonical ledger used by
backtest, paper and live operation.
