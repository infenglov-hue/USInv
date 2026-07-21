# USInv compact handoff

Updated: 2026-07-21. Read this file first; open `PROGRESS.md` only for history.

## Exact state

| Item | Value |
|---|---|
| Repository | `infenglov-hue/USInv` (private) |
| Branch | `agent/phase-2-3-universe-builder` |
| Current commit | `ac10b01` |
| Current phase | Phase 2.3 identity/universe acceptance remediation |
| Active SEC refresh | `29821740675` — disk-bounded retry from `ac10b01` |
| Reused discovery plan | run `29772784252` |
| Reused immutable Alpha listing | run `29769888331`, date `2026-07-17` |
| Last real D032 run | `29810991030`, artifact `8487682614`, failed closed |
| Local verification | Ruff green; full suite `320 passed` |

The ranking engine, portfolio engine, backtest and PWA are intentionally not
built yet. Phase 2.3 must pass before Phase 2.4/3 under `AGENTS.md` and
`CODEX_TASKS.md`; do not skip or weaken this gate.

## Last measured gate

`29810991030` produced 14,207 listing candidates, 1,461 included securities,
1,658 identity gaps, 97 FF49 gaps, 4,375 evidence gaps, 89.9286% core coverage
and 67.1732% secondary coverage. Required: zero identity/FF49/mandatory-input
gaps, core >=90%, secondary >=75%.

Gap diagnosis on the retained artifact:

- 279 quarantined: 169 all non-common SEC classes, 92 all foreign SEC
  issuers, 18 genuinely mixed collisions. Commit `f08232a` safely excludes
  only the first 261 from the common-stock identity denominator.
- 1,377 unmapped plus two invalid symbols. Of the unmapped rows, 346 have an
  exact Tiingo series ending before the cutoff, but this is diagnostic only;
  it must not satisfy the PIT identity gate or be treated as proof of delisting.
- Explicit Alpha ETF names are out of the common-stock scope.

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

1. Monitor SEC refresh `29821740675`; do not start another refresh while it runs.
2. On success, verify `security-discovery-plan` and
   `filing-backed-security-evidence` artifacts and compare acquisition/gap
   metrics with `29772784252`.
3. Only if the symptom improved, dispatch one D032 run through
   `alpaca-smoke.yml` on the same branch with:
   `cover_run_id=<new successful refresh>`, `listing_run_id=29769888331`.
   Leave `lifecycle_run_id` empty on the first run; reuse that D032 run ID on
   later reruns so the lifecycle ZIP is immutable.
4. If the refresh fails, fix the first concrete code/data error, run Ruff plus
   the full test suite, commit/push, and rerun only after the symptom changes.
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
