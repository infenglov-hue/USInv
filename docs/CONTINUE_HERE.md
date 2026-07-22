# CONTINUE HERE — Phase 2.3, snapshot 2026-07-21 evening

> Bu dosya, işi başka bir araçla (ChatGPT dahil) veya PC yeniden açıldıktan sonra
> kesintisiz sürdürmek için yazıldı. Kısa Türkçe özet en altta. Read this whole
> file first, then `docs/HANDOFF.md` and `docs/REVIEW_NOTES_2026-07-21.md`.

## 0. Can we continue from another tool / after a reboot? YES.

Everything needed is on disk and in git:
- All code is committed and pushed (branch `agent/phase-2-3-universe-builder`,
  HEAD `9644819` at time of writing). `git status` is clean.
- All gate inputs are immutable local artifacts under `data/local-gate/`
  (gitignored, but present on this PC).
- The measurement runs LOCALLY (no GitHub Actions needed) — see §3.
- The only running process is the background v4 cover refresh (§2), which is
  fully resumable after any interruption.

Nothing is held only in a chat session. A cold start = read the three docs
above + run the commands below.

## 1. Where we are (exact status)

Phase 2.3 D032 universe acceptance gate. Four blocking conditions; **2 of 5
pass**:

| Condition | Value | Pass? |
|---|---|---|
| Core coverage >= 90% | 91.49% | YES |
| Secondary coverage >= 75% | 78.72% | YES |
| Identity gaps == 0 | 830 | no |
| Mandatory-missing == 0 | 18 | no |
| Sector/FF49 gaps == 0 | 107 | no |

Last fully-measured run: local build on 2026-07-21 (also CI run `29834169463`
for the coverage rates). Session start was 4/4 failing with identity 1,658 —
today it is 2/5 passing with identity 830.

Two code commits are UNMEASURED against a gate (`e97139d` 40-F regime bump,
`83e9d01` zero-revenue soundness fix) because they require the v4 cover
evidence, which the running refresh is regenerating.

## 2. The running background refresh (v4 cover evidence)

**What it is:** re-download + re-archive SEC cover filings for all 5,627 CIKs in
the discovery plan, this time recording 40-F filings as foreign-filer-regime
evidence (commit `e97139d`). Needed because `COVER_MERGE_VERSION` was bumped
v3 -> v4, so the old v3 evidence can no longer be read.

**Command that is running** (single-threaded; SEC politeness-limited to
~11-12 filings/min; ~22,000 filings total => ~24-30h wall clock):

```bash
cd "/c/Ai Projects/USInv"
export USINV_EDGAR_EMAIL="keremdemirbas53@gmail.com"
root="C:\\Ai Projects\\USInv\\data\\local-gate"
plan="$root\\plan\\2026-07-17\\3416bcd44a7d6122c4c3cacc05ae287758975886b92aff0aca925c26bb514ae7"
python -m usinv sec-cover-bootstrap \
  --discovery-plan "$plan" \
  --as-of "2026-07-17T16:00:00-04:00" \
  --avoid-duplicate-binary-cache \
  --max-filings-per-cik 4 \
  --output-dir "$root\\cover-v4" \
  --archive-dir "$root\\cover-v4-archive" \
  --cache-dir "$root\\edgar-cache"
# then, only after the bootstrap completes the FULL set:
python -m usinv sec-cover-merge --discovery-plan "$plan" \
  --evidence-root "$root\\cover-v4" --output-dir "$root\\cover-v4"
python -m usinv sec-cover-reconcile \
  --cover-evidence "$(ls -d "$root"/cover-v4/*/2026-07-17/snapshots/* | head -1)" \
  --output-dir "$root\\cover-v4"
```

**Progress check** (how many filings archived so far, target ~22,000):

```bash
ls -1 "/c/Ai Projects/USInv/data/local-gate/cover-v4-archive/accessions" | wc -l
```
At snapshot time: ~2,730 / ~22,000 (~12%).

## 2a. Does it resume if the PC shuts down / the process is killed? YES.

The download is idempotent and cached on disk. Two independent safety layers,
both verified in code:

1. **Persistent EDGAR cache** (`usinv/data/edgar/client.py`, `_request_resource`):
   every request checks `data/local-gate/edgar-cache/*.body.json` first and
   returns the cached copy without touching the network when present and fresh.
   So already-downloaded filings become instant cache hits on a re-run.
2. **Immutable hash-addressed archive** (`data/local-gate/cover-v4-archive/`):
   already-archived accessions are reused, never re-fetched or duplicated.

Interruption loses at most the ONE filing being downloaded at the instant of the
kill. Everything already on disk is kept.

**To resume after any interruption: re-run the EXACT same `sec-cover-bootstrap`
command in §2.** Done CIKs replay from cache (fast, no network wait); it
continues network fetches only for the remainder, then writes the output. No
special flag is required.

- Optional faster resume: `sec-cover-bootstrap` processes CIKs in ascending
  order and supports `--start-after-cik <N>` and `--max-ciks <M>` to skip
  already-done CIKs entirely. Only needed to avoid the (cheap) cache-replay.
- IMPORTANT: the `sec-cover-merge` step requires a COMPLETE partition (all
  5,627 CIKs, nothing deferred). So finish the full bootstrap before merging.

The background task output log on this PC:
`C:\Users\kerem\AppData\Local\Temp\claude\C--Ai-Projects-USInv\dfc80014-395c-49f9-a81a-7135bb1dcb06\tasks\bdfn1hbpf.output`

## 3. How to MEASURE the gate locally once the v4 refresh is done

After bootstrap + merge + reconcile produce a reconciled v4 snapshot under
`data/local-gate/cover-v4/`, run:

```bash
cd "/c/Ai Projects/USInv"
export USINV_EDGAR_EMAIL="keremdemirbas53@gmail.com"
root="C:\\Ai Projects\\USInv\\data\\local-gate"
python -m usinv fsds-sync --start 2025q1 --end 2026q1 --archive-dir "$root\\fsds-archive"
python -m usinv phase-2-3-build \
  --listing-snapshot "$root\\listing\\private-listing\\alpha-vantage\\listing-status\\2026-07-17\\ead12cd7b99379038d84cdd3bd4b6ec4be94ff04e9fc2f3c05b9e878687d669b" \
  --discovery-plan "$root\\plan\\2026-07-17\\3416bcd44a7d6122c4c3cacc05ae287758975886b92aff0aca925c26bb514ae7" \
  --cover-evidence "<RECONCILED v4 SNAPSHOT DIR under $root\\cover-v4>" \
  --price-universe "$root\\prices-lifecycle\\universe-prices\\universe-runs\\10df6688b6c2e1603648a16a59d21675a06d6f13268a1fc400524bf75dbfa24d" \
  --tiingo-lifecycle-zip "$root\\prices-lifecycle\\private-lifecycle\\supported_tickers.zip" \
  --signal-at "2026-07-17T16:00:00-04:00" \
  --fsds-start 2025q1 --fsds-end 2026q1 \
  --archive-dir "$root\\fsds-archive" --parquet-dir "$root\\fsds-parquet" \
  --store-dir "$root\\pit-store" --output-dir "$root\\phase-2-3"
```

The final stdout line reports `identity_gaps=`, `sector_gaps=`, `core_coverage=`,
`secondary_coverage=`, and `gate=passed|blocked`. Detailed evidence lands in
`$root\phase-2-3\gate-evidence\<snapshot>\{coverage.json,evidence-gaps.json}`.

**Verification criterion for the 40-F change:** identity gaps should drop from
830 by roughly the number of 40-F-only foreign filers (small, tens). If it drops
by MORE than the diagnostic upper bound (117 foreign + 97 no-periodic = 214
total classifiable, from `scratchpad/unmapped_classification.json`), investigate
the `no_periodic` soft spot in `docs/REVIEW_NOTES_2026-07-21.md` §2.

## 4. Honest note: the refresh is NOT the path to a passing gate

The v4 refresh only helps the small 40-F-foreign subset (~tens of rows). It will
NOT close identity to zero. The real remaining work (does not need this refresh):

1. **571 stale / no-SEC-ticker-match rows** — the largest identity bucket. Needs
   name-based EDGAR resolution (a separate, smaller request set), not the bulk
   cover refresh. See `scratchpad/unmapped_classification.json`
   (`no_sec_match_*` categories).
2. **96 domestic 10-K/10-Q filers whose cover extraction failed** — repair the
   cover parse, keep them as real gaps until resolved.
3. **18 mandatory-missing cells** — implement the CODEX_TASKS 1.5 current-quarter
   API supplement (FSDS 2026q2 is unpublished); consider
   `InterestAndDividendIncomeOperating` as a documented revenue fallback for
   in-scope lenders (1032033, 1584207, 1411342, 1766478); investigate custom /
   dimensioned top lines for Meritage 833079, NRP 1171486, APA 1841666.
4. **107 sector/FF49 gaps** (`missing_filing_sic`) — ride on the same evidence.

Only after identity + mandatory + sector all reach zero does Phase 2.3 pass;
then Phase 2.4 (freshness / kill-switch), then Phase 3.

## 5. Code review findings from 2026-07-21 (for a second reviewer)

Full packet: `docs/REVIEW_NOTES_2026-07-21.md`. Two headline items:
- FIXED (`83e9d01`): the zero-revenue accounting identity was unsound for the
  `OperatingExpenses` tag (which excludes COGS); now restricted to single-step
  statements. Conservative (can only leave a cell uncovered, never assert a
  false zero); changes zero current numbers.
- FLAGGED, not changed: the `no_periodic_filing_at_cutoff` classification infers
  "no periodic filing" from archive absence, which could fail-open if a domestic
  filer's archiving failed. Verify at the v4 measurement using the §3 criterion.

## 6. Guardrails that must never be relaxed (from AGENTS.md)

- Point-in-time / as-first-filed; no future filing or restatement leakage.
- Entity, security, ticker are different keys; zero/multiple matches are
  quarantined, never guessed.
- Missing fundamentals are NEVER converted to zero without an explicit filed
  structural-zero identity.
- D032 thresholds (core 0.90, secondary 0.75, zero identity/sector/mandatory)
  are never loosened to make the gate pass.
- Tiingo lifecycle end dates are diagnostic only.

## Türkçe özet

- **Nerede kaldık:** Phase 2.3, 4 kabul şartından 2'si geçti (core %91,5 +
  secondary %78,7). Kalan: kimlik 830, mandatory 18, sektör 107.
- **Başka araçtan devam mümkün mü:** Evet. Tüm kod commit+push, tüm veri diskte,
  ölçüm lokal. Soğuk başlangıç = bu üç dosyayı oku + komutları çalıştır.
- **PC kapanırsa:** Refresh yarıdan devam eder. İnen her dosya diskte cache'te;
  §2'deki AYNI komutu tekrar çalıştırınca inenler anında geçilir, kalanlar iner.
  En fazla o an inen tek dosya yarım kalır. `merge` adımı için 5.627 CIK'in
  hepsi bitmiş olmalı.
- **Dürüst not:** Bu refresh kapıyı geçirmez, sadece ~onlarca 40-F satırını
  düşürür. Asıl iş 571 bayat satır + 18 mandatory + 107 sektör (§4).
