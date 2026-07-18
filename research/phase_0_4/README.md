# Phase 0.4 — historical-data feasibility

This directory is the bounded, throwaway feasibility spike required before any
historical provider is purchased or any backtest is called honest. It is not a
production price adapter and nothing here selects stocks.

## What is committed

- `awkward_securities.json`: 36 deliberately awkward securities, four in each
  required stratum. The labels are probe hypotheses anchored to SEC entity
  pages, not a production security master.
- `provider_contracts.json`: field-by-field contract review for EODHD, Alpha
  Vantage and CRSP.
- `observations.json`: hashes and validation metadata from public demos. No
  provider payload rows or credentials are committed.
- `generated/coverage_matrix.csv`: 108 provider/security rows. A documented
  capability remains documented—not observed—until a sample-scoped probe says
  otherwise.
- `generated/coverage_summary.json` and `generated/REPORT.md`: deterministic
  summaries of the three inputs above.

Raw demo responses are archived under the gitignored
`artifacts/phase_0_4/public_demo/` directory on the machine that performed the
probe. The capture manifest redacts API tokens. A raw capture inside a tracked
source directory is rejected by code and regression test.

## Reproduce the no-account checks

From the repository root:

```powershell
.\.venv\Scripts\python.exe tools\historical_feasibility.py validate
.\.venv\Scripts\python.exe tools\historical_feasibility.py render
.\.venv\Scripts\python.exe tools\historical_feasibility.py live-demo `
  --provider all `
  --output-dir artifacts\phase_0_4\public_demo
```

The live demo intentionally has mixed outcomes. On 2026-07-18, EODHD returned
AAPL OHLCV, one split and four dividends but denied ID mapping and the delisted
list to the demo entitlement. Alpha Vantage returned a valid 425-row historical
delisted CSV for 2014-07-10, while the matching active demo returned an empty
JSON object rather than the documented CSV. Those failures are evidence, not
test results to suppress.

## Credentialed sample probes (not yet run)

The full probes read secrets only from environment variables, bound their date
windows, redact URLs in metadata and write raw responses only to the explicitly
supplied ignored directory:

```powershell
$env:ALPHA_VANTAGE_API_KEY = '<personal key>'
.\.venv\Scripts\python.exe tools\historical_feasibility.py live-sample `
  --provider alpha_vantage `
  --as-of 2026-07-17 `
  --output-dir artifacts\phase_0_4\alpha_vantage_sample

$env:EODHD_API_TOKEN = '<licensed token>'
.\.venv\Scripts\python.exe tools\historical_feasibility.py live-sample `
  --provider eodhd `
  --as-of 2026-07-17 `
  --output-dir artifacts\phase_0_4\eodhd_sample
```

No account has been created, no secret has been requested and no purchase has
been made. EODHD's full 36-security probe should not be run until the user has
approved the plan and the retention terms have been resolved in writing.

## Current decision boundary

The historical evidence mode remains `undecided`:

- EODHD is technically plausible for research and inexpensive at the displayed
  plan price, but pre-2018 delisted actions are absent and the current public
  terms require data deletion within one month after subscription expiry. The
  former one-month-then-keep snapshot design is therefore prohibited without a
  written retention override.
- Alpha Vantage is useful for date-specific universe membership after 2010 but
  is not a permanent security master and needs a personal free key for the full
  active/delisted sample measurement.
- CRSP documents the strongest action/delisting/identity evidence of the three,
  but requires institutional access, has no posted individual price and does
  not by itself provide the opening field needed for full daily OHLCV.

The next user decision is material, so this phase stops at that boundary: choose
the affordable research route (after retention is resolved) or authorize an
audit-source search/budget. Until then, no honest backtest claim is allowed.
