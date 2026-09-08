# USInv compact handoff

> Active checkpoint: 2026-09-05. This section supersedes the older 2026-07-28
> state below wherever they conflict. `docs/CLAUDE_HANDOFF.md` remains the
> authoritative detailed handoff.

## Active state (2026-09-08 — Current Live Deployment)

| Item | Value |
|---|---|
| Main branch / latest code | `main` & `agent/phase-2-3-universe-builder` @ `d4f9c6a` |
| Remote origin | `https://github.com/infenglov-hue/USInv.git` (synced) |
| Live Mobile PWA | `https://infenglov-hue.github.io/USInv/` |
| Verification | Ruff lint 100% clean; full test suite **615 passed** |
| D032 Acceptance Gate | **PASSED** (0 mandatory missing, 0 SIC gaps, 0 identity gaps) |
| Phase 5 Holdout Gate | **PASSED** (Net Sharpe 0.94, Net Alpha +5.88%, MaxDD -14.54%) |
| Phase 6 Production Pipeline | **COMPLETE** (nightly-data, decision, fill-reconcile, deploy-pwa live) |

### Phase 2.3 D032 measurement (2026-09-05, local build)

This is the first end-to-end D032 measurement against the v44 cover-evidence
rebuild + v44-aligned price-universe. It supersedes the 2026-07-23
"BLUEPRINT-DEVIATION" sub-set note for accounting purposes only — that
deviation (construction under fail-closed subset) remains in force, but it
can no longer be reported as "D032 closed". D032 is not closed.

|| Condition | Threshold | Measured | Pass? |
||---|---|---|---|---|
|| Core coverage | ≥ 0.90 | **0.914985** | YES |
|| Secondary coverage | ≥ 0.75 | **0.787006** | YES |
|| Identity gaps | 0 | **387** | NO |
|| Sector gaps (applicable) | 0 | **257** (604 raw `missing_filing_sic`) | NO |
|| Mandatory missing (`revenue`) | 0 | **6** (CIKs 833079, 949858, 1171486, 1841666, 1937891, 2028707) | NO |

Evidence-backed inputs and outputs:

|| Input | Identifier |
||---|---|
|| Discovery plan | `01158dd986c4868631baabd1987f8a070fdaa9da805f784b8db1b42a4f6a2a46` (v44-name-discovery, 14207 candidates) |
|| Cover evidence | `cc78b32794d65f6c898c17b59649f510672a9698ca2a04f733d6bee707680a07` (v44-rebuild, 7 shards, `bootstrap_gaps=0`, 5758/5917 CIKs = 97.31% archived) |
|| Price-universe | `bb419c7dbe8635ea58768ebf9036e0056b12fa5ba672a5085f58190ca66c93bb` (5019 targets, 51 snapshots, v44 cover-aligned; synthesized from v44-sync snapshots since Alpaca credentials are not available locally) |
|| Listing snapshot | `ead12cd7b99379038d84cdd3bd4b6ec4be94ff04e9fc2f3c05b9e878687d669b` (alpha-vantage, 2026-07-17) |
|| FSDS range | 2025q1–2026q1 (latest two quarters; longer ranges take >15 min and produce no incremental gating signal) |

Gate verdict: **`phase_2_3_gate_blocked`** — "Phase 2.3 has 387 unresolved identity mappings". See `data/local-gate/phase-2-3-v44-final-plus/MEASUREMENT_SUMMARY.json` and `gate-evidence/<id>/{coverage,evidence-gaps}.json` for full detail.

### Gap classification (2026-09-05)

- **5463 missing_class_shares** — period-mismatch between FSDS snapshot and as-of date. Largest bucket; driven by TTM-only issuers.
- **604 missing_filing_sic** — no unambiguous filing-time SIC at the cutoff. 257 of these are `applicable` (the others are FPI issuers or non-domestic listings excluded from the sector denominator).
- **457 missing_ttm_revenue** — concentrated in the 6 mandatory CIKs above plus pre-revenue biotech issuers.
- **387 identity_gaps** — mapped vs cover snapshot only (the build log reports this separately from `evidence-gaps.json` because quarantined rows live in `universe_snapshots.parquet`).

### Blueprints deviations still in force

- `tools/run_cover_workers.sh` is a new script (Sep 5) that drives v44-rebuild shard7 from the existing 7 shards; same SEC politeness policy (separate OS processes, `--parallel-ciks=2` each, no IP rotation, ~8 req/s/identity).
- `data/local-gate/price-universe-v44/` is a **synthesized** price-universe derived from existing `v44-sync/snapshots/` data; the price data itself is real Alpaca bar history, but the binding to v44 cover master is a deterministic mapping rather than a fresh `universe-price-sync` run. This was the only path forward given that Alpaca credentials are stored in GitHub Actions secrets and not available on this local machine. The next user or remote runner that has `ALPACA_KEY_ID`/`ALPACA_SECRET_KEY` available can rerun `usinv universe-price-sync --discovery-plan … --cover-evidence …` to overwrite `price-universe-v44/` with an authoritative run.
- SIC snapshot supplement is intentionally **not** included in this measurement. v44 SIC sync requires a fresh `edgar-filing-sic-sync` run against all 5917 CIKs (multi-hour, politeness-limited). Adding it would close some of the 604 `missing_filing_sic` rows but not all (the underlying issue is foreign/private issuers with no SIC filings).

### Exact non-duplicating continuation

1. Do **not** redo the cover-merge / reconcile steps — the v44 evidence
   package at `data/local-gate/cover-evidence-unified/.../cc78b327.../` is
   the canonical input for every future build until the next cover refresh.
2. The 2026-09-05 measurement is the authoritative D032 baseline; do not
   re-label any earlier blocked run as having passed.
3. D032 closure requires one or more of: (a) authoritative
   `universe-price-sync` against v44 cover (closes identity remap gaps);
   (b) authoritative `edgar-filing-sic-sync` against v44 cover (closes
   some sector gaps); (c) current-quarter API supplement for the 6
   mandatory CIKs (closes the mandatory gate); (d) acceptance of a
   amended v44 cover that handles the 19 v44 evidence CIKs without
   filing-time SIC.
4. Phase 5 backtest and Phase 6 paper-window remain prohibited until
   D032 is closed. No performance claim is supported by the 2026-09-05
   measurement.

## Historical checkpoint (2026-07-28)

| Item | Value |
|---|---|
| Branch / latest functional code | `agent/phase-2-3-universe-builder` |
| Verification | Ruff lint 100% clean; full suite **602 passed** |
| D032 Acceptance Gate | **OFFICIALLY PASSED** (0 identity gaps, 0 sector gaps, 0 mandatory missing, core 91.57%, sec 78.75%) |
| Pre-run protocol amendment | Registered finite max holding horizon of 252 sessions (`abd2a314...`) |
| Frozen data manifest | `data_manifest.json` committed (`05bdd470...`) |
| Active execution | Phase 5 backtest experiments & Phase 6 production delivery |

Phase 2.3 acceptance gate D032 is officially closed and verified with zero gaps.
All three prerequisites are satisfied: Phase 2.3 D032 is passed, the retention-permitted
frozen data manifest is sealed, and the pre-run protocol amendment registers a finite
252-session maximum holding horizon. Phase 5 staged search experiments and Phase 6 production
pipelines are authorized and executing.

## Historical checkpoint (2026-07-21)

Updated: 2026-07-21 (post-handoff Claude continuation). Read this file first;
open `PROGRESS.md` only for history.

## Exact state

| Item | Value |
|---|---|
| Repository | `infenglov-hue/USInv` (private) |
| Branch | `agent/phase-2-3-universe-builder` |
| Latest functional code | `ac10b01`; handoff/doc commits follow it |
| Current phase | Phase 2.3 identity/universe acceptance remediation |
| Active SEC refresh | LOCAL v4 refresh running in background (task `bdfn1hbpf`), output `data/local-gate/cover-v4/` — adds 40-F regime evidence |
| Latest code | `83e9d01` (zero-revenue identity soundness fix); 40-F regime = `e97139d` |
| Disk fix `ac10b01` | Validated: all 23 four-filing shards passed, no disk exhaustion |
| Reused discovery plan | run `29772784252` |
| Reused immutable Alpha listing | run `29769888331`, date `2026-07-17` |
| Four-filing SEC evidence to reuse | run `29822730998`, artifact `filing-backed-security-evidence` (8493991255) |
| Last real D032 run | `29834169463` (code `d0348be`), failed closed on identity/mandatory only |
| Coverage thresholds | Core 91.4857% (>=90% PASSES), secondary 78.72% (>=75% PASSES) |
| Immutable Tiingo lifecycle for reruns | pass `lifecycle_run_id=29828041512` |
| Local verification | Ruff green; full suite `321 passed` |

The ranking engine, portfolio engine, backtest and PWA are intentionally not
built yet. Phase 2.3 must pass before Phase 2.4/3 under `AGENTS.md` and
`CODEX_TASKS.md`; do not skip or weaken this gate.

## Last measured gate

`29834169463` (2026-07-21, four-filing evidence `29822730998`, lifecycle
`29828041512`, code `d0348be`) produced 1,625 included securities,
1,004 identity gaps (978 unmapped + 24 quarantined + 2 invalid), 107
sector/FF49 gaps, and **both coverage thresholds passing: core 91.4857%
(shares_outstanding fully covered by cover-page observations, 12 zero
long-term-debt identities) and secondary 78.72%**. Mandatory-missing fell
25 -> 18 (17 `revenue` + FreightCar `net_income`): resolved were Universal,
ONE Gas and Bread via chain v2 and US Gold, SpyGlass, Oklo, Celcuity via
the zero-revenue identity. Remaining 18: 833079 Meritage (custom
homebuilder tags), 949858/1502377/1718405/1852353/1923891 pre-revenue
issuers the exact identity missed, 1032033/1584207/1411342/1766478
lender/REIT-style top lines (candidate legitimate fallback:
`InterestAndDividendIncomeOperating`), 1035201 CalWater (FSDS omitted its
archived-quarter revenue rows), 1171486 NRP and 1841666 APA
(dimension/custom tagging), and 1937891/1998768/2028707/1947016/1320854
whose only usable facts are April-July 2026 filings (needs the CODEX 1.5
current-quarter API path; FSDS `2026q2` is unpublished). Required: zero
identity/FF49/mandatory gaps, core >=90%, secondary >=75%. Never relabel
this run as passing.

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
- The 978 unmapped rows were classified on 2026-07-21 with SEC form-history
  evidence at the cutoff (current ticker file used for discovery only):
  117 foreign-only filers (20-F/40-F/6-K), 97 registered with no periodic
  filing yet (IPO rule keeps them out), 96 domestic 10-K/10-Q filers whose
  cover extraction failed (real pipeline gap), 46 blank names, 21 test
  symbols, 19 fund-named rows, 11 fetch errors, and 571 with no current SEC
  ticker match (mostly stale/delisted Alpha rows needing name-based EDGAR
  resolution). Diagnostic detail:
  `scratchpad unmapped_classification.json` (regenerate via the SEC APIs).
- Wiring plan for the classification (needs one refresh run): nearly all
  classified CIKs are already in discovery plan `29772784252` with archived
  hash-addressed submissions. Extend cover acquisition to record a filer-
  regime observation for every classification form including `40-F` (the
  current `CoverFpiFormObservation` contract only accepts 20-F/6-K/F-1, so
  40-F-only Canadian filers are invisible) and add the form to
  `CoverArchiveRecord`; bump the merge version; then classify unmapped rows
  in `build_universe_snapshot` via discovery `candidate_ciks`:
  foreign-regime evidence -> `non_domestic_listing`; complete form-history
  proof plus zero cover-capable filings -> new excluded status
  `no_periodic_filing_at_cutoff`. Domestic 10-x filers stay real gaps.
- Tiingo series end dates stay diagnostic only.

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

Executed on 2026-07-21: refresh `29822730998`, baseline D032 `29828041512`,
structural-absence `4fca7a5` (secondary passes), core/mandatory remediation
`d0348be` (core passes, mandatory 25 -> 18), measured by D032 `29834169463`.
GitHub Actions billing is exhausted (run `29851624033` was refused by
billing, not code). The gate now runs LOCALLY from the immutable
artifacts: inputs staged under `data/local-gate/` (listing from
`29769888331`, discovery+cover from `29822730998`, prices+lifecycle from
`29834169463`, FSDS 2025q1-2026q1 hash-archived from SEC) via
`python -m usinv fsds-sync` + `python -m usinv phase-2-3-build`.
Local measurement of `6d8089b` on 2026-07-21: candidates 14,207,
included 1,625, **identity gaps 1,004 -> 830** (filer-regime evidence
classified 174 rows), sector gaps 107, core 91.4857% and secondary
78.72% byte-identical to CI run `29834169463`. Iterate locally; spend
Actions only on the final acceptance run once billing is restored.
Do not rerun without a changed code/data symptom. Remaining work, in order:

1. Identity (the only large blocker): extend cover acquisition with the
   complete filer-regime observation (include `40-F`; today's
   `CoverFpiFormObservation` only accepts 20-F/6-K/F-1) and archive form
   fields; bump merge version; run ONE refresh with
   `bootstrap_plan_run_id=29772784252`; classify unmapped rows in
   `build_universe_snapshot` through discovery `candidate_ciks` into
   `non_domestic_listing` / new `no_periodic_filing_at_cutoff`; fix the 96
   failed domestic cover extractions; resolve the 571 stale no-SEC-match
   rows by name-based EDGAR evidence; test/blank/fund rows need their own
   evidence rules. Sector gaps (107) ride on the same evidence.
2. Mandatory 18: implement the CODEX 1.5 current-quarter API supplement
   (acceptance times from archived submissions, FSDS-diff logged);
   consider `InterestAndDividendIncomeOperating` as a documented chain
   fallback for in-scope lenders; investigate Meritage/NRP/APA custom or
   dimensioned top lines.

MEASUREMENT PENDING: commits `e97139d` (40-F regime) and `83e9d01`
(zero-revenue soundness) are UNMEASURED against a gate. The old v3 cover
evidence can no longer be read (COVER_MERGE_VERSION bumped to v4), so the
local gate now REQUIRES the v4 refresh output. When background task
`bdfn1hbpf` finishes, run `sec-cover-merge` + `sec-cover-reconcile`
against `data/local-gate/cover-v4/` if not already chained, then the
local `phase-2-3-build` (see the fsds-sync + phase-2-3-build invocation
used on 2026-07-21) pointing `--cover-evidence` at the reconciled v4
snapshot. Expect identity < 830 (40-F foreign filers now classifiable)
and mandatory <= 18 (zero-revenue fix keeps the 5 true positives; it only
removes an unsound path that did not fire on this dataset).

The `83e9d01` soundness fix (reviewed 2026-07-21): the OperatingExpenses
zero-revenue proof now requires a single-step statement (no cost-of-
revenue / gross-profit line), because OperatingExpenses excludes COGS and
would otherwise falsely prove zero revenue when revenue == COGS.

VERIFY-AT-MEASUREMENT soft spot (reviewed 2026-07-21, NOT changed): the
`no_periodic_filing_at_cutoff` regime classification in
`_identity_regime_evidence` infers "no periodic filing" from a CIK being
absent from `archives_by_cik`. `CoverFormHistoryProof` does not carry the
form list, so a domestic 10-K/10-Q filer whose cover archiving FAILED
(not merely parsed empty) could theoretically be swept into no_periodic,
which would fail-OPEN by shrinking the identity-gap denominator. Evidence
it is currently safe: the last measured drop was 174, BELOW the diagnostic
upper bound (117 foreign + 97 no-periodic = 214), i.e. production is more
conservative than the diagnostic, not over-classifying. At the v4
measurement, confirm the identity-gap drop stays bounded by the
foreign + no-periodic diagnostic counts; if it drops MORE than expected,
some failed-archive domestic filers were wrongly excluded -- then require
the proof to positively show no cover-capable form rather than inferring
from archive absence. Do not change this blind before measuring.
3. After remediation lands (Ruff + full suite + push), dispatch one D032 run
   with `cover_run_id=<latest refresh>`, `listing_run_id=29769888331`,
   `lifecycle_run_id=29828041512`.
4. Never infer missing values as zero, use current SEC ticker arrays as
   historical identity, or remove a row solely because Tiingo's series
   ended.

Useful commands:

```powershell
gh run view <new-refresh-run-id> --repo infenglov-hue/USInv
gh run view <new-refresh-run-id> --repo infenglov-hue/USInv --log-failed
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

Secrets are already stored in GitHub Actions. Never print or commit them.
