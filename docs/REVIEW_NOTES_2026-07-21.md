# Review notes — Phase 2.3 evidence work, 2026-07-21

Self-contained review packet for an external reviewer. It covers every code
change made on 2026-07-21 on branch `agent/phase-2-3-universe-builder`
(commits `d310f7c`..`083991e`). Goal of the session: move the D032 universe
acceptance gate toward passing **without weakening any threshold or converting
missing data to zero**. Governing rules: `AGENTS.md` (esp. point-in-time /
as-first-filed / "missing is never zero" / no threshold loosening).

## D032 gate, in one paragraph

The gate builds a date-valid US common-stock universe as of 2026-07-17 and
requires: (a) zero identity-mapping gaps, (b) zero FF49/sector gaps, (c) zero
missing mandatory scoring inputs (`revenue`, `net_income`), (d) core concept
coverage >= 90%, (e) secondary coverage >= 75%. A concept cell is "covered"
when a filed fact is present OR an explicit accounting identity proves a
structural zero. Missing alone must stay uncovered.

## Measured trajectory (all real runs, thresholds never changed)

| Metric | Session start | Latest measured |
|---|---:|---:|
| Core coverage | 89.90% (fail) | 91.49% (**pass**) |
| Secondary coverage | 67.17% (fail) | 78.72% (**pass**) |
| Mandatory-missing cells | 25 | 18 |
| Identity gaps | 1,658 | 830 |
| Sector/FF49 gaps | 97 | 107 |

Latest fully-measured CI run: `29834169463` (code `d0348be`). Two later code
commits (`e97139d` 40-F regime, `83e9d01` zero-revenue soundness) are
**UNMEASURED** — the local gate now requires the v4 cover refresh (running in
background) because `COVER_MERGE_VERSION` was bumped v3->v4.

## Code commits to review (highest scrutiny first)

### 1. `83e9d01` — zero-revenue identity soundness fix (self-caught during review)

- File: `usinv/data/edgar/applicability.py`, `derive_structural_absence_evidence`
  (the `revenue_zero` loop) and `usinv/data/edgar/tag_chains.py`
  `STRUCTURAL_IDENTITY_TAGS`.
- Claim proved: `revenue == 0` when `OperatingIncomeLoss + expenses == 0`.
- **Bug that was fixed:** sound for `CostsAndExpenses` (a total that includes
  COGS) but NOT for `OperatingExpenses`, which per US-GAAP taxonomy EXCLUDES
  cost of revenue. On a two-step statement `OperatingIncomeLoss = GrossProfit -
  OperatingExpenses`, so `OpInc == -OperatingExpenses` only forces
  `GrossProfit == 0` (i.e. `revenue == COGS`), NOT `revenue == 0`. A filer with
  revenue exactly equal to COGS would be falsely proven zero-revenue — a
  fail-open conversion of a nonzero mandatory input to zero.
- **Fix:** accept `CostsAndExpenses` always; accept `OperatingExpenses` only on
  a single-step statement (no cost-of-revenue tag and no `GrossProfit` tag in
  the same period).
- **Why it does not regress:** empirically (raw FSDS 2026q1) the five filers the
  rule currently resolves (US Gold 27093, SpyGlass 1778922, Oklo 1849056,
  Celcuity 1603454, Nano Nuclear 1923891) report NEITHER COGS nor GrossProfit,
  so they stay covered. Contango 1502377 uses `CostsAndExpenses` with positive
  operating income, so it never fired (stays a real gap). The fix changes ZERO
  current numbers; it hardens against data not currently in the universe.
- **Failure direction:** conservative-only. The guard can only REJECT a proof
  (leave a cell uncovered), never assert a false zero. Reviewer should confirm
  this direction is acceptable (it is fail-closed).
- Tests: `tests/test_structural_absence.py` —
  `test_operating_expenses_zero_revenue_rejected_when_cogs_is_reported`,
  `..._when_gross_profit_is_reported`,
  `test_total_costs_and_expenses_prove_zero_revenue_even_with_cogs`.

### 2. `6d8089b` + `e97139d` — filer-regime classification of unmapped listings

- Files: `usinv/phase_2_3.py` `_identity_regime_evidence`; `usinv/universe.py`
  `IdentityRegimeEvidence`, regime branch in `build_universe_snapshot`,
  `_EVIDENCED_NON_MEMBER_STATUSES`, `FPI_FORMS`; `cover_acquisition.py`
  `_FPI_CLASSIFICATION_FORMS` (+`40-F`); `cover_shards.py` version bump v4.
- Purpose: an unmapped listing row whose discovery `candidate_ciks` carry
  archived SEC evidence leaves the common-stock identity denominator only with
  explicit proof: (a) `non_domestic_listing` when EVERY archived cover filing
  for the CIK is a foreign-form accession (20-F/40-F/6-K); (b) new
  `no_periodic_filing_at_cutoff` when a complete form-history proof exists but
  no cover filing was archived and no foreign form seen (IPO/just-registered).
  Mixed or unevidenced candidates stay real `unmapped` gaps.
- **KNOWN SOFT SPOT (flagged, deliberately NOT fixed — see below).**
- Tests: `tests/test_universe.py` —
  `test_filer_regime_evidence_classifies_unmapped_rows`,
  `test_regime_evidence_never_reclassifies_mixed_or_unlisted_candidates`.

#### SOFT SPOT to scrutinize (potential fail-open)

`no_periodic_filing_at_cutoff` infers "no periodic filing" from the CIK being
absent from `archives_by_cik`. `CoverFormHistoryProof` (cover_acquisition.py)
does NOT carry the form list, so a domestic 10-K/10-Q filer whose cover
archiving FAILED (not merely parsed-empty) could theoretically be swept into
`no_periodic`, shrinking the identity-gap denominator (fail-OPEN on gate (a)).

- Reviewer question: does `acquire_cover_evidence` emit a
  `CoverFormHistoryProof` for a CIK even when its cover archiving failed? If
  yes, the misclassification is reachable.
- Evidence it is currently safe: the last measured identity-gap drop was 174
  (1,004 -> 830), BELOW the independent diagnostic upper bound of 117 foreign +
  97 no-periodic = 214 (from `scratchpad/unmapped_classification.json`, built
  from the SEC submissions API). Production classified FEWER rows than the
  diagnostic, i.e. it is more conservative, not over-classifying.
- Verification criterion at the v4 measurement: the identity-gap drop must stay
  bounded by the foreign+no-periodic diagnostic counts. If it drops MORE than
  expected, failed-archive domestic filers were wrongly excluded; the fix is to
  require the proof to positively show no cover-capable form rather than
  inferring from archive absence.

### 3. `4fca7a5` + `d0348be` — structural-absence & cover-share evidence

- File: `usinv/data/edgar/applicability.py`
  (`derive_structural_absence_evidence`, `cover_share_evidence`).
- Identities added (each exact-Decimal, PIT-bounded to `accepted <= cutoff`,
  suppressed if an observed fact for the same (cik, concept) exists):
  - minority_interest zero: `Liabilities + StockholdersEquity(parent) ==
    LiabilitiesAndStockholdersEquity` (total credit side). Because the total
    credit side includes temporary/redeemable NCI, this proves BOTH permanent
    and mezzanine NCI are zero — conservative and sound.
  - minority_interest observed: `StockholdersEquityIncludingNCI - parent` when
    total > parent (positive derived value; outranks the zero proof).
  - long_term_debt zero: `Liabilities == LiabilitiesCurrent` => noncurrent
    liabilities are zero => long-term debt (a noncurrent item) is zero. Sound.
  - preferred_equity zero: every `PreferredStockShares{Issued,Outstanding}`
    fact for the CIK is zero => no preferred stock. Sound.
- cover_share_evidence: filing cover-page `dei` share counts become direct
  `shares_outstanding` facts (availability = filing acceptance time). This
  closed the entire 344-cell `shares_outstanding` core gap.
- Leakage guard: facts accepted after cutoff can neither create nor suppress
  evidence; tests
  `test_future_facts_cannot_create_or_suppress_structural_evidence`.
- Reviewer check: confirm the PIT filter and the observed-fact suppression are
  correct, and that emitting multiple same-classification evidence rows per
  (cik, concept) is harmless (the coverage builder rejects only MIXED
  classifications and otherwise picks the earliest-available).

### 4. `d0348be` — revenue chain v2 (`CHAIN_VERSION` -> `usinv-sec-concepts-v2`)

- File: `usinv/data/edgar/tag_chains.py`, `CONCEPT_CHAINS` revenue chain.
- Appended as LOWEST-priority fallbacks: `RevenuesExcludingInterestAndDividends`
  (trading/investment top line), `RevenuesNetOfInterestExpense` (bank net
  revenue), `RegulatedOperatingRevenue` (utilities). Only used when no standard
  revenue tag exists. Reviewer check: is `RevenuesNetOfInterestExpense`
  acceptable as a revenue proxy for in-scope lenders? (It is net of interest
  expense — borderline; it is last-resort priority.)

## What did NOT change (guardrails held)

- No D032 threshold was altered (`core_threshold=0.90`, `secondary=0.75`).
- No row was excluded without explicit archived evidence.
- No missing value was converted to zero except via an exact filed accounting
  identity.
- Tiingo lifecycle end dates remain diagnostic only.

## How to reproduce the measurement locally (no GitHub Actions)

Inputs are immutable artifacts under `data/local-gate/` (listing run
`29769888331`, discovery+cover `29822730998`, prices+lifecycle `29834169463`)
plus hash-archived public FSDS quarters. Run `python -m usinv fsds-sync` then
`python -m usinv phase-2-3-build` (exact invocation and paths in
`docs/HANDOFF.md`). The current code requires the v4 cover refresh output
(`data/local-gate/cover-v4/`, background task, ~10-15h) before the gate can be
re-measured, because the cover merge version was bumped to v4.

## Test status

`ruff` clean; `pytest -q` = 343 passed (one unrelated session-teardown
artifact-guard trip caused by the concurrent local refresh writing under
`data/`, not a code failure).
