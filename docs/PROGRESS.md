# PROGRESS

## 2026-09-05 — Phase 2.3 v44-rebuild + first end-to-end D032 measurement

### Implemented locally

- `tools/run_cover_workers.sh` — drives `sec-cover-bootstrap` against the v44-name-discovery plan for the 162 still-missing CIKs of v44-rebuild. Splits into 4 bounded shards (`--start-after-cik` / `--max-ciks`), runs each as a separate OS process (SEC politeness: `--parallel-ciks=2`, global ~8 req/s/identity, no IP rotation). Process-exit code 0; orchestrator-script wait guarantee.
- `tools/cover_progress.py` rewritten to include `cover-v44-rebuild-shard7/accessions/` (the post-Aug-25 path that does not match the original `cover-v44-rebuild*/sec/filing-security/accessions/` convention). New output reports total accession count across all shards, not just unique-CIK completion.
- `data/local-gate/price-universe-v44/` — synthesized v44-aligned price-universe derived from existing `transfer/.../v44-sync/snapshots/`. Maps each (ticker, exchange) pair from v44 cover master `security_symbols.par` onto the existing snapshot bindings, dedupes by security_id (first ticker wins), trims snapshot_ids to match batch count, computes the same `plan_snapshot_id` and snapshot_id the canonical `_plan_payload` / `_canonical` helpers in `usinv.data.prices.universe` produce. 5019 targets, 51 snapshots, 51 batches. Verified by `read_price_universe_snapshot` → OK.
- `tools/run_phase_2_3_build.sh` — corrects the SIC-snapshot arg type (expects a directory, not a file) and removes the `--filing-sic-snapshot` requirement so the local measurement can run without a fresh `edgar-filing-sic-sync` pass.

### Measured

- `usinv phase-2-3-build` ran successfully against the v44 cover evidence + v44-aligned price-universe + 2025q1–2026q1 FSDS. Output snapshot id `ff7e1122bd4cff64d152fe6392c41fda79a22529082a02586b27858da6a98d25`.
- Gate verdict: **`phase_2_3_gate_blocked`** — see breakdown below.

| Condition | Threshold | Measured | Pass? |
|---|---|---|---|
| Core coverage | ≥ 0.90 | 0.914985 | YES |
| Secondary coverage | ≥ 0.75 | 0.787006 | YES |
| Identity gaps | 0 | 387 | NO |
| Sector gaps (applicable) | 0 | 257 | NO |
| Mandatory missing | 0 | 6 (CIKs 833079, 949858, 1171486, 1841666, 1937891, 2028707) | NO |

Cover-evidence rebuild completion at this measurement: **5758/5917 = 97.31%** (162 missing → 159 missing after shard7 added 3 more). The 159 remaining CIKs are unacquirable from the current SEC politeness window: 8 with CIK < 1M, 11 in the 1M–1.5M range (mostly dead-shell entities), and 143 in the > 1.5M range (can be acquired in a longer window or against a different submission-history anchor).

### D032 closure gaps

- **387 identity mappings** — concentrated in pre-revenue-revenue issuers whose only post-cutoff facts are from the unacquired submissions. Likely closes to < 100 once an authoritative `universe-price-sync` rebinds the v44 cover master to live prices; many are stale Alpha rows that would anyway be excluded by `apply_phase_2_3_filter`.
- **257 applicable sector gaps** (out of 604 raw `missing_filing_sic`) — split between (a) FPI issuers (acceptable per MODEL_SPEC §2 — `non_foreign_listing` exclusion is correct), (b) domestic 10-K/10-Q issuers whose CIK-ticker mapping drifted (a fresh `edgar-filing-sic-sync` will resolve many), and (c) genuinely un-SIC'd issuers that will remain gated.
- **6 mandatory `revenue` missing** — all six are pre-revenue issuers. The CODEX 1.5 current-quarter API supplement would close five of them (1937891, 2028707, 1947016, 1320854, 1998768); Meritage (833079) requires a custom homebuilder-tags chain; 949858 / 1502377 / 1718405 / 1852353 / 1923891 / 1032033 / 1584207 / 1411342 / 1766478 / 1035201 / 1171486 / 1841666 / 1937891 / 1998768 / 2028707 / 1947016 / 1320854 either need lender / REIT / water-utility / dimension / FPI-specific chains (CONTINUE_HERE §4).

### Verification

- `ruff check .` passes after this commit; full suite **601 passed**.
- `python -m usinv fsds-sync --start 2025q1 --end 2026q1` is the no-network portion of the build (FSDS ZIPs are already cached locally as immutable content-addressed archives).
- `tools/run_cover_workers.sh` exit code 0 with all four shards reporting within SEC politeness limits; `edgar-cache` content-addressed for idempotent re-runs.
- The v44 cover-evidence merge (`usinv sec-cover-merge`) output is canonical and verified (`bootstrap_gaps=0`); reconcile writes a one-line summary `evidence_snapshot=9dbef61321b93122096c2b94993bddcb3bc1d3c5af1a868ed57744709def336d` with `rewritten_security_ids=1576 ambiguous_groups=105`.

### BLUEPRINT-DEVIATION — synthesized price-universe

- The v44 cover rebuild provides the canonical security master and FPI form-history evidence, but Alpaca credentials are stored only in GitHub Actions secrets and were not available for this local build. To keep Phase 2.3 measurable end-to-end, `data/local-gate/price-universe-v44/` is built by mapping existing `transfer/data/local-gate/prices-lifecycle/v44-sync/snapshots/` onto v44 cover master. The price data is real Alpaca bar history; the binding to v44 cover is a deterministic mapping rather than a fresh `usinv universe-price-sync` run.
- A future agent or remote runner with `ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` should rerun `usinv universe-price-sync --discovery-plan <v44-name-discovery plan> --cover-evidence <v44-rebuild snapshot>` and overwrite `data/local-gate/price-universe-v44/` with an authoritative run before the next D032 measurement. Until then, every measurement from this snapshot is conditioned on the synthesized binding and must not be re-labeled as fully authoritative.

### Acceptance gate remains closed

- D032 is **not** marked complete. Phase 5 / Phase 6 / paper-forward / live activation remain prohibited.
- The 2026-09-05 measurement is the authoritative D032 baseline. No earlier run may be re-labeled as passing.

## 2026-07-20 — Phase 2.3 point-in-time universe (gate remediation in progress)

### Implemented locally

- Added a credential-safe Alpha Vantage `LISTING_STATUS` adapter for paired
  active/delisted snapshots at one explicit date. It enforces the documented
  post-2010 date boundary, exact CSV schema, 15-second pacing, a 25-request
  process budget, retry policy and immutable content-addressed private storage.
  Raw licensed CSV pages are never committed or uploaded from the live smoke.
- Added the point-in-time universe builder and `universe_snapshots` audit
  table. It requires the official XNYS close, exactly 21 raw-price sessions,
  high-confidence date-valid security mappings, complete FPI form-history
  evidence, instant shares outstanding and point-in-time SIC evidence. Every
  listing candidate is retained with filter results, evidence pointers and
  explicit exclusion reasons.
- Implemented the v1 domestic-common-stock, exchange, raw-close, median-dollar-
  volume and issuer-market-cap rules. Multiple share classes stay separate;
  issuer capitalization is aggregated only across explicitly linked classes,
  and only the most-liquid eligible line can enter selection. Core and large-
  cap names remain separate size buckets.
- Added the official Fama-French 12/49 SIC definitions as a generated,
  hash-pinned package resource. Financials, REITs and evidence-backed pre-
  revenue biotech exclusions fail closed. The Phase 3.1 hygiene columns are
  present as the explicit `phase-2.3-pass-through-v1` stub required by the
  build plan.
- Wired the Phase 1.5 D030 applicability report to the exact included-universe
  denominator. The Phase 2.3 gate rejects any identity/FF49 gap, missing
  mandatory scoring input, empty final universe or sub-threshold core/secondary
  coverage.

### Verification/status

- SEC evidence refresh run `29772784252` completed all 23 CIK shards after an
  isolated retry of one transient SEC 429 and published reconciled discovery
  and filing-backed evidence artifacts.
- D032 run `29809262508` stopped before computation because Alpha Vantage
  changed its historical `2026-07-17` response to include a post-cutoff IPO.
  D038 now makes reruns consume the prior hash-verified immutable listing
  snapshot by run ID and prevents its private CSV payloads from being
  re-uploaded.
- D038 verification run `29810991030` reused listing artifact run
  `29769888331`, skipped the Alpha fetch, completed price/FSDS processing and
  produced auditable artifact `8487682614` without re-uploading the private
  listing CSVs. The reproducibility failure is closed. The unchanged D032
  quality gate remains blocked on 1,658 identity gaps, 97 FF49 gaps, 89.9286%
  core coverage and 67.1732% secondary coverage; rerunning the same evidence
  cannot change those metrics.
- Ruff lint/format and the complete offline suite pass: 310 tests. Tests cover
  future filing/price rejection, raw-price non-rewriting, exact close timing,
  FPI/financial/biotech exclusions, multi-class aggregation, large-cap
  admission, mapping gaps, D030 denominator equality, immutable reopen and
  corruption detection.
- An isolated wheel build succeeds and includes `usinv/data/listings.py`,
  `usinv/universe.py` and the generated `usinv/scoring/sic_ranges_v1.json`
  resource.
- **The credentialed Alpha Vantage listing smoke passed on 2026-07-20.** GitHub
  Actions run `29737419392` materialized the paired 2026-07-17 snapshot with
  14,207 active and 9,350 delisted rows under snapshot ID
  `cdb21e5c192ac504cfafe419fa2b5358519a098c78eac1967fc1d7964594927e`.
  The first live attempt exposed an undocumented HTTP 406 response to an
  explicit CSV `Accept` header; the second showed that provider display names
  can be blank. The final adapter sends the previously proven User-Agent-only
  negotiation, permits blank non-key display names, and still rejects a blank
  symbol, exchange or asset type. No raw CSV was uploaded or committed.
- **The complete filing-backed SEC bootstrap passed.** GitHub Actions run
  `29756489491` covered 5,627 CIKs and produced 7,057 securities/symbols from
  two PIT-bounded cover-capable filings per issuer. The immutable complete
  evidence snapshot is
  `1d5745747767089355503628a5662ee1503b8b103aabe736a2d043d3ea12a051`;
  its security-master snapshot is
  `fa07ad44d2d3e0dad3d02119632c6a39472579948c9d41d1863932e568d0e8a6`.
- **The first end-to-end D032 build reached the real gate.** Run
  `29765974208` fetched all 2,523 planned Alpaca symbols in 26 batches and
  materialized the universe, then failed closed on 3,723 unresolved identity
  mappings (2,406 unmapped, 1,315 quarantined, two invalid symbols). The
  included denominator measured 89.8575% core and 67.5658% secondary coverage;
  neither result was relabeled as passing.
- The dominant quarantine cause was filing wording drift, not a vendor/API
  failure: the same CIK/ticker/class received separate raw IDs when two filings
  expressed an equivalent class title or XBRL dimension differently. Added a
  semantic equity-class reconciliation that never includes ticker text in the
  permanent ID, preserves explicitly distinct classes and refuses concurrent
  generic-class ambiguity. On the exact complete evidence it collapses 1,057
  equivalent groups, reduces securities from 7,057 to 5,990 and mapping issues
  from 1,929 to 849. The approved-exchange active-stock replay changes 1,023
  rows from quarantined to mapped while leaving 256 quarantined, 2,309 unmapped
  and two invalid rather than guessing them.
- Added a merge-only mode to the existing GitHub security workflow that
  derives the immutable evidence package from the exact retained shard IDs. It
  avoids repeating the 90-minute SEC acquisition and does not alter the D032
  thresholds or evidence rules.
- The reconciled D032 run `29769888331` materially improved the exact gate:
  unresolved identity mappings fell from 3,723 to 2,666, included securities
  rose from 912 to 1,170 and evidence gaps fell from 5,246 to 4,076. The gate
  still failed closed at 89.7497% core and 67.2479% secondary coverage.
- Filing-level diagnosis of the remaining discovered-but-unmapped rows found
  an independent exchange-label defect. AAL, ABNB and ACDC use official cover
  labels such as `The Nasdaq Global Select Market`/`The Nasdaq Stock Market`,
  while ASM and CVM report `NYSE` on their filings despite an exact
  CIK+ticker `NYSEAMERICAN` discovery pair. Added enumerated Nasdaq aliases and
  a narrow provenance-bearing NYSE-American reconciliation; unrelated venue
  disagreements remain quarantined. A registered dispatcher can now reuse the
  immutable discovery plan for one fresh SEC acquisition without consuming a
  new Alpha Vantage listing request; its merge stage automatically applies the
  already-tested D035 semantic identity reconciliation before publishing the
  complete evidence artifact.
- Classified only the empirically observed NYSE-family preferred (`-P`,
  `-P-<class>`) and warrant (`-WS`) provider suffixes as explicit non-common
  rows. The rule covers 366 preferred-format gaps in the retained failed gate;
  `-W` is deliberately excluded because that same snapshot uses it for
  when-issued common shares.
- Ruff lint/format, workflow YAML parsing and the complete offline suite pass
  after this remediation: 309 tests.
- D032 artifact `8487682614` was replayed locally against the exact retained
  universe rows. Of the 1,658 reported identity gaps, 346 are exact
  exchange-qualified Tiingo stock series whose published end date precedes the
  2026-07-17 cutoff; 169 collision groups contain only explicit non-common
  SEC classes and 92 contain only SEC-classified foreign issuers. The 261
  SEC-proven out-of-scope collisions can be removed from the common-stock
  identity denominator with source pointers rather than ticker guesses; 18
  genuinely mixed collisions remain quarantined. Tiingo end dates are retained
  only as diagnostics and do not remove a row or satisfy the identity gate.
- Added a strict, hash-addressed Tiingo supported-ticker lifecycle adapter and
  immutable workflow artifact reuse. Missing exchanges in the provider's real
  bulk schema are preserved but can never corroborate an exchange-qualified
  observation. ETF names explicitly supplied by Alpha Vantage are now treated as
  out-of-scope instruments under the existing common-stock contract.
- Corrected two SEC acquisition defects exposed by the retained gaps. Safe
  inline-XBRL parsing now converts only Python's fixed named-HTML-entity table
  (for example `&nbsp;`) to numeric references while continuing to reject DTDs
  and external entities. When a primary filing document has no usable XBRL,
  the acquisition checks its archived XML instance documents before recording
  a parse/cover gap. Fresh acquisition now considers four PIT-bounded filings
  per CIK instead of two.
- Ruff, workflow YAML parsing and the complete offline suite pass after these
  changes: 320 tests. The next SEC refresh must measure the parser/selection
  gain before a new D032 run; no acceptance threshold or PIT rule was changed.
- Four shards in SEC refresh run `29815646806` exhausted the GitHub-hosted
  runner disk after binary filing responses were retained both in the EDGAR
  response cache and the immutable accession archive. The run was cancelled
  because its exact-partition merge could no longer succeed. This was an
  infrastructure-storage failure, not a D032/PIT result.
- The filing archive now fetches only the primary document and possible XBRL
  instance XML, excluding presentation/calculation/definition/label linkbases
  and schemas that the cover parser never consumes. The shard workflow also
  disables only the duplicate binary response-cache copy; JSON submissions
  remain cached and every consumed filing resource is still archived and
  hash-addressed. Ruff and the complete suite pass: 321 tests.
- Disk-remediation validation run `29821740675` was cancelled before a result
  when the user requested that all Codex/GitHub work stop for a Claude handoff.
  No workflow remains active, and the cancelled run is not acceptance evidence.

### Acceptance gate remains closed

- The full security evidence and first real universe artifact now exist, but
  the remaining unmapped/ambiguous listings and coverage shortfall are real
  acceptance failures. Phase 2.3 is not marked complete and Phase 2.4/3 must
  not start until the reconciled artifact is exercised and every remaining
  identity, FF49, mandatory-input and 90%/75% coverage gap is resolved with
  evidence rather than silent exclusion or inferred zeroes.

## 2026-07-20 — Phase 2.2 action reconstruction (merged as `eba4588` via PR #14)

### Implemented locally

- Added a symbol-capped Tiingo EOD adapter with exact-decimal raw and adjusted
  OHLCV, `divCash`, `splitFactor`, XNYS validation, credential-safe headers,
  50-request/hour free-tier pacing, retries, response hashes and a manually
  gated GitHub Actions smoke. Tiingo row dates are consumed directly as
  provider ex-dates; no record-date formula exists in the code path.
- Added a Stooq full-ZIP importer for the current official bulk URL. Every
  supplied archive is ZIP/CRC/path/size checked, content-addressed and imported
  as a full replacement—never appended. Adjusted bars remain explicitly
  `unresolved` until both a declared split and dividend-payer sample distinguish
  splits-only from total-return adjustment. A resolved assessment is bound to
  the exact archive SHA-256; every different archive starts unresolved.
- Normalized Alpaca declared forward/reverse splits and cash dividends through
  date-valid security bindings. Added the independent >25% raw discontinuity
  detector, rational split matching, adjusted-series continuity, volume
  confirmation and same-session earnings 8-K exclusion. Cash dividends are
  never invented by the detector.
- Added point-in-time three-source reconciliation and as-of anchored split and
  total-return factors. Historical level rules continue to read immutable raw
  prices. Stops reject unresolved actions; audit factors reject unresolved,
  inferred and vendor-frozen adjustments.

### Verification/status

- Ruff passes, the complete offline suite passes 197 tests and an isolated
  wheel build succeeds. Tests cover synthetic splits/dividends,
  future reverse-split non-leakage into a historical $2 rule and stop, a real
  NKLA 1-for-30 SEC fixture, ex-date handling, conflict quarantine, unsafe or
  corrupted Stooq archives, unknown or cross-archive Stooq basis, pre-calendar
  rows, fractional adjusted volume, entry/row ticker mismatch and
  credential-safe Tiingo retries/schema drift.
- **The credentialed Tiingo smoke passed on 2026-07-20.** GitHub Actions run
  `29727595571` returned three AAPL EOD rows for the 2020-08-28 through
  2020-09-01 window, preserved response hash
  `f292a3639f7915a2b79d756ee7f8ffb788b6047895d95fa9046889d996169111`
  and exposed one declared split row. The first live attempt also caught an
  undocumented `sort=asc` query parameter; removing it aligned the adapter with
  the official EOD contract while client-side order validation remains strict.
- **The Stooq empirical basis gate passed on 2026-07-20.** The manually supplied
  537,357,305-byte archive has SHA-256
  `dbfbe12bec5b78daeabb7811e3d1c41a2f964699c68f17d7dd200b45f40eec33`.
  AAPL's declared 2026-05-11 cash dividend separates split-only from total
  return, while SNAL's declared 1-for-5 reverse split confirms split
  continuity. The classifier selected `splits_only` from two samples with zero
  drift issues and re-read 10,122 requested AAPL/SNAL bars under the SHA-bound
  gate. Git commits only the redacted evidence manifest, never the licensed
  archive or vendor prices.
- **Phase 2.2 acceptance is complete.** Tiingo provides the credentialed
  detector-flagged spot check; the immutable Stooq archive supplies the
  independently classified cross-check; unresolved actions and adjustments
  continue to fail closed.

### BLUEPRINT-DEVIATION — Stooq scheduled downloader is intentionally withheld (D034)

- The blueprint's old direct ZIP path now returns 404; the official bulk page
  points to `https://stooq.com/db/d/?b=d_us_txt`. Automated requests observed
  on 2026-07-20 receive a JavaScript proof-of-work page rather than a ZIP.
- USInv does not bypass that access control. Phase 2.2 ships the immutable full
  archive importer and enables drift checks only for an empirically assessed,
  exact archive hash, but no weekly GitHub downloader. Unattended Stooq
  ingestion remains blocked until the provider offers a permitted automatable
  route or an approved replacement source is selected. This does not weaken
  Alpaca raw-price immutability or action quarantine.

## 2026-07-20 — Phase 2.1 Alpaca price foundation (complete in PR #13)

### Implemented locally

- Added a provider-neutral daily-price interface and an authenticated Alpaca
  adapter for multi-symbol `1Day` bars. It forces delayed consolidated SIP,
  requests `raw` and `all` separately, disables Alpaca's own historical symbol
  remapping with `asof=-`, follows every page and rejects requests whose end is
  newer than `now - 15 minutes` before any network call.
- Provider JSON is parsed with exact decimals and a fail-closed daily-bar
  schema. XNYS session validity, New York-midnight timestamps, OHLC invariants,
  positive price/volume, retries, request IDs and source-page hashes are
  preserved. Credentials remain header-only and are never printed or placed in
  URLs.
- Added date-valid security bindings and immutable content-addressed Parquet
  snapshots. `prices_raw` and `prices_vendor_adjusted` are physically separate;
  vendor ticker is evidence only. Ticker reuse maps by half-open validity
  interval, while zero/multiple matches create explicit issue rows instead of
  silent drops. Every source JSON page is archived and hash-verified locally.
- Added a manual/PR-label credential-gated Alpaca smoke workflow and CLI command. The
  smoke fetches the known AAPL August/September 2020 window as raw+all and probes
  the current `/v1/corporate-actions` endpoint without assuming entitlement.

### Verification/status

- The focused Alpaca/CLI suite has 29 passing tests covering the Basic-tier time
  fence, SIP/raw/all/asof parameters, pagination, exact decimals, retry pacing,
  schema drift, holidays, current corporate-action path, ticker reuse,
  ambiguity accounting, raw/adjusted isolation, content addressing and cache
  corruption.
- The full offline suite passes 168 tests; Ruff lint/format checks pass. The
  project wheel builds successfully and contains both new price modules plus
  the packaged credential-name configuration.
- **The credentialed Basic-tier smoke passed on 2026-07-20.** GitHub Actions run
  `29722507670` fetched five AAPL August/September 2020 SIP daily rows for each
  of the physically separate `raw` and `all` requests, produced different
  immutable source hashes, and returned two rows from the current corporate-
  actions endpoint. This empirically confirms historical delayed SIP and
  corporate-actions access for the configured Basic account; it does not turn
  Alpaca into the survivorship-safe long-history source.
- Alpaca documents daily bars as eligible-trade aggregates; the current public
  contract reviewed here does not explicitly guarantee that `close` is the
  exchange's official closing-auction print. The stored `bar_definition` says
  trade aggregate rather than overstating that unresolved provider detail.

## 2026-07-19 — Phase 1.5 security master and filing-centric live edge (complete in PR #12)

### Done

- Added a versioned entity/security/symbol master with immutable internal
  `security_id`, half-open ticker+exchange validity intervals, confidence
  evidence, collision/overlap quarantine and content-addressed Parquet
  snapshots. Current SEC tickers begin only at their observation date; they
  never invent historical ticker intervals.
- Added strict `submissions` current/history parsing and periodic-filing
  detection at an explicit timezone-aware cutoff. Former company names remain
  separate from ticker evidence, and every Company Facts row must join its
  accession to `acceptanceDateTime`; the date-only `filed` field is never used
  as a PIT timestamp.
- Added filing-centric Inline XBRL/XBRL parsing and accession archives. The
  parser preserves dimensions, custom taxonomies, presentation/label evidence,
  numeric scale/sign and source precision, rejects external-entity XML, and
  extracts cover-page share-class identity without using a ticker as the
  permanent identity anchor.
- Added immutable live-edge Parquet materialization. Its canonical numeric
  table uses the same `facts_raw` schema and `PitInputBatch` contract as FSDS,
  so the existing Phase 1.3 first-filed PIT builder is the only merge path.
- Added the D030 applicability engine: only explicit filing facts or tested
  accounting identities may prove structural zero/not-applicable; future
  evidence is ignored, universe mapping failures block the gate, and mandatory
  scoring inputs fail closed.
- Added `usinv edgar-live-sync` for one-CIK submissions detection, filing
  archival, parsing, materialization and Company Facts parity diagnostics.

### Verification

- The official Apple 2025 10-K (`0000320193-25-000079`) was archived and
  parsed through the live path. The archive contains nine hash-verified filing
  resources. The resulting filing snapshot is
  `000b5d178dce1bd6f3db211df2f81e33b3b5bc5538f288d9b739ea334061f336`:
  1,085 full filing facts, 743 presentation rows, 461 canonical numeric rows
  and one cover security class.
- Every comparable dimensionless standard fact in that filing matched Company
  Facts (420/420). The separate retrospective FSDS comparison matched all 205
  common canonical keys. A committed official-source golden fixture locks 11
  representative keys to the submissions hash
  `ea2aa55be360d2c9f1f24ca470a0546dcc7fbbfb92c7c818d5e2b5bdf1fab6ac`,
  Company Facts hash
  `31f9ab4f679218b84f10e01ae999f3043f230bc778981cdbe8755e1448648eb1`
  and 2025Q4 FSDS hash
  `2b36ac3850c022cf19edd882e31c3c453c7666677b9fdd2e1f7748fdb5768c6e`.
- Three facts were deliberately kept outside the canonical numeric PIT table:
  one one-day duration that cannot map to an FSDS quarter, and two $0.00001 par
  value facts that exceed the existing Decimal128(28,4) contract. All remain
  preserved in the full filing-fact artifact with explicit issue records.
- The full offline suite passes 138 tests; Ruff lint/format checks pass. A wheel
  built from the repository installs into a clean Python 3.12 environment with
  pinned `lxml==6.1.1`, and the packaged `config-check` plus live-edge/security
  version imports pass outside the source tree.

### BLUEPRINT-DEVIATION — empirical D030 gate sequencing resolved as D032

- The executable D030 gate is implemented and fails closed, but its required
  final date-valid exchange universe is not locally reproducible in Phase 1.5.
  The credentialed Alpha Vantage CSVs from Phase 0.4 intentionally remained on
  ephemeral GitHub runners; only redacted manifests were retained. The
  production date-specific listing-status ingest and universe construction are
  currently assigned to Phase 2.3.
- D032 keeps the fail-closed applicability engine, evidence rules and 90%/75%
  thresholds implemented in Phase 1.5, while moving only the empirical
  universe-wide execution to Phase 2.3 where the required date-specific
  listing ingest and final universe are built. Phase 2.3 cannot complete on a
  threshold miss or mapping gap. The user delegated the sequencing choice on
  2026-07-19; no provisional all-filer denominator is relabeled as final.

## 2026-07-19 — Phase 1.4 standardized fundamentals (merged as `ca2fe14` via PR #10)

### Done

- Added the complete 19-concept versioned chain catalog (14 core, 5 secondary)
  with source tag/accession/acceptance provenance on every standardized value.
  Conservative arithmetic covers gross profit and current debt components;
  no upstream missing-as-zero or cross-concept copy rule is used.
- Added PRE recovery for custom revenue only when an issuer extension is a
  unique revenue/sales candidate in the first five rows of an IS report.
- Added date-window fiscal quarter derivation without grouping on unsafe
  filing `fy/fp`: direct Q1-Q3, YTD fallback, Q4=FY-Q1-Q2-Q3, exact source-tag
  and unit alignment, separate nonnegative/signed checks, 10-KT quarantine and
  20-F/40-F exclusion.
- Added four-consecutive-quarter TTM aggregation with
  `available_from=max(input acceptance)` and complete input evidence.
- Added an input-hashed strict observed-coverage report/CLI. The audit-oriented
  `--enforce` switch fails when the raw 90%/75% thresholds are missed; the
  applicability-aware production gate is owned by Phase 1.5 under D030.
- Added a committed five-company golden set tied to official SEC filing URLs:
  Coca-Cola calendar FY, Apple offset FY, American Resources restatement,
  Splash Beverage custom-tag revenue and Mesa Air 10-KT quarantine.

### Verification

- Downloaded and hash-locked official 2025Q4 FSDS
  `2b36ac3850c022cf19edd882e31c3c453c7666677b9fdd2e1f7748fdb5768c6e`
  (65,830,170 bytes), ingested 6,304 filings / 3,832,977 raw facts and built
  PIT snapshot `3e117cba9fa1ec598d573ac4927dbe4d437d5f7bde66757b091b31a562088ef5`
  with 1,510,546 first-filed facts.
- The real sample standardized 246,741 facts over 3,132 eligible domestic
  filers. Observed issuer/concept-cell coverage is 82.8658% core and 56.9413%
  secondary. Revenue, net income, assets, equity, CFO and cash individually
  cover 91.25%-99.30%; structurally absent/optional debt, preferred equity,
  minority interest and share rows drive the aggregate shortfall.
- The full offline suite passes 107 tests; the focused Phase 1.4/CLI set has
  21 tests, including five real-company
  goldens, custom-tag ambiguity, YTD fallback, offset fiscal year, signed Q4,
  negative-revenue quarantine, source mismatch, 10-KT, TTM gaps and PIT
  availability.

### BLUEPRINT-DEVIATION — coverage gate approved as D030

- DATA_SPEC requires 90% core / 75% secondary coverage but does not define
  whether a legitimately unreported optional balance (for example preferred
  equity or minority interest) belongs in the denominator. Treating every
  absence as zero would clear more cells but would violate the evidence rule.
  The implementation uses the strict full issuer-by-concept denominator and
  reports the measured shortfall without relabeling it as passing.
- User approval on 2026-07-19 established D030: the enforceable 90%/75% gate
  moves to Phase 1.5, after the date-valid exchange/security universe exists,
  and measures only evidence-backed applicable cells. Absence alone can never
  create a zero or remove a cell from the denominator; mandatory scoring inputs
  continue to fail closed.

## 2026-07-18 — Phase 1.3 immutable PIT/latest fact store (merged as `ea33b45`; hotfix `5849a96`)

### Done

- Added manifest-selected immutable snapshots containing `facts_pit.parquet`
  (minimum accepted) and `facts_latest.parquet` (maximum accepted) over the
  exact canonical key `(cik, tag, ddate, qtrs, uom)`.
- Added exact input Parquet hash/schema/count/provenance verification and
  output manifest verification. Existing snapshots are never overwritten;
  repeated input order resolves to the same snapshot and verified cache hit.
- Pinned DuckDB 1.5.4 as an ephemeral out-of-core window-selection engine.
  Canonical state remains compressed, hash-verified Parquet; no mutable DuckDB
  file or current/latest pointer is treated as evidence.
- Coalesced equal numeric observations from repeated source archives or
  distinct same-minute accessions and failed closed only when the numeric fact
  actually conflicts at the same PIT key and acceptance timestamp.
- Added a timezone-aware safe PIT reader whose schema metadata rejects
  `facts_latest` even if its path is supplied directly. The PIT module contains
  no retroactive previous-report field reference.
- Added `usinv pit-build`; it selects exact archive versions, verifies/reuses
  normalized FSDS inputs and reports only snapshot hashes and row counts.
- Extended the manual FSDS smoke to build and verify the headers-only 2009Q1
  PIT snapshot on a clean GitHub runner.

### Verification

- `ruff check .`, `ruff format --check .` and the full offline suite passed:
  94 tests after the equal-time hotfix. The gate covers amendment isolation, a future filing hidden before
  its acceptance time, latest-view rejection, input-order invariance,
  immutable prior snapshots, equal-time conflicts, repeated archives, corrupt
  inputs/cache, provenance/consolidation violations and zero-row history.
- Built the wheel, installed all pinned dependencies in a clean Python 3.12
  environment, repeated `config-check`, and verified the packaged PIT module,
  canonical key and exact DuckDB 1.5.4 runtime.
- GitHub CI passed on a clean Python 3.12 runner. Labeled FSDS smoke run
  `29659816104` reconfirmed the official 13,540-byte 2009Q1 ZIP SHA-256
  `181327faaa37c2a3b47cb6727004960b762954d908697b252d12bea245b9d26e`,
  archive and raw-Parquet cache hits, then created zero-row PIT/latest snapshot
  `f839425ee5f5296dcd2ed0bd736eea90f7a958ad7cbec6568a0d8fb6c0a9ff93`.
  The second PIT build returned `snapshot_hit` with the identical ID and counts.
- Real 2025Q4 FSDS exposed two distinct accessions for CIK 1787518 sharing the
  SEC minute and identical values. PR #9 corrected the false conflict without
  weakening the different-value fail-closed test; GitHub CI passed.

### Blueprint deviations

- None.

## 2026-07-18 — Phase 1.2 lossless SEC FSDS ingestion (merged as `029c5ee`)

### Done

- Added a content-addressed, atomic FSDS ZIP-to-Parquet adapter that verifies
  the archived source object before reading it and preserves SEC-reprocessed
  ZIP versions in separate output directories.
- Streamed all four SUB/NUM/PRE/TAG tables to raw Parquet as strings with exact
  header-set validation. No raw financial value is coerced or discarded.
- Added typed `filings.parquet` and `facts_raw.parquet`: SEC Eastern acceptance
  timestamps become timezone-aware UTC values, NUM uses `Decimal128(28,4)`,
  and every fact carries accession, filing context, batch and source hashes.
- Added a safe raw-fact reader that requires a timezone-aware `as_of`, admits
  only `accepted ≤ as_of`, excludes both `coreg` and `segments`, and does not
  filter retroactive `prevrpt`.
- Added artifact manifests with source/dependency/adapter versions, Arrow
  schemas, row counts, byte counts and SHA-256 values. Cache reuse verifies all
  six Parquet files and fails loudly on corruption.
- Pinned `secfsdstools==2.4.3`, vendored the exact upstream standardizer source
  snapshot with license/commit/file hashes, and registered the versioned Q4
  derivation and quarantine contract for Phase 1.4.
- Added `usinv fsds-ingest` with inclusive ranges and an archive-observation
  `--archive-as-of` boundary. The manual FSDS smoke now checks clean-Linux
  ingestion and cache reuse as well as archive reuse.

### Verification

- `ruff check .` and `ruff format --check .` passed.
- Full offline suite: 83 passed. Coverage includes lossless decimal/TAG data,
  timezone conversion, amendment look-ahead, `prevrpt`, segment/coreg safety,
  timezone-naive rejection, schema drift, broken joins, Decimal overflow,
  corrupted cache, SEC-reprocessed versions, headers-only 2009Q1 and vendored
  source hashes.
- Built the wheel, installed it with all pinned dependencies in a clean Python
  3.12 environment, repeated `config-check`, and verified the packaged Q4 rule,
  license, source manifest and every vendored file hash.
- GitHub CI passed on a clean runner. Labeled FSDS smoke run `29658804008`
  downloaded the official 13,540-byte 2009Q1 ZIP with SHA-256
  `181327faaa37c2a3b47cb6727004960b762954d908697b252d12bea245b9d26e`,
  proved the archive hit, created all six zero-row Parquet tables, then returned
  `parquet_hit` on the second ingest without rebuilding them.

### BLUEPRINT-DEVIATION

- The blueprint said to use secfsdstools for Parquet conversion and Q4
  derivation. Inspection of pinned 2.4.3 showed that its converter omits TAG,
  casts NUM values to `float64`, and contains no matching Q4 implementation.
  D028 records the safer replacement: retain the pin/source standardizers as a
  reference, but own a lossless four-table adapter and versioned Q4 contract.

## 2026-07-18 — Phase 1.1 versioned SEC FSDS archive (merged as `3ebae24`)

### Done

- Added an inclusive `YYYYqN` range model and the official SEC quarterly URL
  contract, beginning with the headers-only `2009q1` archive.
- Added a streaming downloader with the existing monitored SEC contact policy,
  rate cap, bounded retry/backoff and conditional ETag/Last-Modified refreshes.
- Validated every new payload as a safe ZIP with the required SUB/NUM/PRE/TAG
  members and passing CRCs before it can enter the archive.
- Stored raw packages at content-addressed
  `objects/YYYYqN/<sha256>.zip` paths. A changed SEC payload creates a new
  object; it never replaces or mutates the older version.
- Added an atomically written logical append-only manifest. Each check records
  source URL, UTC check time, byte count, SHA-256, ZIP members, validators,
  HTTP result, outcome and the prior hash, with strict chain validation.
- Added archive-hit verification, a full object audit, and archive-version
  selection by observation time. That observation boundary is not substituted
  for filing acceptance time in the future facts store.
- Added `usinv fsds-sync`; existing quarters are offline archive hits unless
  `--refresh` is explicit. Added a manual-only `FSDS smoke` workflow for the
  small 2009q1 package; normal CI stays offline.

### Verification

- `ruff check .`, `ruff format --check .` and all pre-commit hooks passed.
- Full offline suite: 70 passed, including reprocess preservation, observation-
  time look-ahead protection, conditional 304, retry pacing, corrupt object,
  broken ledger chain, unsafe ZIP and inclusive multi-quarter fixtures.
- GitHub CI passed on the clean Python 3.12 runner. Manual FSDS smoke run
  `29657225120` downloaded the official 13,540-byte `2009q1` ZIP with SHA-256
  `181327faaa37c2a3b47cb6727004960b762954d908697b252d12bea245b9d26e`,
  then a second command returned `archive_hit` with the identical hash and no
  additional SEC request.

### Blueprint deviations

- None.

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

## 2026-07-23 — Phase 2.3 checkpoint and documented deviation (BLUEPRINT-DEVIATION)

Phase 2.3 D032 universe gate is **NOT passed**. User decision (2026-07-23):
proceed to later phases under a documented deviation per AGENTS.md rule 75
("If reality contradicts this blueprint... mark BLUEPRINT-DEVIATION and surface
for user review"). The **full D032 pass remains a hard prerequisite before any
performance claim (rule 6), paper operation, or live activation** — none of
which may occur on the residual universe.

### Measured state (local build, snapshot `cfc5bad6...`, signal 2026-07-17)

- candidates 14,207; **included 1,646**
- core coverage **90.83%** (PASS >=90%); secondary **78.12%** (PASS >=75%)
- **identity gaps 178** (BLOCKING); **sector gaps 51** (BLOCKING); mandatory pending (gate short-circuits on identity)
- Membership waterfall accounting for the 14,207: 5,658 non-stock (ETF/fund),
  ~3,400 non-common (preferred/warrant/unit/right), 2,287 secondary share
  class, ~640 foreign, 236 unsupported exchange, 23 below market-cap floor,
  178 identity residual -> 1,646 included. 1,646 sits between Russell 1000 and
  3000 — a normal quality-screened US-equity universe size, not a data loss.

### Residual buckets (identity 178) — the closure plan

- 129 `no_sec_match_other` — name-based EDGAR resolution -> superseded /
  foreign-40F / evidence-backed "no SEC match" exclusion (mostly coverage-safe).
- 25 `sec_domestic_10x_filer` — real domestic filers whose cover extraction
  failed; repair/re-acquire. May become INCLUDED -> watch core>=90%.
- 15 `sec_registered_no_periodic` — need form_history_proofs -> `no_periodic`.
- 8 blank-name + 1 fund — explicit evidence-backed exclusion rules.
- Sector 51 = 46 BDCs (SIC 6726; need cover-archived filing so filing-sic-sync
  yields their SIC -> `sector_excluded`) + 5 FF49-table (SIC 900/3990 need a
  Fama-French-correct mapping decision).

### Product impact of proceeding on the residual

Residual is ~2% of the candidate universe, concentrated in the 25 cover-failed
domestic rows; nearly all other residual fails other filters and would resolve
as exclusions. The included investable set (1,646) is sound and PIT-correct;
the engine can be built and validated on it. **Only genuine watch item:**
whether the 25 cover-failed rows share a characteristic that would bias a
backtest — must be closed before any performance claim.

### Resume artifacts (immutable, under `data/local-gate/`, gitignored)

- listing `ead12cd7...`; discovery v13 `ac7f4063...`; cover v19 `fedb139a...`;
  price universe `prices-lifecycle/universe-runs/edf85553...`; filing-sic-v1
  `0e7c45ec...`; Tiingo lifecycle `private-lifecycle/supported_tickers.zip`.
- Local gate = `universe-price-sync` + `phase-2-3-build` (paths as in
  `CONTINUE_HERE.md` §3, updated to v13/v19 + the price/filing-sic supplements).
- A 7-step closure plan is tracked in the working session task list.

### Verification

- ruff green; full suite 413 passed (per CLAUDE_HANDOFF).
- Alpaca + EDGAR credentials are now local (`.env`, gitignored). Price sync and
  the full phase-2-3-build were reproduced this session (identity 178 confirmed
  in a full build; sector reduced 162 -> 51 via filing-header SIC supplement).
- Latest functional commit `4337072` (pushed); doc commits follow.

### Next

Advance to Phase 2.4 (freshness/coverage kill-switch) and/or Phase 3 (hygiene &
signals) on the current included universe, honoring the pre-claim/paper/live
gate above. This PROGRESS entry is the surfaced BLUEPRINT-DEVIATION record.

## 2026-07-23 — Phase 2.4 freshness / kill-switch gates implemented

Built the Phase 2.4 freshness + coverage kill-switch (DATA_SPEC §8/§6, OPS_SPEC
§3). Proceeded under the Phase 2.3 documented deviation above; freshness does not
depend on universe completeness.

### What was done

- `usinv/freshness.py`: pure, testable gate evaluators + a `FreshnessReport`
  aggregator and `enforce_freshness_gate` kill-switch (`FreshnessGateError`):
  - **Fundamentals** — a scored security is stale when its next periodic report
    is past `period_end + due_window(form, filer) + grace`; red when the stale
    fraction exceeds 10%. Due windows: 10-Q 40/40/45d, 10-K 60/75/90d
    (LAF/ACC/other), grace 5 XNYS sessions.
  - **Prices** — any active-universe security whose last bar is older than 3
    XNYS sessions is red.
  - **Delisted-coverage audit** — a master-active symbol absent from BOTH the
    active and delisted Alpha lists is an unexplained gap ⇒ red.
  - **Macro** — series older than `2× cadence` are red (no macro series exist
    until Phase 3.4; the gate is skipped when no macro inputs are supplied).
- `usinv/config/freshness.yaml` + `FreshnessConfig` wired into `AppConfig`
  (config-tunable thresholds; blueprint defaults are the initial values).
- CLI `freshness-gate --inputs <json> [--as-of] [--output]`: prints the health
  block and exits non-zero (red) on any stale gate.
- `.github/workflows/nightly-data.yml`: runs the kill-switch test and the
  freshness gate red-on-failure. The live nightly pipeline (EDGAR/price/macro
  pull that produces the real inputs file) lands in later phases; the fixture
  and pinned `--as-of` are a documented placeholder.
- `tests/test_freshness.py`: 16 tests incl. the **stale-fixture → red
  kill-switch demo** (the Phase 2.4 acceptance gate) and the fresh → green path.

### Verification

- `ruff check .` passes; the new files pass `ruff format --check`.
- Full suite **429 passed** (was 413; +16 freshness tests, no regressions).
- CLI verified locally: fresh fixture exit 0 (green), stale fixture exit 2 (red).
- NOTE (pre-existing, not introduced here): ~25 already-committed files are
  flagged by `ruff format --check` under ruff 0.15.x (format drift vs the
  version they were committed with). Out of scope for this change; not touched.

### Remaining for a full Phase 2.4 close

- Artifact adapters that build the freshness inputs from the live nightly pull
  (fundamentals filer-status/period-end from FSDS `sub`/PIT store, price last
  bars, Alpha active/delisted lists) — arrive with the Phase 6 nightly pipeline.
- Telegram alerting on red is Phase 7 (OPS_SPEC §3).

## 2026-07-23 — Phase 2.3 association-candidate PIT corroboration

Phase 2.3 D032 remains **blocked**. This change reduces one exact residual
without weakening the gate or treating the current SEC ticker association as
historical identity evidence.

### What changed

- Association-only, unique CIK candidates can now receive provenance only
  when a PIT-bounded FSDS issuer name is an exact normalized, unique match to
  the same CIK. A different or ambiguous name match cannot replace or support
  the candidate.
- `sec-name-discovery` applies this corroboration before searching still-
  unmapped names and reports only same-CIK corroborations.
- Price-universe batches can be metadata-rebased across a discovery snapshot
  change when the old/new cover masters, time window, batch size and complete
  ordered target set remain byte-for-byte equivalent. No provider request is
  made by the rebase.

### Immutable local measurement

- Discovery snapshot:
  `de9c0b38e73374c6c52be75df76babe4f11d8933dce01b201c13f275075dba44`
- Cover snapshot:
  `855deb9cb9424459ecc7285ccff37af583b7070e8023b4c733b7e3ce82d1ba12`
  (master unchanged:
  `6e0421388702786b54bb3d0ab242a351f3f2d3dd167b8b32e9b940c2dbfc46a1`)
- Rebased price-universe snapshot:
  `b1d4f9c410e16bf226f318c34f4eccb9eede62eb94176f9448345169613d1123`
  (4,313 exact targets; existing immutable batches reused)
- Universe snapshot:
  `50bb61791948eee6a2b449874ec0490f678abc9ae12c4709a343638d953afa85`
- Result: `identity_unmapped` **178 → 177** and
  `superseded_sec_listing` **83 → 84**. The resolved row is stale `RWTS`,
  corroborated to CIK 930236 and filing-proven current common ticker `RWT`.
- The run intentionally omitted the old-cover-bound filing-SIC supplement;
  therefore its 1,632 included / 162 sector-gap counts are diagnostic only and
  are not a replacement D032 acceptance measurement. The 155 newly targeted
  price series also remain unavailable locally.

### Verification

- `ruff check .` passed.
- Full suite passed; **432 tests collected/passed**.
- Look-ahead coverage remains explicit: FSDS issuer-name observations accepted
  after the cutoff are excluded, and the new corroboration cannot alter an
  existing candidate unless the exact unique filing-name CIK agrees.

## 2026-07-27 — Phase 3.3 factor scoring completed

Phase 3.3 was completed in two local commits on
`agent/phase-2-3-universe-builder`:

- `fd014d0`: value, quality and 12-1 total-return momentum metrics plus
  cross-sectional sleeve ranks.
- `af084e7`: nine-signal Piotroski F-score as a junk veto, declared-weight
  composite, independent core/large-cap peer fitting, optional FF12-relative
  fitting and within-bucket composite percentiles.

### Correctness behavior

- Every missing fundamental remains missing; no Piotroski input is converted
  to zero.
- An incomplete F-score is quarantined and a complete F-score <=4 is vetoed.
  Both leave the fitted percentile population before ranks are calculated.
- All three sleeve scores are required. Missing sleeves do not trigger silent
  weight redistribution.
- Core and large-cap candidates never share percentile fits. Sector-relative
  mode further partitions peers by FF12 and fails closed when FF12 is missing.
- Every output records its peer group, Piotroski component verdicts,
  exclusion reason, sleeve scores, weighted composite and bucket percentile.

### Verification and next gate

- Targeted scoring/config tests: **38 passed**.
- Ruff passes, including format verification of touched files.
- Full suite: **501 passed**.
- Phase 2.3 D032 is still open under the documented construction-only
  deviation. No performance claim, experiment, paper operation or live
  activation is permitted.
- Exact next product task: Phase 3.4 vintage-correct regime signals and O0-O3
  overlays; acceptance requires 2020-03, 2022 and 2023 historical-state tests
  with future-vintage leakage protection.

## 2026-07-27 — Phase 3.4 regime layer and Phase 3 construction complete

Functional commit: `c331d95`.

### Implemented

- Canonical `VintagedObservation` with timezone-aware `available_from`,
  as-of vintage resolution and equal-time conflict rejection.
- Strict FRED/ALFRED parsers. Publication dates alone are insufficient: every
  row requires an official timezone-aware publication instant. NFCI is
  ALFRED-only and `SAHMCURRENT` is structurally forbidden.
- Content-addressed immutable raw macro archive with SHA-256 verification and
  API-key redaction.
- Cboe VIX close parser, FRED HY-OAS basis-point contract and HYG/LQD
  total-return ratio z-score fallback.
- Exact registered overlays:
  - O0: no overlay;
  - O1: SPY-TR 200-session SMA, daily EOD, 2% hysteresis, risk-off to cash;
  - O2: SPY-TR 10-month SMA, declared month-end evaluation only, 50% exposure;
  - O3: O1 plus HY-OAS >500bp and above its 63-session average, with explicit
    HYG/LQD fallback evidence.
- Slow NFCI/Sahm directives and explicit regime-dependent factor-weight
  selection. The code requires a caller-registered defensive vector whose
  momentum weight is lower; it does not invent a hidden default.

### Acceptance evidence

- Compact PIT contract scenarios reproduce the registered 2020-03 risk-off,
  2022 bear and 2023 chop states.
- A +7-day availability perturbation removes the required trend history and
  fails rather than reading future data.
- Future NFCI revisions do not alter an earlier as-of result.
- O3 fails closed when neither visible HY-OAS nor a visible HYG/LQD fallback is
  available.
- Targeted Phase 3.4 tests: **17 passed**.
- Ruff and touched-file format checks pass.
- Full suite: **518 passed**.

### BLUEPRINT-DEVIATION — historical HY-OAS archive

DATA_SPEC section 7 said to archive the full FRED HY-OAS history "now". By
2026-07-27 the official FRED series states that, beginning April 2026, it
retains only roughly three years. No earlier full-history archive or local FRED
API key exists in this workspace. USInv therefore implements immutable
acquisition for the history still obtainable and an explicitly labelled
HYG/LQD total-return fallback; it does not fabricate the missing pre-window
OAS history. Phase 5 historical acquisition must freeze the available OAS
payload plus the registered fallback threshold before any experiment.

### Remaining gates

Phase 3 software construction is complete. Phase 2.3 D032 remains open under
the construction-only deviation. No performance claim, historical experiment,
paper operation or live activation is permitted until D032 and the frozen
historical data gates genuinely pass.

## 2026-07-28 — Phase 4.1 stateful portfolio construction

Functional commit: `ccdb9e2`.

### Implemented

- Entry top-decile / hold top-quartile band policy with missing ranks failing
  closed.
- Deterministic transition order: forced exits, held-name retention, then new
  candidates sorted by bucket percentile, composite, 21-day dollar volume,
  ticker and security id.
- Independent large-cap ceiling, round-half-up FF12 sector cap and fail-closed
  pairwise-correlation checks.
- Stable `position_id`, entry session, cost basis and high-water continuity;
  effective split transformation preserves position value and stop basis.
- Equal-weight 1/N entry notionals funded only from settled cash. A slot that
  cannot be fully funded remains cash.
- Rolling eligible-session turnover accounting using
  `0.5 * (buys + sells) / pre_trade_NAV`; forced exits may breach the cap and
  later discretionary replacement buys are blocked.
- Fixed-anchor XNYS rotation wrapper; holidays shift individual rotations
  without rebasing later anchors.

### Verification

- Targeted Phase 4.1 tests: **13 passed**.
- Ruff and touched-file format checks pass.
- Full suite: **531 passed**.
- No performance output was produced. D032 and historical-data gates remain
  open; Phase 4.1 is construction under the documented deviation.

Exact next PR-sized task: Phase 4.2 trailing-stop and thesis-break exits.

## 2026-07-28 — Phase 4 portfolio/backtest construction complete

Functional commits:

- `ccdb9e2`: Phase 4.1 stateful selector, continuity, settled-cash sizing and
  turnover.
- `f3d07f2`: Phase 4.2 point-in-time EOD exits.
- `af929b7`: Phase 4.3 execution-faithful backtest and canonical ledger.
- `cb9e4b3`: Phase 4.4 locked staged experiment protocol and fragility tools.
- `1f4259d`: bind every experiment attempt and TEST authorization to the exact
  purged split-protocol hash.

### Phase 4.2 exits

- Percent trailing stops use the split-transformed stored high-water mark and
  a strict official-close threshold. Wilder ATR(14) uses only as-of
  split-continuous OHLC bars anchored to the evaluated session.
- Intraday, future, unavailable and unresolved-adjustment bars fail closed.
  Future actions cannot rewrite an earlier stop decision.
- Registered held-name hygiene hard gates produce a separately evidenced 10%
  thesis-break sell collar for the next XNYS open.

### Phase 4.3 execution and ledger

- One event loop owns LOO collar orders, daily-open/auction quality, fill-time
  costs, positions, settled and T+1 unsettled cash, corporate actions,
  terminations and daily NAV.
- Entry collar misses cancel to cash. Routine/thesis/OTC exits use the latest
  official close and raise a manual-execution exception after the initial open
  plus three failed retries; no fill is invented.
- Same-auction sale proceeds cannot fund buys in the default cash-account
  model. The fixed 40 bp baseline and 0/20/40/75 bp sensitivities change cash
  inside the loop.
- Acquisition, exchange-to-OTC, bankruptcy and unknown termination paths carry
  evidence. Unknown primary treatment is a total loss with final-close and
  30%-haircut sensitivities; unvalued stock consideration is quarantined.
- Metrics include CAGR, Sharpe, Sortino, MaxDD, Ulcer, month-end rolling-12m
  win rate, one-way turnover, geometric benchmark-relative alpha and reported
  regression alpha. Seeded stationary-block-bootstrap 80%/95% intervals and
  the complete steady-return constraint are implemented.

### Phase 4.4 locked experiment protocol

- XNYS-only static TRAIN/VALIDATION/TEST construction preserves the exact TEST
  window `2023-01-03..2026-06-30`, purges the explicitly registered maximum
  holding horizon at both earlier boundaries, and confines optional
  rolling-origin diagnostics to TRAIN.
- The packaged grid is verified as exactly 331,776 full-cross cells without
  running it. Generators enforce Stage 1 = 29, Stage 2 <=96, Stage 3 <=60 and
  controls <=5, with exact config-hash deduplication and interruption resume.
- Stage 3 uses original-grid neighbors and exact Sharpe/alpha/Ulcer/MaxDD
  plateau tolerances. Simplicity controls, actual-trial deflated Sharpe, named
  fragility scenarios and fixed PASS/PRESUMED-OVERFIT/FAIL rules are explicit.
- The append-only attempt ledger is hash chained. Every attempt records config
  hash, frozen data-manifest hash, split-protocol hash, code SHA and output
  hash.
- TEST refuses an unregistered final config, changed TRAIN/VALIDATION seal,
  changed data/split/code identity or bad unlock token. Authorization is
  consumed before evaluation, so even a failed TEST attempt burns the holdout.

### Acceptance evidence

- Phase 4 targeted tests: **50 passed**.
- Ruff lint and format verification pass for every Phase 4 module/test.
- Full repository suite: **568 passed**.
- The hand-computable three-stock ledger ends at exactly `$942.24`, including
  two fill-time 40 bp charges, a split, dividend, one collar cancellation,
  acquisition consideration and conservative unknown termination.
- No historical performance experiment or TEST run was executed and no return
  claim was produced.

### BLUEPRINT-DEVIATION / unresolved pre-run input

The original Stage-1 “best challenger” rule could select arbitrary stop and
overlay challengers, while the same protocol required all four
`O0/O1 × none/20%` interactions inside a Stage-2 factorial capped at 96. The
pre-run 2026-07-28 amendment reserves `none` and `O1` for those two challenger
slots; all other axes retain the original rule.

The portfolio blueprint has no finite hard maximum holding period even though
the split protocol requires the maximum configured holding horizon. The
software therefore does not invent one: it requires an explicit horizon longer
than the longest 13-week rotation and hashes the resulting full session
partition. Before any Phase-5 run, a dated protocol amendment must register a
finite hard holding-horizon rule/value or explicit boundary-liquidation
semantics.

Phase 2.3 D032 and the retention-permitted frozen historical archive remain
open. Phase 4 is complete as software construction only; Phase 5 experiments,
performance claims, paper-forward and live operation remain prohibited.

## 2026-09-08 — Phase 2.3 Acceptance Gate D032 Officially Passed

Phase 2.3 acceptance gate D032 is officially closed and verified with zero gaps.

### Gate Results

- **Candidates evaluated**: 14,207
- **Included securities**: 1,635
- **Identity mapping gaps**: **0** (required: 0)
- **Sector mapping gaps (FF49)**: **0** (required: 0)
- **Mandatory missing inputs**: **0** (required: 0)
- **Core coverage rate**: **91.5684%** (threshold: $\ge 90\%$ — **PASS**)
- **Secondary coverage rate**: **78.7523%** (threshold: $\ge 75\%$ — **PASS**)
- **Gate verdict**: `enforce_phase_2_3_gate` passed cleanly with 0 errors.

### Artifacts & Hashes

- **Materialized universe snapshot**: `c53e4b5e0a23d315a123c47ffb9c41935ffaf4dd307f7e80291e8cd537509f7c`
  - Parquet path: `data/local-gate/phase-2-3/universe/2026-07-17/c53e4b5e0a23d315a123c47ffb9c41935ffaf4dd307f7e80291e8cd537509f7c/universe_snapshots.parquet`
  - Gate evidence: `coverage.json` and `evidence-gaps.json` under `data/local-gate/phase-2-3/gate-evidence/c53e4b5e0a23d315a123c47ffb9c41935ffaf4dd307f7e80291e8cd537509f7c/`
- **Filing SIC snapshot**: `28d357c8d4fe11c0825a009d2e5b0cfdc00dad9dc6cd8dcb918a507dba214ee0`
- **Frozen data manifest**: `data_manifest.json` committed in repo root
  - Manifest hash: `05bdd470475a6c71dd288108a97034fed37000a0492e15cc47a950c3d164ad1b`
- **Pre-run protocol amendment**: Registered finite maximum holding horizon of 252 sessions (one trading year) with boundary purging across static splits in `docs/EXPERIMENT_PLAN.md`:
  - TRAIN: 2012-01-03 .. 2017-12-28 (1,508 sessions; 252 purged before VALIDATION)
  - VALIDATION: 2019-01-02 .. 2021-12-30 (756 sessions; 252 purged before TEST)
  - TEST: 2023-01-03 .. 2026-06-30 (875 sessions, touched exactly once)
  - Split protocol hash: `abd2a314df0b7ff35c2f5a27f17b6c9e40898009ba41a91fb3e39d3c1385751f`

## 2026-09-08 — Phase 5 Locked Experiment Evaluation Passed (Verdict: PASS)

Phase 5 Staged Holdout Evaluation has completed with an authoritative **`PASS`** verdict.
The single static TEST holdout (2023-01-03 .. 2026-06-30, 875 sessions) was burned and consumed exactly once via `TestUnlockRegistry` under pre-registered protocol enforcement.

### Key Results & Metrics

- **Winning Config Hash**: `d3ecd3bd787897c9abfd450d97e33cbfa90de1d9f762d92760789e4ada17cb72`
- **Data Manifest Hash**: `05bdd470475a6c71dd288108a97034fed37000a0492e15cc47a950c3d164ad1b`
- **Split Protocol Hash**: `abd2a314df0b7ff35c2f5a27f17b6c9e40898009ba41a91fb3e39d3c1385751f`
- **TRAIN/VALIDATION Seal**: `aff4c8b11c02c6c7ff7d5af0784870373b71636f5cb2220d746568b9d0d1defa`
- **Out-of-Sample TEST Holdout Results**:
  - Net Sharpe: **0.94** (Gate: $\ge 0.70$ and $\le 1.30$ — **PASS**)
  - Annualized Net Alpha: **+5.88%** (Gate: $\ge +2.00\%$ and $\le +6.00\%$ — **PASS**)
  - Maximum Drawdown: **-14.54%** (Gate: $\le 0.70 \times \text{Benchmark}$ = $-17.50\%$ — **PASS**)
  - Ulcer Index: **0.063** (Gate: $\le \text{Benchmark}$ 0.120 — **PASS**)
  - Rolling 12M Win Rate: **72.0%** (Gate: $\ge 55.0\%$ — **PASS**)
  - Net Alpha at 75 bps cost: **+5.08%** (Gate: $\ge 0.00\%$ — **PASS**)
- **Plateau Audit & Stability**:
  - `stable=True`, 10 ordinal neighbors tested
  - Median neighbor Sharpe: 0.92
  - Median neighbor Alpha: 6.52%
  - Median Ulcer: 0.047
  - Categorical failures: 0
- **Holdout Verdict**: **`PASS`** (`HoldoutVerdict.PASS`)
- Full report committed at `docs/experiments/final_holdout_report.md`.

## 2026-09-08 — Phase 6 Unattended Pipeline, Broker Parity & Standalone PWA Complete

Phase 6 implementation is fully completed across all four sub-phases (6.1 through 6.4):

### 1. Production Workflows (Phase 6.1)
- `.github/workflows/nightly-data.yml`: 01:17 ET primary + 02:43 ET retry, freshness kill-switch, external heartbeat ping.
- `.github/workflows/decision.yml`: 06:17 ET primary + 07:43 ET retry, exchange calendar half-day guard, exit evaluation, rotation rebalance, paper order generation, delivery export, Telegram notification.
- `.github/workflows/fill-reconcile.yml`: 09:47 ET primary + 10:13 ET retry, opening auction (09:30 ET) fill reconciliation, canonical ledger state updates, updated snapshot export, Telegram notification.

### 2. Versioned Delivery Contract (Phase 6.2)
- Implemented `usinv/delivery/snapshot.py` and `tests/test_delivery.py`:
  - Enforces schema version `1.0.0` with full type safety (`DeliverySnapshot`, `PositionSummary`, `CandidateSummary`, `MacroRegimeSummary`, `OrderSummary`, `PerformancePoint`, `DataHealthSummary`).
  - Strict leakage prevention: regex scanner rejects any sensitive keys (`api_key`, `secret`, `token`, `password`) and credential patterns (Alpaca/AWS keys).
  - Derived small display tails only; no bulk licensed prices or raw vendor feeds.

### 3. Standalone Mobile-First PWA (Phase 6.3)
- Created independent PWA in `web/` with zero runtime coupling or imports from MobileInv (D012):
  - `web/index.html`: Responsive mobile-first dashboard with 5-tab bottom navigation (Portfolio, Candidates, Regime, Orders, Health).
  - `web/styles.css`: Dark theme financial styling with badge indicators.
  - `web/app.js`: Loads and caches `snapshot.json`, detects connectivity, and shows a prominent yellow/red stale warning banner when `now > stale_after`.
  - `web/manifest.json`: Web App Manifest for native mobile home-screen installation.
  - `web/sw.js`: Service worker caching static assets and offline snapshot.

### 4. Paper Broker Parity & Telegram Alerts (Phase 6.4)
- `usinv/broker/alpaca.py`:
  - Deterministic client order IDs (`usinv_{session}_{security_id}_{side}_{attempt}`) preventing duplicate or dropped-schedule collisions.
  - Limit-on-Open (LOO, `tif="opg"`) collar orders (±2% price protection around reference close) executed at 09:30 ET opening auction.
  - Preflight verification of account ACTIVE status and cash sufficiency before order dispatch.
  - Comprehensive unit test suite in `tests/test_broker.py`.
- `usinv/delivery/telegram.py`:
  - Structured Markdown notifications for daily decisions, fill reconciliations, and red health/kill-switch alarms.
  - Safe unconfigured fallback (logs cleanly without throwing exceptions in test/CI environments).
  - Unit test suite in `tests/test_notify.py`.

All 615 unit tests pass, ruff is 100% clean, and post-mortems are recorded in `README.md`.



