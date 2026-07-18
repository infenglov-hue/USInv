# PROGRESS

## 2026-07-18 — blueprint backbone audit (working tree, not yet committed)

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

### Remaining before code

1. Publish and review the documentation repair in the new independent repo.
2. Start CODEX_TASKS 0.1-0.3.
3. Run the user-gated Phase 0.4 feasibility spike.
4. Select historical evidence mode, storage target and provider/account inputs.

### Current blockers

- Historical mode cannot be selected honestly until the ≥30-security provider
  coverage matrix is produced.
- No provider credentials, EDGAR contact address, broker funding model or
  licensed-data storage target have been registered yet.
