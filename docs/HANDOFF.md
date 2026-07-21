# USInv compact handoff

> Codex work is stopped. For Claude continuation, use
> `docs/CLAUDE_HANDOFF.md` as the authoritative detailed handoff.

Updated: 2026-07-21 (post-handoff Claude continuation). Read this file first;
open `PROGRESS.md` only for history.

## Exact state

| Item | Value |
|---|---|
| Repository | `infenglov-hue/USInv` (private) |
| Branch | `agent/phase-2-3-universe-builder` |
| Latest functional code | `ac10b01`; handoff/doc commits follow it |
| Current phase | Phase 2.3 identity/universe acceptance remediation |
| Active SEC refresh | None; last refresh `29822730998` completed successfully |
| Disk fix `ac10b01` | Validated: all 23 four-filing shards passed, no disk exhaustion |
| Reused discovery plan | run `29772784252` |
| Reused immutable Alpha listing | run `29769888331`, date `2026-07-17` |
| Four-filing SEC evidence to reuse | run `29822730998`, artifact `filing-backed-security-evidence` (8493991255) |
| Last real D032 run | `29828041512`, artifact `phase-2-3-universe-evidence` (8494461411), failed closed |
| Immutable Tiingo lifecycle for reruns | pass `lifecycle_run_id=29828041512` |
| Local verification | Ruff green; full suite `321 passed` |

The ranking engine, portfolio engine, backtest and PWA are intentionally not
built yet. Phase 2.3 must pass before Phase 2.4/3 under `AGENTS.md` and
`CODEX_TASKS.md`; do not skip or weaken this gate.

## Last measured gate

`29828041512` (2026-07-21, using four-filing evidence `29822730998`) produced
14,207 listing candidates, 1,625 included securities, 1,004 identity gaps
(978 unmapped + 24 quarantined + 2 invalid symbols), 107 sector/FF49 gaps,
25 mandatory-missing cells (24 `revenue`, 1 `net_income`), 4,807 evidence
gaps, 89.8989% core coverage (23 cells short of 90%) and 67.1138% secondary
coverage (~641 cells short of 75%). Required: zero identity/FF49/mandatory
gaps, core >=90%, secondary >=75%. Never relabel this run as passing.

Comparison with the previous real gate `29810991030` (two-filing evidence):
included 1,461 -> 1,625; identity gaps 1,658 -> 1,004; quarantined 279 -> 24;
core/secondary rates roughly flat because newly included securities bring
their own uncovered cells.

Gap diagnosis on the retained `29828041512` artifact:

- Secondary shortfall is dominated by `minority_interest` (1,064 uncovered),
  `preferred_equity` (847) and `interest_expense` (551) — concepts that are
  typically absent because they are genuinely zero. Under
  `missing_is_uncovered_not_zero` they only become covered through
  evidence-based `derived_identity` structural-absence proofs (1,201 cells
  already use that proof kind). Extending those proofs is the main lever.
- Largest uncovered core concepts: `current_debt_and_borrowings` (577),
  `gross_profit` (479), `long_term_debt` (432), `shares_outstanding` (344).
- Evidence-gap kinds: 3,933 `missing_class_shares`, 447 `missing_filing_sic`,
  427 `missing_ttm_revenue`.
- The 978 unmapped identities remain the evidence-by-category tail (ETFs,
  stale Alpha rows, foreign issuers, new IPOs, test symbols, issuers without
  a matching cover fact). Tiingo series end dates stay diagnostic only.

## What `f08232a` changes

- Safely classifies filing-proven all-foreign/all-non-common collisions while
  preserving mixed collisions.
- Parses SEC inline XBRL containing fixed named HTML entities such as `&nbsp;`
  without permitting DTDs/external entities.
- Falls back from a plain primary filing document to archived XML instance
  documents.
- Selects four PIT-bounded cover-capable filings per CIK instead of two.
- Archives a hash-addressed Tiingo supported-ticker snapshot as diagnostic
  evidence and supports immutable artifact reuse.
- Keeps every D032 threshold, PIT rule and mandatory-input rule unchanged.

## Next action — do not start duplicate runs

The handoff continuation steps 1-7 of `docs/CLAUDE_HANDOFF.md` §8 were
executed on 2026-07-21: refresh `29822730998` succeeded and D032
`29828041512` failed closed with the metrics above. Do not rerun either
workflow without a changed code/data symptom. Remediation order:

1. Extend `derived_identity` structural-absence proofs for
   `minority_interest`, `preferred_equity` and `interest_expense`
   (largest lever; the 23-cell core shortfall likely closes with it).
2. Resolve the 25 mandatory-missing cells per CIK with filing evidence.
3. Classify the 978 unmapped identities per category with explicit evidence
   (as `f08232a` did for the 261 quarantined rows); no suffix guessing.
4. After remediation lands (Ruff + full suite + push), dispatch one D032 run
   with `cover_run_id=<latest valid evidence run>`,
   `listing_run_id=29769888331`, `lifecycle_run_id=29828041512`.
5. Never infer missing values as zero, use current SEC ticker arrays as
   historical identity, or remove a row solely because Tiingo's series ended.

Useful commands:

```powershell
gh run view <new-refresh-run-id> --repo infenglov-hue/USInv
gh run view <new-refresh-run-id> --repo infenglov-hue/USInv --log-failed
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

Secrets are already stored in GitHub Actions. Never print or commit them.
