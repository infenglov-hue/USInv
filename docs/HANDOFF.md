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
| Last real D032 run | `29830795288` (post-`4fca7a5` structural absence), failed closed |
| Immutable Tiingo lifecycle for reruns | pass `lifecycle_run_id=29828041512` |
| Local verification | Ruff green; full suite `321 passed` |

The ranking engine, portfolio engine, backtest and PWA are intentionally not
built yet. Phase 2.3 must pass before Phase 2.4/3 under `AGENTS.md` and
`CODEX_TASKS.md`; do not skip or weaken this gate.

## Last measured gate

`29830795288` (2026-07-21, four-filing evidence `29822730998`, lifecycle
`29828041512`, code `4fca7a5`) produced 1,625 included securities,
1,004 identity gaps (978 unmapped + 24 quarantined + 2 invalid), 107
sector/FF49 gaps, 25 mandatory-missing cells (24 `revenue`, 1 `net_income`),
89.8989% core coverage (23 cells short of 90%) and **78.72% secondary
coverage — the 75% secondary threshold now passes** via `4fca7a5`
structural-absence proofs (932 accounting-identity zeros + 11 derived
minority-interest values). Required: zero identity/FF49/mandatory gaps,
core >=90%, secondary >=75%. Never relabel this run as passing.

Comparison with the previous real gate `29810991030` (two-filing evidence):
included 1,461 -> 1,625; identity gaps 1,658 -> 1,004; quarantined 279 -> 24;
core/secondary rates roughly flat because newly included securities bring
their own uncovered cells.

Gap diagnosis on the retained `29830795288` artifact and 2026-07-21 root
causes (verified against SEC companyfacts and raw FSDS 2026q1):

- Largest uncovered core concepts: `current_debt_and_borrowings` (577),
  `gross_profit` (479), `long_term_debt` (432), `shares_outstanding` (344).
  Only 23 covered core cells are needed; cover-page share observations
  (9,826 already in the security evidence) can cover `shares_outstanding`,
  and `Liabilities == LiabilitiesCurrent` proves zero long-term debt.
- Mandatory-missing root causes: (a) several issuers' newest facts sit in
  filings accepted April-July 2026, but FSDS `2026q2` is not yet published
  (404 as of 2026-07-21) — CODEX_TASKS 1.5 already mandates the API/live
  path for the current quarter instead of waiting for the drop; (b) FSDS
  omits some face-statement facts that companyfacts has (e.g. FreightCar
  `1320854` FY2025 10-K has zero NetIncome/ProfitLoss rows in num.txt);
  (c) Universal `102037` tops its income statement with
  `RevenuesExcludingInterestAndDividends` and ONE Gas with
  `RegulatedOperatingRevenue` — legitimate chain-fallback extensions per
  MODEL_SPEC; (d) SIC 6141 lenders and 6500 REIT-like issuers are in-scope
  per MODEL_SPEC's exact exclusion lists (6020-6036/6199/6211/6311-6399,
  REIT=6798 only) — do not widen exclusions to dodge gaps; (e) genuinely
  pre-revenue non-biotech issuers (e.g. exploration miners, Oklo) need an
  explicit structural-zero-revenue identity
  (`OperatingIncomeLoss == -OperatingExpenses` style) or stay blocking.
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

Executed on 2026-07-21: refresh `29822730998` (success), D032 `29828041512`
(fail-closed baseline), structural-absence commit `4fca7a5`, and measurement
D032 `29830795288` (secondary now passes at 78.72%). Do not rerun without a
changed code/data symptom. Remaining remediation order:

1. Core +23 cells: wire cover-page share observations into
   `shares_outstanding` applicability evidence; add the
   `Liabilities == LiabilitiesCurrent` zero-long-term-debt identity.
2. Mandatory 25: current-quarter API/live-edge fact supplement (CODEX_TASKS
   1.5 sanctions it), chain-fallback extensions
   (`RevenuesExcludingInterestAndDividends`, `RegulatedOperatingRevenue`,
   versioned chain bump), structural zero-revenue identity for pre-revenue
   issuers.
3. Classify the 978 unmapped identities per category with explicit evidence
   (as `f08232a` did for the 261 quarantined rows); no suffix guessing.
   Resolve the 107 FF49/sector gaps (`missing_filing_sic`).
4. After remediation lands (Ruff + full suite + push), dispatch one D032 run
   with `cover_run_id=29822730998`, `listing_run_id=29769888331`,
   `lifecycle_run_id=29828041512`.
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
