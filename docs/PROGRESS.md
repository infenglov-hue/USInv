# PROGRESS

## 2026-07-18 — Phase 0.4 historical-data feasibility (research mode selected)

### Feasibility assets

- Added an isolated `tools/historical_feasibility.py` probe; no feasibility
  code is imported by the production data package.
- Registered 36 deliberately awkward securities, four per required stratum:
  active, acquired, bankrupt, OTC-moved, ticker-recycled, multi-class,
  reverse-split, pre-2018 delisted and post-2018 delisted.
- Added a 108-row provider/security coverage matrix covering raw OHLCV,
  open/close semantics, adjusted prices/method, splits, dividends, listing and
  delisting lifecycle, historical exchange/type, identifiers and ticker
  validity for EODHD, Alpha Vantage and CRSP.
- Separated `documented`, sample-scoped `observed`, `observed_gap`, `blocked`
  and `invalid`; provider marketing/docs cannot silently become measured
  sample coverage.
- Added credentialed full-sample commands for EODHD and Alpha Vantage. Secrets
  are environment-only, URLs are redacted, date windows are bounded, and raw
  payloads can be written only under ignored `artifacts/`/`data/` paths.
- Archived permitted public-demo payloads locally under ignored artifacts and
  committed only schema, row count, retrieval time, SHA-256 and validation
  results.

### Contract/live findings

- EODHD public demo: AAPL OHLCV (3 rows), split (1) and dividends (4) passed
  schema checks; ID mapping and the US delisted list both returned HTTP 403.
- Alpha Vantage public demo: the 2014-07-10 delisted CSV returned 425 rows with
  the documented seven columns, all `Delisted`, and no date later than the
  requested cutoff. The same date's active demo returned `{}` and is recorded
  invalid rather than treated as empty coverage.
- Alpha Vantage personal-key probe: pacing the two calls by 15 seconds returned
  valid seven-column snapshots with 14,207 active and 9,350 delisted rows. The
  36-security sample matched 24 cases: active/acquired/ticker-recycled/
  multi-class/reverse-split were each 4/4, bankrupt was 1/4, OTC-moved 0/4,
  deliberately pre-2010 delisted 0/4 and post-2018 delisted 3/4. Two recycled
  symbols matched both snapshots, directly confirming the permanent-identity
  requirement.
- CRSP's current official contract documents permanent security identifiers,
  dated name/ticker/exchange history, detailed distributions and structured
  delisting reason/return. Access is institutional/quote-based, and the
  reviewed daily contract does not include an opening-price field.
- EODHD displayed its personal EOD plan at $19.99/month with 100k calls/day,
  but its current public terms require deletion within one month after expiry
  and prohibit redistribution/display. No purchase was made.

### Verification so far

- `ruff check .` and `ruff format --check .` — passed.
- `pytest -q` — 58 passed offline, including 15 Phase-0.4 tests.
- All pre-commit hooks passed; all relative Markdown links resolve.
- Public-demo and Alpha Vantage sample hashes/metadata are in
  `research/phase_0_4/observations.json`; raw provider rows and credentials are
  not in Git. The personal key remains only in GitHub Secrets.
- The feasibility-specific tests cover the ≥30/strata gate, namespaced
  security identities, complete capability schema, raw-artifact containment,
  secret redaction, sample-only evidence promotion and a post-cutoff
  look-ahead rejection.
- Deterministic input manifest SHA-256:
  `a25783cb38525cc46e4d69cd94b09f3295865048bed54d91c2bde106e60180d5`.
- The user selected `research`; `usinv/config/settings.yaml` now records that
  choice. This does not unlock live execution or permit a historical backtest
  before the remaining archive gate passes. Current config SHA-256:
  `fde336ca86211e8f4931c1d523c08c022513afe996050d74610fa492f356f5f7`.

### Required to close Phase 0.4

1. Resolve EODHD post-expiry retention in writing and then run its 36-security
   probe, maintain the subscription for the required reproduction period, or
   select another retention-permitted research archive.
2. Import that archive's metadata-only observations, regenerate the matrix and
   record missing-field consequences. No honest backtest claim before this.

### BLUEPRINT-DEVIATION

- The former one-month EODHD subscription → freeze forever → cancel plan
  conflicts with the provider's public terms checked on 2026-07-18. D026 now
  prohibits that route without written retention rights. This is a licensing
  correction, not a performance-driven model change.

## 2026-07-18 — Phase 0.3 polite EDGAR client (merged as `62438e0`)

### Done

- Added a declared `USInv/<version> <monitored-email>` User-Agent sourced only
  from `USINV_EDGAR_EMAIL`; missing or placeholder contact fails before any
  network request.
- Added CIK-normalized `submissions` and `companyfacts` clients, capped at the
  configured 8 requests/second.
- Added exponential retry/backoff for 403/429/transient 5xx and network errors,
  including bounded `Retry-After` support.
- Added atomic response caching with SHA-256 verification, separate retrieval
  and validation timestamps, ETag/Last-Modified conditional requests and loud
  corruption failures.
- Added `usinv edgar-smoke` and an isolated manual GitHub Actions smoke workflow.
  CI remains fixture-only and never depends on SEC availability or a secret.

### Verification

- `ruff check .` and `ruff format --check .` — passed.
- `pytest -q` — 43 passed offline. Tests cover contact/rate gates, exact URLs,
  cache reuse/corruption, conditional revalidation, 403/429 backoff, permanent
  errors, malformed JSON and safe CLI output.
- User approved a monitored contact; it is stored as the new repository's
  `USINV_EDGAR_EMAIL` secret and is not present in code or test output.
- Live local smoke passed for Apple CIK `0000320193`: submissions SHA-256
  `ea2aa552e984a29e920cf80e0827cf32b632563935f029b6ec9de7f4fa3c026d` and
  companyfacts SHA-256
  `31f9ab4398402faabc733178497af89dbf94dd5038c6e36d4c894317de8a4647`.
- The built wheel was installed into a clean Python 3.12 environment and read
  the same verified live cache successfully.
- Committed schema-preserving recorded fixture subsets with the SEC URLs,
  retrieval instants and full raw-response hashes; the 164 KB/3.75 MB payloads
  remain outside Git.

### Outcome

- Fresh GitHub runner smoke reproduced both local SEC response hashes.
- PR #4 passed CI and the labeled live smoke, then was squash-merged.
- The user authorized the free/no-account portion of Phase 0.4.

### Blueprint deviations

- None.

## 2026-07-18 — Phase 0.2 XNYS calendar contract (merged as `8c83e83`)

### Done

- Added a stdlib-facing, version-pinned XNYS calendar wrapper with exact
  sessions, timezone-aware official opens/closes and half-day detection.
- Added strict previous/next-session and T-1 signal/T fill mappings.
- Added the fixed-anchor {2,4,6,13}-week rotation generator. Each anchor shifts
  independently to the first full-session pair, so a holiday or half-day never
  re-bases the following rotation.
- Kept all weekday/holiday arithmetic inside `usinv/calendar.py`; financial
  modules can consume session objects instead of inventing business days.

### Verification

- `ruff check .` and `ruff format --check .` — passed.
- `pytest -q` — 22 passed, including the T-1-only mapping assertions.
- Pre-commit hooks — passed on all files.
- Built a wheel, installed it and all pinned/runtime dependencies into a clean
  Python 3.12 environment, then repeated the Thanksgiving rotation check and
  `usinv config-check` successfully.
- Golden calendar coverage includes Good Friday, observed Independence Day,
  Juneteenth cutover, 13:00 ET closes, both DST boundaries, MLK,
  Memorial/Labor Day and the Thanksgiving T-1 half-day trap.

### Outcome

- PR #3 passed GitHub Actions, was user-approved and squash-merged.
- Phase 0.3 was explicitly authorized and started from the updated `main`.

### Blueprint deviations

- None.

## 2026-07-18 — Phase 0.1 repository scaffold (merged as `e276041`)

### Done

- Added Python 3.12 packaging, the incremental package tree and a single CLI.
- Added immutable typed configuration for settings, universe, factors,
  portfolio, regime and the exact pre-registered experiment grid.
- Added strict unknown-key and invariant validation plus a stable full-config
  hash.
- Added ruff, pytest, local pre-commit hooks and a small GitHub Actions CI job.
- Added an automatic test guard that fails if tests write into production
  `data/` or `artifacts/` paths.

### Verification

- `ruff check .` — passed.
- `ruff format --check .` — passed.
- `pytest -q` — 8 passed.
- `usinv config-check` — passed with stable config hash
  `eb46acfe591d68e72c0e943e89a6f293d836205c08ff007dc2bd753b34d4ee9f`.
- Built a wheel, verified that all six YAML configuration files are packaged,
  installed it into a clean Python 3.12 environment and repeated
  `usinv config-check` successfully.
- No market-data flow exists in this task, so the lookahead-test requirement is
  not yet applicable; it becomes mandatory as soon as a dated data API exists.

### Outcome

- PR #2 passed GitHub Actions, was user-approved and squash-merged.
- Phase 0.2 was explicitly authorized and started from the updated `main`.

### Blueprint deviations

- None.

## 2026-07-18 — blueprint backbone audit (merged as `15f7763`)

### Done

- Separated research-mode and audit-mode historical evidence.
- Replaced CIK/ticker joins with entity/security/symbol lifecycle contracts.
- Corrected contemporaneous raw vs split-continuous vs total-return consumers.
- Documented the EODHD pre-2018 delisted corporate-action limitation.
- Promoted date-specific Alpha Vantage listing status into the historical
  universe contract.
- Corrected experiment counts and then registered the large-cap extension:
  29 + ≤96 + ≤60 + ≤5 = ≤190, with executable
  neighborhoods, fixed TEST end date and replaced the invalid PIT performance
  heuristic with structural assertions.
- Corrected cash-account funding, exchange-specific listing-risk semantics,
  benchmark-proxy naming and conservative termination handling.
- Replaced Git-as-data-lake assumptions with user-controlled licensed storage.
- Added `BUILD_GUIDE.md` and `SOURCE_REGISTER.md`.
- Fixed the end-product contract: independent installable PWA, unattended
  GitHub Actions production, automatic paper orders and explicitly gated live
  broker mode.
- Added a separately ranked {0,3,5}-slot large-cap extension so >$10B companies
  are eligible without rewriting the core small/mid-cap cross-section.

### Verification

- No model/backtest result existed before the amendment.
- Official provider/regulator/academic links are recorded in
  `SOURCE_REGISTER.md` with check dates.
- `git diff --check` passed; all relative Markdown links resolve.
- Experiment arithmetic independently recomputed: full grid 331,776; planned
  staged maximum 29 + 96 + 60 + 5 = 190 distinct configurations.

### Follow-on sequence

1. Phases 0.1-0.3 are complete.
2. Phase 0.4 selected research mode and measured Alpha Vantage membership.
3. Resolve research-archive retention, storage and full-sample access.

### Current blockers

- The historical price/action archive and licensed storage target are not yet
  selected; no backtest may start before their rights and sample gates pass.
- Alpha Vantage and EDGAR secrets are registered and are not blockers. Broker
  funding remains a later paper/live activation gate.
