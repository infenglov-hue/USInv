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
| morning | — | user may review at leisure; production does not wait for a login or approval |
| ~13:17 / retry ~14:43 | 06:17 / retry 07:43 | daily decision job; on rotation days create paper orders or, after the explicit live-capital enablement gate, submit broker LOO orders |
| ≤16:28 | ≤09:28 | automated preflight confirms fresh evidence, settled funding, collars and broker acknowledgements; otherwise no new buy is sent |
| 16:30 | 09:30 | opening auction fills |
| ~16:47 / retry ~17:13 | 09:47 / retry 10:13 | fill reconciliation updates the canonical remote ledger and publishes delivery |

The whole design needs **no real-time market data** (LOO auction orders need no
live quote) — that is deliberate and load-bearing for the $0 budget.

## 3. GitHub Actions workflows

GitHub Actions supports timezone-aware schedules, so every production cron
sets `timezone: "America/New_York"` directly. Cron is still not an SLA: high
load can delay or drop a scheduled run. Each critical operation therefore has
a primary time and an off-the-hour retry. Both acquire the same durable
`(operation, business_date, mode)` idempotency key; the retry exits quickly if
the primary already committed. A monitor outside GitHub receives success
heartbeats and can alert or call `workflow_dispatch` when both triggers miss.

| Workflow | Schedule (ET, timezone-aware) | Job |
|---|---|---|
| `ci.yml` | on PR | ruff + pytest + artifact-guard |
| `nightly-data.yml` | 01:17 ET; retry 02:43 ET | the §2 pipeline; red on any freshness/coverage gate; heartbeat on committed success |
| `decision.yml` | sessions 06:17 ET; retry 07:43 ET | daily exit/preflight; rotation-day selection; paper order write or explicitly enabled live LOO submission |
| `fill-reconcile.yml` | sessions 09:47 ET; retry 10:13 ET | broker/paper fill reconciliation, ledger commit, snapshot publish and notify |
| `weekly-audit.yml` | weekend | full metrics audit artifact, cost stresses, coverage report |
| `fsds-refresh.yml` | quarterly + dispatch | new FSDS drop ingest, edge-vs-spine mismatch report |

Every workflow uses a concurrency group and a hard timeout. Data and broker
writes are transactional; broker requests carry deterministic client-order
IDs. Retries must be safe after failure at every step. Historical experiments
and multi-GB restores do not run in scheduled production Actions.

## 4. Repos & artifacts

- `USInv` (this repo): code + blueprint + small reference data. Public or
  private — user's call.
- Historical/licensed state: separately controlled storage plus a tested backup
  target. A Git repository stores only manifests, schemas, hashes and small
  open reference files—not EODHD payloads, multi-GB Parquet or mutable DuckDB
  binaries. Optional object storage is a separate user decision with cost,
  encryption, retention and license review. Git LFS is not assumed free or
  suitable.
- Scheduled runtime state: a compact canonical ledger/database plus object
  pointers in durable remote storage. A fresh GitHub-hosted runner must be able
  to restore, transact and publish without the developer PC. Actions artifacts
  are delivery/debug outputs, never the only state copy. Restore/upload size
  and duration are measured before enabling cron.
- Delivery artifact: `snapshot.json` (versioned schema, documented in
  `delivery/snapshot.py`): as-of dates, universe stats, top-N with per-factor
  ranks + red-flag status + entry references, held positions with stops,
  regime state, NAV series tail, freshness/coverage health block, config hash.
  Published to an independent installable, responsive `web/` PWA that reads
  `snapshot.json` but performs no portfolio calculation of its own. The PWA is
  the primary product surface: portfolio/cash state, per-name factor reasons,
  regime, performance, order status and freshness/coverage evidence. It keeps
  the last verified snapshot available offline with a prominent stale banner.
  It contains derived signals and small display tails only; it never
  republishes licensed bulk price/vendor data or broker secrets. Delivery is
  private/authenticated by default; any public deployment requires a redacted
  snapshot contract and license review.
- Secrets: `ALPACA_KEY_ID/SECRET`, `TIINGO_TOKEN`, `FRED_API_KEY`, remote-state
  credentials and `TELEGRAM_BOT_TOKEN/CHAT_ID` in repository/organization
  Actions secrets. GitHub Free private repositories cannot rely on environment
  secrets. Provider accounts/tokens,
  the EDGAR contact address, historical-data mode, backup target and broker
  account/funding model are explicit user decisions before their first
  dependent phase—not deferred to Phase 5.

## 5. Monitoring

- Telegram bot (Phase 6.3): rotation summary, exit alerts (stop/thesis-break
  fired), red health lines (freshness gate, job failure, stale artifact).
- Independent heartbeat monitor: every critical committed stage pings success;
  a missed primary+retry window alerts without depending on GitHub Actions to
  notice its own absence. Optional external dispatch uses a least-privilege
  token and invokes the same idempotent workflow.
- Paper-forward ledger integrity: weekly job re-verifies NAV continuity and
  that the frozen config hash hasn't drifted (any drift = loud alert).
- Every published number carries its config hash + code SHA (reproducibility).
