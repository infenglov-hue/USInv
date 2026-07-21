# USInv — complete Claude handoff

Updated: 2026-07-21 after all Codex work and GitHub Actions were stopped at
the user's request.

This is the authoritative continuation document. It is intentionally detailed
enough that Claude should not need the Codex conversation transcript.

## 1. Immediate repository state

| Item | State |
|---|---|
| GitHub repository | `infenglov-hue/USInv` (private) |
| Working directory | `C:\Ai Projects\USInv` |
| Main branch | `main` at `f71dc5e` — Phase 2.2 merged |
| Active development branch | `agent/phase-2-3-universe-builder` |
| Branch HEAD | The commit containing this file; verify with `git rev-parse HEAD` |
| Latest functional code commit | `ac10b01` (`Bound SEC shard disk usage`) |
| Current phase | Phase 2.3, universe/identity/D032 remediation |
| Tests | 321 passing; Ruff passing |
| Active GitHub Actions | None; run `29821740675` was cancelled deliberately |
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
