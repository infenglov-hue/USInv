# USInv — complete Claude handoff

Updated: 2026-07-23 after local Phase 2.3 identity remediation and a blocked
GitHub Actions refresh attempt.

This is the authoritative continuation document. It is intentionally detailed
enough that Claude should not need the Codex conversation transcript.

## 0AA. Live Codex checkpoint — 2026-07-28, commit `ccdb9e2`

This checkpoint supersedes every older continuation instruction below where it
conflicts.

### Exact repository state

| Item | Current state |
|---|---|
| Branch / latest functional code | `agent/phase-2-3-universe-builder` / `ccdb9e2` |
| Remote relation | All current Phase 2.4/3 commits are local; no push was performed |
| Working tree | Clean immediately after functional commit; this handoff edit follows |
| Verification | `ruff check .` passed; full suite **531 passed** |
| Active external workflow/process | None |
| D032 | Still not passed; the documented deviation permits construction only |

Local construction has advanced beyond the 2026-07-23 checkpoint: Phase 2.4
freshness and all four Phase 3 tasks are now implemented:

- Phase 3.1: fail-closed hygiene gates and evidence pointers.
- Phase 3.2: the shared point-in-time scoring context.
- `fd014d0` supplies value/quality/momentum metrics and cross-sectional sleeve
  ranks.
- `af084e7` supplies the complete nine-signal Piotroski veto, weighted sleeve
  composite, core/large-cap independent ranking, optional FF12-relative
  ranking and final within-bucket percentile.
- Piotroski evidence that is missing is quarantined; F-score <=4 is vetoed.
  Both are removed before peer percentiles are fitted. A missing sleeve is not
  replaced or reweighted, and a missing FF12 group fails closed when
  sector-relative mode is enabled.
- `c331d95` supplies canonical macro vintages, immutable content-addressed
  archive records, strict FRED/ALFRED/Cboe parsers, HY-OAS and HYG/LQD credit
  signals, O0-O3 exposure overlays, slow NFCI/Sahm directives and explicit
  regime-conditional weights.
- Phase 3.4 acceptance covers 2020-03 risk-off, 2022 bear and 2023 chop plus a
  +7-day availability perturbation and future NFCI revision regression.
- `BLUEPRINT-DEVIATION`: FRED now exposes only roughly three years of HY-OAS
  history and no earlier full archive exists locally. Older history must use a
  frozen, explicitly thresholded HYG/LQD total-return fallback.
- `ccdb9e2` completes Phase 4.1: deterministic stateful selection, buy/hold
  bands, large-cap/sector/correlation constraints, position and split
  continuity, settled-cash equal-weight sizing, fixed XNYS rotations and the
  rolling 21-session turnover hard cap. Forced exits may breach the cap and
  then block replacement buys, as required.

### Exact non-duplicating continuation

1. Do not redo Phase 3 or the v1-v20 identity/cover work.
2. If continuing product construction under the approved deviation, the next
   PR-sized slice is Phase 4.2 only: trailing-stop and thesis-break exits per
   CODEX_TASKS 4.2 / MODEL_SPEC section 6.
3. The HYG/LQD fallback threshold and defensive factor vector must remain
   explicit inputs until registered; do not create hidden defaults.
4. D032 remains mandatory before any performance claim, experiment, paper
   operation or live activation. Do not describe it as passed.

## 0A. Live Codex checkpoint — 2026-07-23, commit `fff1529`

This checkpoint supersedes the older `4337072` / 178-gap continuation details
below wherever they conflict. The user has only about 8% of the weekly Codex
quota left and explicitly requested incremental handoff notes rather than one
final note. Update this section after every meaningful implementation or
measurement slice.

### Exact repository state

| Item | Current state |
|---|---|
| Branch | `agent/phase-2-3-universe-builder` |
| HEAD | `fff1529` (`Corroborate association-only CIK candidates`) |
| Remote | local branch is 3 commits ahead; the three local commits are `33c45ad`, `4ea3dd1`, `fff1529` |
| Working tree after `fff1529` | Clean before this handoff-note edit |
| Verification | `ruff check .` passed; full suite passed, **432 tests collected** |
| Active external workflow/process | None |

Phase 2.3 still has not passed D032. A documented, user-approved
`BLUEPRINT-DEVIATION` in `docs/PROGRESS.md` allowed Phase 2.4 and later engine
construction to proceed, but zero D032 gaps remains mandatory before any
performance claim, paper operation or live activation. Phase 2.4 freshness
kill-switch code already exists in local commit `4ea3dd1`.

### Why proceeding is acceptable, and where it is not

The user and Codex explicitly reviewed the residual and concluded that it is
not a major blocker for constructing the later engine:

- Unresolved identity, sector or mandatory-evidence rows fail closed. They are
  excluded from the investable universe; the pipeline never guesses an
  identity or silently fills missing values.
- The last fully supplemented local build had 1,646 included securities. The
  unresolved identity set was 178 of 14,207 listing candidates (about 1.25%);
  most residuals were stale/delisted Alpha rows, foreign issuers, SPACs,
  non-common products or issuers that would fail other filters anyway.
- Therefore Phase 3 hygiene/scoring contracts can be implemented and tested on
  the already evidence-complete included universe. The practical effect during
  construction is that some unresolved stocks simply cannot enter the
  universe; it does not corrupt the identities or facts of stocks that do
  enter.
- The genuine watch item is the small domestic 10-K/10-Q cover-extraction
  bucket. Those companies might eventually become eligible, so excluding them
  could bias a measured backtest. This is why the deviation permits software
  construction but does **not** permit performance claims, model selection,
  paper operation or live activation before the residual and D032 gate are
  closed.

Do not misstate the decision as “D032 no longer matters” or “Phase 2.3
passed.” The precise decision is: later components may be built against the
fail-closed evidence-complete subset, while final empirical/operational use
remains gated on full D032 acceptance.

### What `fff1529` did

- Added exact PIT FSDS issuer-name corroboration for unique CIK candidates that
  previously came only from the current SEC ticker association.
- The corroboration never changes the candidate CIK: the exact normalized name
  must resolve uniquely to the same CIK. Mismatched or ambiguous names add no
  provenance.
- `sec-name-discovery` applies corroboration before searching still-unmapped
  rows.
- Price-universe snapshots may reuse immutable batches across a discovery
  metadata change only when cover master, time bounds, batch size and the
  complete ordered target set are unchanged.
- Added regression tests, including the existing explicit post-cutoff FSDS
  issuer-name exclusion. Full suite increased 429 -> 432.

### Latest immutable local artifacts

- Discovery:
  `data/local-gate/name-plan-v14-corroborated-only/security-bootstrap/discovery/2026-07-17/de9c0b38e73374c6c52be75df76babe4f11d8933dce01b201c13f275075dba44`
- Cover:
  `data/local-gate/cover-v20-corroborated/security-bootstrap/complete-evidence/de9c0b38e73374c6c52be75df76babe4f11d8933dce01b201c13f275075dba44/2026-07-17/snapshots/855deb9cb9424459ecc7285ccff37af583b7070e8023b4c733b7e3ce82d1ba12`
- Master unchanged:
  `6e0421388702786b54bb3d0ab242a351f3f2d3dd167b8b32e9b940c2dbfc46a1`
- Metadata-rebased 4,313-target price universe:
  `data/local-gate/prices-lifecycle/universe-runs/b1d4f9c410e16bf226f318c34f4eccb9eede62eb94176f9448345169613d1123`
- Diagnostic universe:
  `data/local-gate/phase-2-3-corroborated/universe/2026-07-17/50bb61791948eee6a2b449874ec0490f678abc9ae12c4709a343638d953afa85`

The exact identity result is now **177 unmapped** and **84 superseded** (was
178 / 83). `RWTS` is the resolved stale listing: exact FSDS name evidence
corroborates CIK 930236 and filing cover evidence proves current common ticker
`RWT`.

Do not use the diagnostic run's 1,632 included / 162 sector-gap figures as a
new D032 measurement: the old-cover-bound filing-SIC supplement was omitted.
Also, the 155 newly targeted price series are still not locally downloaded.

### Exact non-duplicating continuation

1. Do not redo v1-v19 discovery/cover work or redispatch blocked GitHub run
   `30000558041`.
2. If continuing D032, start from the v14/v20 artifacts above. Rebase the
   filing-SIC supplement to the new cover metadata only after verifying its
   target CIK set and archived header payloads are unchanged. New provider data
   is still required for the 155 price additions and cache-missing SEC rows.
3. Under the approved deviation, the next new product task is Phase 3.1
   hygiene gates. Keep it a separate commit/PR-sized slice from `fff1529`.
4. Before every new implementation slice, read the Phase 3.1 rows in
   `docs/CODEX_TASKS.md` plus the referenced DATA/MODEL spec sections. Preserve
   PIT, as-first-filed and fail-closed behavior; add a look-ahead regression.
5. After each meaningful code/measurement slice, update this live checkpoint
   immediately. Do not wait until the session end.

## 0. Read this first: 2026-07-23 continuation checkpoint

This section supersedes older measurements and continuation instructions later
in this document where they conflict.

### Repository and verification state

| Item | Current state |
|---|---|
| Active branch | `agent/phase-2-3-universe-builder` |
| Latest functional commit | `4337072` (`Reduce Phase 2.3 identity gaps with PIT evidence`) |
| Remote state | `4337072` was pushed successfully to the active branch |
| Working tree at handoff edit start | Clean |
| Verification | `ruff check .` passed; full suite **413 passed in 41.42s** |
| Phase | **2.3 still in progress; D032 has not passed** |
| Active workflow | None |

Do not claim Phase 2.3 complete. The hard D032 acceptance rule remains zero
identity, FF49/sector and mandatory-input gaps, core applicability >=90% and
secondary applicability >=75%. No threshold was weakened.

### What Codex changed after the older handoff

- Cover reconciliation now safely unions same-CIK, exact-ticker/exchange,
  overlapping common-stock intervals despite harmless SEC identity wording
  drift. Genuine multi-class ambiguity remains quarantined.
- Added narrow historical ticker normalization for observed `BAX (NYSE)`,
  `BAX-(NYSE)` and unique allowed-pair `BFA` -> `BF-A` cases.
- Added filing-proven `superseded_sec_listing` classification. It requires a
  historical exact SEC interval ending before the cutoff, current different
  common ticker evidence, complete recent cover history and form proof.
  Future-known intervals cannot classify an old row; regression coverage
  explicitly guards this PIT boundary.
- Foreign-filer classification now accepts the conservative case where every
  master security for the CIK is filing-proven `domestic_flag=False`, even if
  archived cover history also contains 8-K/10-K forms.
- Added exact non-common product evidence for observed explicit trust/fund/debt
  issuer names. These are registered exact patterns, not broad suffix guesses.
- Added exact normalized EFTS entity-name discovery plus a suffix-insensitive
  company-stem candidate with explicitly weak confidence. Weak discovery alone
  cannot create a historical identity.
- The CLI discovery path gained optional `--listing-snapshot` support for
  exact entity-name matching.
- Strongly resolved unique CIKs can admit actual SEC cover tickers beyond a
  stale listing pair. Weak or multi-candidate discoveries remain fail-closed.
- Reconciliation/version constants were bumped so stale evidence cannot be
  silently read as if produced by the new rules.

### Latest local immutable artifacts and measurement

All work below was cache-only/offline; no SEC contact identity or provider
credential was fabricated.

- Discovery plan:
  `data/local-gate/name-plan-v13-stem-cache/security-bootstrap/discovery/2026-07-17/ac7f40631da48e4a39069c2bdfd50aeb58edfae1b631db6f790503aca69f0b20`
- Versioned cover evidence:
  `data/local-gate/cover-v19-versioned/security-bootstrap/complete-evidence/ac7f40631da48e4a39069c2bdfd50aeb58edfae1b631db6f790503aca69f0b20/2026-07-17/snapshots/fedb139a4fd15dde7fca5f913b706ca0d21789472a813d55745de0d9055c9339`
- Master snapshot hash:
  `6e0421388702786b54bb3d0ab242a351f3f2d3dd167b8b32e9b940c2dbfc46a1`

Latest 14,207-candidate identity status:

| Status | Count |
|---|---:|
| mapped | 5,682 |
| non-common | 1,996 |
| non-domestic | 292 |
| unmapped/identity gap | **178** |
| superseded SEC listing | 83 |
| exchange test | 43 |
| no periodic filing | 39 |
| not attempted / outside identity attempt | 5,894 |

The 178 identity gaps comprise 77 candidate-discovery rows and 101
no-candidate rows. This improves the immediately preceding local measurement
from 358 to 178, but it is not a D032 pass.

The new cover/master state expands the exact price universe from 4,159 to
4,313 targets: 155 additions and one removal (`LIXT`). Those 155 additions
have not been downloaded because no local Alpaca credentials are available.
Therefore the latest full universe/D032 result has not been measured and the
older mandatory/sector counts must not be presented as current.

### External-state findings

- No local `USINV_EDGAR_EMAIL`, Alpaca key/secret or usable `.env` was found.
- Cache-complete name discovery admitted useful identities, but roughly 50
  changed/new CIK candidates still lack local submissions cache.
- GitHub authentication initially used the wrong active account. Codex
  temporarily switched to `infenglov-hue`, pushed `4337072`, then restored
  `Somethinglikeu-hub` as the previously active account.
- A single non-duplicating SEC refresh was dispatched from plan run
  `29772784252`: GitHub Actions run
  [`30000558041`](https://github.com/infenglov-hue/USInv/actions/runs/30000558041).
  It never started. GitHub reported: recent account payments failed or the
  spending limit must be increased. No data artifact was produced.
- The user explicitly decided not to wait for GitHub: continue constructing
  and verifying the project locally; publishing can happen later.

### Exact next step for Claude

1. Stay on Phase 2.3 and inspect `git status` plus commit `4337072`. Preserve
   all local immutable artifacts above.
2. Do **not** redispatch run `30000558041` or wait for GitHub billing.
3. Continue cache-only classification of the 178 exact residual identity rows,
   preserving PIT and fail-closed rules. Start from the latest gap artifact;
   do not rebuild v1-v18 evidence.
4. For gaps that genuinely require new provider data, the user must set
   credentials locally without pasting them into chat:
   `USINV_EDGAR_EMAIL`, `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`. Once present,
   acquire only the missing SEC submissions and 155 added price targets.
5. Rebuild the complete local Phase 2.3 snapshot, measure current identity,
   FF49/sector, mandatory, core and secondary gates, and retain hashes/config.
6. Fix only evidence-backed residuals. Phase 2.3 ends only after a genuine
   local D032 pass, Ruff and the full test suite. GitHub upload may follow
   later.

## 1. Immediate repository state

| Item | State |
|---|---|
| GitHub repository | `infenglov-hue/USInv` (private) |
| Working directory | `C:\Ai Projects\USInv` |
| Main branch | `main` at `f71dc5e` — Phase 2.2 merged |
| Active development branch | `agent/phase-2-3-universe-builder` |
| Branch HEAD before this handoff edit | `4337072` |
| Latest functional code commit | `4337072` (`Reduce Phase 2.3 identity gaps with PIT evidence`) |
| Current phase | Phase 2.3, universe/identity/D032 remediation |
| Tests | 413 passing; Ruff passing |
| Active GitHub Actions | None; run `30000558041` was rejected before start by billing/spending limit |
| Active Codex monitoring | Stopped; do not assume any background continuation |

Do not work in or import from MobileInv. USInv is deliberately independent.

Required reading order:

1. `AGENTS.md` — non-negotiable engineering rules.
2. This file.
3. `docs/CODEX_TASKS.md`, Phase 2.3 only.
4. Relevant sections of `docs/DATA_SPEC.md` and `docs/BUILD_GUIDE.md`.
5. `docs/PROGRESS.md` only when historical detail is needed.

## 2. Product being built

USInv is a point-in-time US-equity research, ranking, portfolio and execution
system, eventually viewed through an independent installable PWA. The target is
profitability, but no return claim is allowed before reproducible artifacts and
the registered out-of-sample experiment.

Important user/product decisions already made:

- It is not a five-stock picker. The normal portfolio is intended to hold
  roughly 15 or more names, subject to the later portfolio rules.
- A separately ranked large-cap extension allows genuinely cheap, high-quality
  companies above $10B to enter; large firms are not structurally excluded.
- Portfolio changes are rotation/state transitions, not daily churn. Exact
  rotation implementation belongs to Phase 4.
- Cash is a valid output when the system cannot safely fill all slots or the
  later regime/portfolio rules reduce exposure. No arbitrary discretionary
  “go to cash” switch should be invented.
- Macro/regime data is planned in Phase 3.4 using vintage-correct series. It is
  not implemented yet.
- No insider-data dependency is planned. Public filing evidence is used; weak,
  stale or poorly covered insider feeds are not part of the v1 backbone.
- New IPOs enter the candidate universe only after dated listing membership,
  filing-backed identity and sufficient price/liquidity evidence. They are not
  bought automatically on listing day.
- Production must run unattended in GitHub Actions without the user's PC.
- Paper operation precedes any live capital. Live activation is a separate,
  explicit user gate.
- The PWA displays versioned engine output; it must not recompute rankings or
  expose licensed raw data/secrets.

## 3. Phase status

| Phase | Status | What exists / what remains |
|---|---|---|
| Blueprint/backbone | Complete | Architecture, data/model/ops/experiment contracts and build guide. |
| 0.1 scaffold | Complete/merged | Python 3.12, typed config, Ruff, pytest, CI, artifact guard. |
| 0.2 calendar | Complete/merged | XNYS sessions, half-days, T-1/T mapping and calendar traps. |
| 0.3 EDGAR client | Complete/merged | Polite <=8 req/s client, monitored UA, retries and immutable cache. |
| 0.4 feasibility | Complete/merged | Research mode selected; provider limitations documented. |
| 1.1 FSDS archive | Complete/merged | Versioned SEC quarterly ZIP archive. |
| 1.2 FSDS ingestion | Complete/merged | Lossless SUB/NUM/PRE/TAG and exact Decimal facts. |
| 1.3 PIT store | Complete/merged | As-first-filed vs latest separation; conflicts quarantined. |
| 1.4 fundamentals | Complete/merged | Tag chains, quarter derivation, TTM and coverage contracts. |
| 1.5 security/live edge | Complete/merged | Security master, filing XBRL, D030 applicability backbone. |
| 2.1 Alpaca prices | Complete/merged | Raw/all bars, immutable security-keyed price snapshots. |
| 2.2 actions | Complete/merged | Tiingo spot checks, Stooq basis gate, split/dividend reconciliation. |
| 2.3 universe | In progress, branch only | Real listing/universe/gate exists; D032 does not pass yet. |
| 2.4 freshness | Not started | Must follow a genuine Phase 2.3 pass. |
| 3 hygiene/signals/macro | Not started | No final ranking engine yet. |
| 4 portfolio/backtest | Not started | No portfolio selector or backtest yet. |
| 5 experiments | Not started | No performance result or selected final model. |
| 6 unattended/PWA | Not started | No PWA yet. |
| 7 paper-forward | Not started | Requires frozen configuration and working delivery. |
| 8 live activation | Not started/user-gated | Out of scope until explicit approval. |

The project is therefore not “almost finished”; the data/identity backbone is
being made trustworthy before building ranking, backtest and UI layers.

## 4. Phase 2.3 implementation already present

- Date-specific Alpha Vantage active+delisted listing snapshots with strict
  schema, request budget, pacing and immutable private storage.
- Filing-backed historical security master: entity, share class and ticker are
  distinct; mapping uses ticker+exchange validity intervals.
- Exact Alpha listing -> SEC discovery -> filing archive -> compact evidence
  shard -> reconciled security master pipeline.
- Exact security-keyed Alpaca price universe batches.
- Point-in-time universe filters: domestic common stock, approved exchanges,
  raw close, 21-session dollar volume, instant shares, issuer/class market cap,
  most-liquid line, financial/REIT/pre-revenue-biotech exclusions.
- Core and large-cap size buckets.
- Hash-pinned FF12/FF49 SIC classifications.
- Exact D030 applicability report over the final included denominator.
- Real D032 gate: zero identity/FF49/mandatory-input gaps, core >=90%,
  secondary >=75%. These thresholds remain unchanged.
- Immutable listing, discovery, SEC evidence, price and lifecycle artifact reuse
  paths for reruns.

## 5. Last trustworthy D032 result

Run `29810991030`, artifact `8487682614`, reused immutable listing run
`29769888331` and completed the real calculation. It failed closed with:

| Metric | Result |
|---|---:|
| Alpha listing candidates | 14,207 |
| Included securities | 1,461 |
| Identity gaps | 1,658 |
| FF49 gaps | 97 |
| Evidence gaps | 4,375 |
| Core applicability | 89.9286% |
| Secondary applicability | 67.1732% |

Acceptance still requires zero identity/FF49/mandatory gaps, core >=90% and
secondary >=75%. Never relabel this run as passing.

Gap diagnosis from the retained artifact:

- 1,377 unmapped, 279 quarantined and two invalid symbols.
- Among quarantined rows: 169 contain only explicit non-common SEC classes;
  92 contain only SEC-classified foreign issuers; 18 are genuine mixed/common
  collisions. Commit `f08232a` safely removes only the first 261 from the
  common-stock identity denominator.
- 346 unmapped names have an exact Tiingo series ending before the cutoff. This
  is diagnostic only, not PIT proof of delisting and not permission to remove
  the row.
- Many remaining unmapped names are ETFs, stale Alpha rows, foreign issuers,
  new IPOs, test/temporary symbols or domestic companies whose selected SEC
  filing did not expose a matching cover fact. Each category needs evidence,
  not suffix guesses.

## 6. Important fixes and lessons

### Provider/API contract issues

- Alpha Vantage returned HTTP 406 when an explicit CSV `Accept` header was
  sent. Fix: use the empirically proven User-Agent-only negotiation.
- Alpha listing company names can be blank. Fix: allow blank display name but
  continue rejecting blank symbol, exchange or asset type.
- Alpha's historical `2026-07-17` response drifted later by adding a post-cutoff
  IPO. Fix: D038 reruns reuse the hash-verified immutable listing from run
  `29769888331`; do not fetch Alpha again for this gate.
- One SEC shard previously hit a transient 429. It was retried in isolation;
  client pacing/backoff remains mandatory.
- Tiingo bulk contains 129 rows with blank exchange. Parser preserves them,
  but they can never prove an exchange-qualified identity/lifecycle claim.

### Identity issues

- Filing wording/dimension drift created duplicate IDs for equivalent equity
  classes. D035 semantic reconciliation collapses only evidence-equivalent
  classes and preserves genuine multi-class ambiguity.
- Official Nasdaq cover labels vary (`The Nasdaq Global Select Market`, etc.).
  Enumerated aliases were added.
- Some NYSE-American companies report contextual `NYSE` in cover facts. A
  narrow CIK+ticker discovery-backed reconciliation exists; unrelated venue
  disagreements remain quarantined.
- Preferred/warrant provider lines are excluded only when explicit evidence or
  empirically registered suffixes prove non-common status. Bare `-W` is not
  generalized because the retained snapshot also uses it for when-issued
  common stock.
- Coherent zero-volume/no-trade bars are zero liquidity, not corrupt prices.
- Current SEC ticker arrays are live-edge discovery only and never become
  historical validity intervals by themselves.

### SEC parser improvements in `f08232a`

- Inline XBRL containing named HTML entities such as `&nbsp;` is converted
  through Python's fixed local entity table to numeric references.
- DTDs and external entities remain prohibited.
- If the primary filing HTML has no usable XBRL, archived candidate instance
  XML is checked.
- Filing selection increased from two to four PIT-bounded cover-capable filings
  per CIK to improve identity recovery.

### GitHub runner disk failure and fix

SEC refresh `29815646806` used four filings per CIK but archived every XML/XSD
and also kept response-cache copies. Four shards failed with:

`OSError: [Errno 28] No space left on device`

The run could never form its exact partition and was cancelled. Commit
`ac10b01` fixes the infrastructure issue without altering evidence rules:

- Archive only the primary filing and possible XBRL instance XML.
- Do not fetch unused calculation/definition/label/presentation linkbases or
  schemas for the cover-identity path.
- In the shard workflow, disable only the duplicate binary response-cache
  copy; JSON submissions stay cached and consumed filing files remain
  hash-addressed in the accession archive.
- Added regression tests; full suite is 321 passing.

Disk-fix validation run `29821740675` was deliberately cancelled when the user
asked Codex to stop everything. It did not produce a result and must not be
treated as success or failure of `ac10b01`.

## 7. Relevant GitHub evidence

| Purpose | Run / artifact |
|---|---|
| Alpha credentialed listing smoke | run `29737419392` |
| Initial complete two-filing SEC bootstrap | run `29756489491` |
| Reconciled immutable SEC evidence used by last gate | run `29772784252` |
| Immutable Alpha listing to reuse | run `29769888331` |
| Last real D032 calculation | run `29810991030`, artifact `8487682614` |
| Four-filing refresh that exhausted disk | run `29815646806`, cancelled |
| Disk-fixed refresh stopped for handoff | run `29821740675`, cancelled |

Do not expose or commit raw licensed/provider payloads. Actions secrets already
exist for:

- `ALPACA_KEY_ID`
- `ALPACA_SECRET_KEY`
- `ALPHA_VANTAGE_API_KEY`
- `TIINGO_TOKEN`
- `USINV_EDGAR_EMAIL`

No EODHD secret/purchase is registered. Do not spend money or create provider
accounts without explicit user approval.

## 8. Exact recommended continuation

No workflow is currently running. Start only when the user asks Claude to
continue.

1. Confirm branch/worktree:

   ```powershell
   cd "C:\Ai Projects\USInv"
   git switch agent/phase-2-3-universe-builder
   git pull --ff-only
   git status --short
   ```

2. Verify locally before network work:

   ```powershell
   .\.venv\Scripts\python.exe -m ruff check .
   .\.venv\Scripts\python.exe -m pytest -q
   ```

3. Start exactly one disk-bounded four-filing SEC refresh, reusing the existing
   discovery plan so Alpha is not called:

   ```powershell
   gh workflow run alpaca-smoke.yml `
     --repo infenglov-hue/USInv `
     --ref agent/phase-2-3-universe-builder `
     -f bootstrap_plan_run_id=29772784252
   ```

4. Monitor the new run. Do not start a duplicate. If a shard fails, inspect the
   first concrete log error before changing anything. Confirm disk usage no
   longer grows to exhaustion.

5. On successful refresh, verify both `security-discovery-plan` and
   `filing-backed-security-evidence` artifacts and compare acquisition,
   parse-gap, security, symbol and reconciliation metrics with run
   `29772784252`.

6. Only if the refresh is valid and materially changes the symptom, start one
   D032 run through `alpaca-smoke.yml` with:

   - `cover_run_id=<new successful refresh run ID>`
   - `listing_run_id=29769888331`
   - leave `lifecycle_run_id` empty on the first D032 execution

   The first D032 run archives the Tiingo lifecycle ZIP. If D032 must be rerun,
   pass that D032 run ID as `lifecycle_run_id` so the diagnostic snapshot is
   immutable.

7. Verify `phase-2-3-universe-evidence`, coverage and gap artifacts. If D032
   still fails, classify exact remaining gaps; do not weaken thresholds or
   exclude rows merely to reach zero.

8. When D032 genuinely passes, document Phase 2.3 acceptance and only then
   begin Phase 2.4 freshness/coverage kill switches.

## 9. Roadmap after Phase 2.3

1. Phase 2.4: freshness/coverage gates and stale-data delivery kill switch.
2. Phase 3.1: real hygiene gates (shell, dilution, going concern, listing risk,
   suspension) with positive/negative fixtures.
3. Phase 3.2: single PIT scoring context used by historical and live paths.
4. Phase 3.3: value, quality, 12-1 momentum and composite scoring.
5. Phase 3.4: vintage-correct macro/regime overlay.
6. Phase 4: stateful portfolio selection, exits, one execution/ledger engine,
   toy hand-verification and registered experiment runner.
7. Phase 5: acquire the approved historical research archive, run TRAIN and
   VALIDATION search, register one configuration and unlock TEST once.
8. Phase 6: unattended durable runtime, paper broker parity, snapshot contract,
   independent PWA and notifications.
9. Phase 7: at least 12 rotations and 12 months paper-forward.
10. Phase 8: explicit user-gated live-capital activation.

## 10. Token- and Actions-efficient working rules

- Do not read the Codex transcript. This file plus targeted source files is the
  handoff.
- Do not reread all of `PROGRESS.md` unless a specific historical fact is
  missing.
- Use exact run/artifact IDs above instead of repeating provider downloads.
- Query run/job status compactly; fetch only failed-job logs.
- Never rerun the same workflow without a changed code/data symptom.
- Preserve successful immutable artifacts and use the registered reuse inputs.
- Run targeted tests while editing, then Ruff and the full suite once before
  push.
- Keep commentary concise and report actual metrics, not generic progress.

## 11. Non-negotiable correctness rules

- Point-in-time availability and as-first-filed facts.
- No future corporate action, filing or restatement leakage.
- Entity, security and ticker remain different keys.
- Zero/multiple identity matches are quarantined, never guessed.
- Missing fundamentals are never converted to zero without explicit structural
  evidence.
- Tiingo lifecycle end date is diagnostic only.
- Research output is not called audit-grade.
- No performance claim without frozen code/config/data artifacts.
- No PWA or live mode shortcut around failed data gates.
