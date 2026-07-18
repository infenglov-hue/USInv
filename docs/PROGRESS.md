# PROGRESS

## 2026-07-18 — Phase 0.3 polite EDGAR client (live gate pending)

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

### Required to close Phase 0.3

1. Run the two-request smoke test from a fresh GitHub runner through the
   explicitly labeled PR workflow.
2. Complete Phase 0.3 review/merge, then obtain user approval for the user-gated
   Phase 0.4 historical-data feasibility spike.

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

1. Complete and review CODEX_TASKS 0.1-0.3 one PR at a time.
2. Run the user-gated Phase 0.4 feasibility spike.
3. Select historical evidence mode, storage target and provider/account inputs.

### Current blockers

- Historical mode cannot be selected honestly until the ≥30-security provider
  coverage matrix is produced.
- No provider credentials, EDGAR contact address, broker funding model or
  licensed-data storage target have been registered yet.
