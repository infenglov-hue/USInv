# Phase 7 — PWA Operator Console (Plan)

> Status: **PLANNED — not implemented.** Added 2026-09-06 per user request
> "Onu da ekle planlara sonra yaparız". No code in this PR, only the plan.
> Execution is deferred until D032 is closed (Phase 2.3 gate) and Phase 6
> unattended pipeline is green. This keeps AGENTS.md rule #7 (gates) intact.

## 0. Why a separate PWA

CODEX_TASKS Phase 6.3 already scopes a mobile-first PWA (portfolio + ranked
candidates + macro + order/fill + data health + offline snapshot). That scope
assumed the unattended pipeline and `snapshot.json` contract. This Phase 7
plan extends it into a standalone **Operator Console** — the same backend but
with a richer UI that can be installed on phone/desktop and used when the
user is away from the local machine. It must:

- share **no runtime or source imports** with MobileInv (AGENTS.md D012),
- never recompute selection in the browser (reads `snapshot.json` only),
- never expose licensed bulk, provider secrets or broker credentials,
- work unattended (GitHub Actions is the only production runner).

## 1. Scope (what it shows, what it never does)

Shows:
- current portfolio + cash + pending orders/fills
- ranked long candidates with factor reasons (value/quality/momentum, sector-relative, Piotroski veto)
- regime overlay state and vintage macro pointers (NFCI/SAHM/HY OAS)
- performance vs IWM_TR / SPY_TR (net of costs, with ulcer/MaxDD)
- data health (freshness, coverage, stale-artifact kill switch)
- offline last-verified snapshot + explicit stale mode banner

Never:
- recomputes scoring, selection or regime in the browser
- calls Alpaca/Tiingo/FRED/EDGAR directly from the PWA
- writes to the ledger except via the deterministic client-order-ID adapter owned by the pipeline

## 2. Architecture

```
GitHub Actions (nightly-data → decision → fill-reconcile)
      │  snapshot.json (versioned contract, hash-pinned)
      ▼
  Artifact store (content-addressed, no secrets)
      │  delivery adapter validates contract, strips licensed fields
      ▼
  Static hosting (GitHub Pages / Cloudflare Pages)
      │
  PWA (SvelteKit + Vite PWA plugin)
      ├── Service Worker (offline-first, stale-while-revalidate)
      ├── IndexedDB (last N snapshots, holdings cache)
      ├── Web Push (rotation / kill-switch alerts via Telegram bridge)
      └── App Shell (mobile-first, installable, 100 Lighthouse PWA score)
```

Backend wrapper (thin):
- `usinv/api/` — FastAPI app that serves ONLY the `snapshot.json` contract
  and ledger reads. No scoring/selection imports in the route handlers;
  handlers are validated by the same golden contract tests as Phase 6.2.
- No database — reads Parquet/DuckDB snapshots produced by the pipeline.

Why SvelteKit:
- smaller bundle than React for a data-heavy list view,
- file-based routing matches the `snapshot.json` versioning,
- Vite PWA plugin gives a one-line `VitePWA({ registerType: 'autoUpdate' })`.

Alternatives considered and deferred:
- SolidStart — similar but smaller ecosystem,
- Next.js — heavier, unnecessary for a static-first console.

## 3. API contract (extends Phase 6.2 `snapshot.json`)

`GET /api/snapshot/latest` → `{ version, as_of, config_hash, code_sha, data_manifest_hash, output_hash, portfolio, candidates, regime, health }`
`GET /api/snapshot/:asOf` → same shape for a historical asOf
`GET /api/ledger/fills?since=` → fills with deterministic client_order_id
`GET /api/health` → freshness, coverage, kill-switch state

Contract tests (must pass before any UI ships):
- PWA cannot infer selection (remove `portfolio` → UI shows empty, no fallback ranking)
- no licensed field leaks into `/api/*` (grep `alpaca`/`tiingo`/`fred` in response)
- offline snapshot renders without network (Playwright offline drill)
- stale-mode banner appears when `as_of` > kill-switch threshold

## 4. PWA manifest + offline

`manifest.json`: `name: USInv Console`, `display: standalone`, `theme_color: #0a0a0a`,
`icons: 192/512`, `shortcuts: [Portfolio, Candidates, Health]`.
Service worker: precaches shell + last snapshot; runtime caches `/api/snapshot/*`
with `StaleWhileRevalidate(24h)`; shows "Stale — last verified <date>" when offline
or when `health.stale == true`.

## 5. Phasing (small PRs, gated)

- **7.1** API wrapper + contract tests (no UI). Gate: `pytest -q` + contract golden tests green.
- **7.2** App shell + manifest + service worker (empty pages, installable). Gate: Lighthouse PWA ≥ 90, Playwright offline test green.
- **7.3** Portfolio + Candidates pages (read-only, factor reasons, regime badge). Gate: visual diff vs `snapshot.json` fixture.
- **7.4** Health + performance + push (Telegram bridge, weekly audit link). Gate: kill-switch drill shows stale banner; push arrives on rotation.
- **7.5** Hardening: a11y, i18n (TR/EN), e2e on real device. Gate: five consecutive nightly artifacts render correctly on phone.

Each PR updates `docs/PROGRESS.md` with SHA + verification, per AGENTS.md.

## 6. Non-goals / deferred

- Intraday signals, options, short side, FPIs/ADRs — out of scope per CODEX_TASKS.
- Reusing MobileInv runtime — forbidden (D012).
- Any new scoring/overlay logic — Phase 7 is presentation only.

## 7. Open questions (to resolve before 7.1)

- Hosting: GitHub Pages (simpler, same repo) vs Cloudflare Pages (better edge cache)?
- Push provider: Telegram bot already in Phase 6.4 — reuse it vs add Web Push via VAPID?
- Auth: PWA is read-only and contains no secrets, but should `/api/*` be behind a simple bearer token? DOCS decision needed.

## 8. Relation to current build

Current branch `main @d285b1a` (9 ahead origin/main) — D032 is still **BLOCKED**
(`phase_2_3_gate_blocked`, identity 387 / sector 257 / mandatory 6). Phase 7
must not start until D032 closes and Phase 6 pipeline is green (AGENTS.md #7).
This document is the only Phase 7 artifact in this PR.
