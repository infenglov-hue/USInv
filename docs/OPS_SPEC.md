# OPS_SPEC — schedules, CI/CD, delivery, monitoring

## 1. Time model

- Exchange tz: `America/New_York` (ET, DST-observing). Operator tz: Istanbul
  (UTC+3, **no DST since 2016**) — the ET↔TRT offset changes twice a year
  (2nd Sun of March, 1st Sun of Nov). **Every schedule pins to ET**; a cron
  pinned to Istanbul time silently shifts 1h twice a year.
- Key instants: US open 09:30 ET = 16:30 TRT (summer) / 17:30 TRT (winter);
  close 16:00 ET = 23:00/00:00 TRT; **Nasdaq on-open order cutoff 09:28 ET =
  16:28/17:28 TRT** — the system-wide daily deadline.
- Calendar: XNYS via `exchange_calendars`/`pandas_market_calendars`. Traps that
  MUST have tests: Good Friday (market holiday, not federal), weekend-observed
  July 4th (e.g. full closure Fri 2026-07-03), 13:00 ET half-days (day after
  Thanksgiving, Christmas Eve, some July 3rds; closing auction at 13:00),
  Juneteenth only since 2022. Rule: never schedule a rotation whose signal or
  fill date is a half-day.
- **Rotation shift rule (load-bearing for `calendar.py`):** the rotation
  anchor sequence (every 4 weeks from the first anchor Monday) is fixed in
  advance and **never re-bases**. If a scheduled rotation Monday is a holiday,
  or its fill session or T-1 signal session is a half-day, execution shifts to
  the **next eligible full session** (its T-1 full session becomes the signal
  date); the following rotation still occurs on the original anchor sequence.
  Concrete cases that must be unit-tested: MLK Monday (shift to Tuesday),
  Thanksgiving week (Friday half-day ⇒ a Monday rotation's Friday signal date
  is a half-day only when Thanksgiving shifts the calendar — encode the
  general rule, not the instance), Memorial/Labor Day Mondays.

## 2. Daily operational timeline (Istanbul-friendly by construction)

| TRT (summer) | ET | What |
|---|---|---|
| ~03:00 | 20:00 T-1 | US session + after-hours done; vendor EOD final |
| 08:00 | 01:00 | **nightly-data job**: EDGAR submissions delta → companyfacts edge → Alpaca bars (end ≤ now−15min ⇒ SIP) → actions reconcile → macro pulls → freshness gates |
| 09:00 | 02:00 | scoring + (on rotation days) selection; snapshot artifact published; Telegram summary |
| morning | — | user reviews at leisure — no overnight work, ever |
| ≤16:28 | ≤09:28 | user stages LOO orders at broker (manual; signals-only system) |
| 16:30 | 09:30 | opening auction fills; ledger records assumed vs (optional) actual fill |

The whole design needs **no real-time market data** (LOO auction orders need no
live quote) — that is deliberate and load-bearing for the $0 budget.

## 3. GitHub Actions workflows

GitHub cron is UTC-fixed while ET observes DST — a single cron drifts 1h
against the exchange twice a year (the §1 trap). Mitigation: schedule each
job at BOTH candidate UTC hours; the job's first step computes current ET and
exits 0 immediately if it is the wrong instance.

| Workflow | Schedule (cron in UTC, dual-hour + ET guard) | Job |
|---|---|---|
| `ci.yml` | on PR | ruff + pytest + artifact-guard |
| `nightly-data.yml` | nightly ~01:00 ET | the §2 pipeline; red on any freshness/coverage gate |
| `rotation.yml` | rotation Mondays ~02:00 ET (+ manual dispatch) | selection + snapshot publish + notify |
| `weekly-audit.yml` | weekend | full metrics audit artifact, cost stresses, coverage report |
| `fsds-refresh.yml` | quarterly + dispatch | new FSDS drop ingest, edge-vs-spine mismatch report |

Lessons from MobileInv encoded: GitHub cron is not an SLA (jobs may start late
or never — add a next-morning retry and a "stale artifact" banner rather than
pretending); state restore must be size-budgeted (keep runtime DB small, bulk
archives in Releases/state repo, checksum-split if >2GB); every workflow ends
with a machine-readable health line consumed by the monitor.

## 4. Repos & artifacts

- `USInv` (this repo): code + blueprint + small reference data. Public or
  private — user's call.
- `USInv-state` (private, created in Phase 1): DuckDB/Parquet archives, FSDS
  zip mirror, EODHD snapshot. Git LFS or chunked+checksummed files.
- Delivery artifact: `snapshot.json` (versioned schema, documented in
  `delivery/snapshot.py`): as-of dates, universe stats, top-N with per-factor
  ranks + red-flag status + entry references, held positions with stops,
  regime state, NAV series tail, freshness/coverage health block, config hash.
  Published via gh-pages (or Releases) + a minimal static `web/` viewer
  (plain HTML/JS reading snapshot.json — explicitly NOT the MobileInv PWA;
  can be pretty later, correct first).
- Secrets: `ALPACA_KEY_ID/SECRET`, `TIINGO_TOKEN`, `FRED_API_KEY`,
  `TELEGRAM_BOT_TOKEN/CHAT_ID` in Actions secrets.

## 5. Monitoring

- Telegram bot (Phase 7): rotation summary, exit alerts (stop/thesis-break
  fired), red health lines (freshness gate, job failure, stale artifact).
- Paper-forward ledger integrity: weekly job re-verifies NAV continuity and
  that the frozen config hash hasn't drifted (any drift = loud alert).
- Every published number carries its config hash + code SHA (reproducibility).
