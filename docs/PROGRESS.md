# PROGRESS

## 2026-07-18 — Phase 0.1 repository scaffold (PR pending)

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

### Remaining

- Complete Phase 0.1 review/merge, then obtain user approval for Phase 0.2.
- Phase 0.2 implements the XNYS calendar contract; no financial data logic is
  present yet.

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
