# MODEL_SPEC — signals, gates, portfolio construction

Design targets **steady returns**: risk-adjusted consistency and drawdown
control are first-class objectives, not afterthoughts. Every default below is a
*blueprint hypothesis* — final values come from the pre-registered walk-forward
grid (EXPERIMENT_PLAN.md), chosen on plateaus, never single best cells.

## 1. Factor menu (evidence-informed, US small/mid-cap)

| Sleeve | Definition (v1) | Role | Evidence note |
|---|---|---|---|
| Quality/profitability | gross profitability (GP/Assets), ROIC, accruals (negative), FCF conversion | core ranking | quality kept paying ~1%/yr in US small caps 2011-2026 (MSCI SC Quality) |
| Value | composite: EBIT/TEV, FCF yield, earnings yield — multi-ratio, NOT P/B alone | core ranking | multi-ratio composites degrade less than single ratios post-publication |
| Momentum | 12-1 (skip most recent month), computed on adjusted total-return series | core ranking; binding constraint on rebalance frequency | robust gross; survives costs only with buy/hold banding |
| Piotroski F-score | 9-point | **junk VETO only** (exclude F ≤ 4), never a primary rank | decayed as primary signal post-publication; still works as junk screen |
| Low-volatility | — | **EXCLUDED as return factor** (risk tie-breaker at most) | lowest-beta US quintile ≈ −4.5%/yr vs market since 2017; crowded, premium gone |

Composite: cross-sectional percentile ranks per sleeve → weighted sum
(weights in `factors.yaml`; starting point equal-ish quality/value/momentum,
re-derived from US attribution in Phase 5 — **BIST weights are NOT ported**).
Sector-relative ranking within FF12 groups is a grid cell (on/off).
Expectation haircut: assume 30-60% decay vs any backtested factor spread.

## 2. Scope carve-outs (v1)

- **Financials** (banks SIC 6020-6036, brokers 6199/6211, insurers 6311-6399):
  excluded from the generic composite (leverage/EV/accrual factors are
  meaningless for them). v2 may add dedicated models (P/TBV, ROTE, NIM) —
  mirrors the BIST bank/holding/REIT pattern.
- **REITs** (SIC 6798): excluded v1 (GAAP EPS/BV distorted; needs FFO/AFFO).
- **Pre-revenue biotech** (SIC 2834/2836/8731 with TTM revenue < $10M):
  excluded — value/profitability undefined; drivers are runway + dilution.
- **FPIs/ADRs** (20-F/6-K filers): excluded — no 10-Qs, fundamentals 6-10
  months stale vs domestic filers; also removes most China VIE/auditor risk
  (the HFCAA issuer list is currently empty and cannot serve as a gate).
- **Utilities** (4900-4949): included v1, watch as a diagnostic group.

## 3. Red-flag HARD gates (selection-blocking, not cosmetic)

BIST lesson (D011): a flag that doesn't gate is not a feature. All gates are
computed from free, keyless EDGAR endpoints and logged with evidence pointers.

| Gate | Detection | Rule |
|---|---|---|
| Shell / blank check | SIC 6770 **now or ever** (SEC reclassifies after de-SPAC — check filing history), `dei:EntityShellCompany = true` on latest 10-K/10-Q cover page | exclude |
| Active ATM dilution | S-3 shelf → 8-K "Sales Agreement"/ATM (agents: Cantor, Jefferies, H.C. Wainwright…) → **cluster of 424B5/424B3 in trailing 90d**; a 424B5 RAISING the program = capacity exhausted (worst) | exclude on active cluster; score otherwise |
| Going concern | efts.sec.gov full-text: `"substantial doubt" "going concern"` in latest 10-K/10-Q (coverage 2001+, page by date slices — 10k result cap) | exclude |
| Delisting clock | reverse split in trailing 12m AND price < $1.50 (Jan-2025 Nasdaq/NYSE rules: sub-$1 after a 1-yr-recent reverse split ⇒ immediate delisting determination); any reverse split in trailing 24m = penalty | exclude / penalize |
| Death-spiral financing | S-1 resale registration shortly after PIPE/convertible in a microcap | exclude |
| Fraud markers | SEC trading-suspension history; frequent name/business pivots (`formerNames`); promo spikes without filings | exclude / penalize |
| Data integrity | share-count jump >50% without detected split; standardization coverage failure; stale fundamentals past expected+grace | quarantine (not scored) |

## 4. Regime layer

Two mechanisms, tested independently and jointly in the grid:

1. **Exposure overlay (drawdown control):** exact cell definitions live in
   EXPERIMENT_PLAN §3 (the single authoritative grid) — O1: S&P 500 (SPY TR)
   200-day SMA, daily EOD evaluation with 2% hysteresis, 100%→cash; O2:
   S&P 500 10-month SMA, last-session-of-month evaluation, 50%→cash; O3: O1
   gated by HY-OAS confirmation (OAS > 500bp AND OAS > its 63-session MA).
   Russell 2000 as alternative signal index is a named robustness check on the
   chosen cell, not a grid axis. Strongest OOS evidence of all overlays
   (halved MaxDD historically); accepted cost: multi-year lag in uninterrupted
   bull markets.
2. **Regime-conditional factor weights:** in high-vol / below-trend states,
   de-weight momentum (momentum crashes cluster in rebounds — Daniel-Moskowitz;
   Barroso-Santa-Clara vol-scaling). NFCI > 0 (tighter than average) and Sahm-
   rule trigger shade gross exposure / tighten quality gates only (slow gates).
   **Volatility targeting is NOT adopted by default** (failed real-time
   replication across 103 strategies — Cederburg et al. 2020); it may appear in
   the grid as a challenger only.

PIT discipline for regime inputs: NFCI via ALFRED vintages, SAHMREALTIME not
SAHMCURRENT, HY OAS archived + HYG/LQD fallback (DATA_SPEC §7). Overlay and
stops partially duplicate each other — the grid explicitly contains the
{overlay ON × stops OFF} and {overlay OFF × stops ON} cells.

## 5. Portfolio construction

| Parameter | Default (hypothesis) | Grid |
|---|---|---|
| Holdings | **15**, equal weight | {8, 10, 12, 15, 20, 25} |
| Position cap | 1/N at entry; no rebalancing between rotations | — |
| Sector cap (FF12) | max 4 of 15 (~25%) | {3, 4, 5} |
| Correlation filter | pairwise 252d return corr < 0.70 vs held names | {off, 0.7} |
| Rotation cadence | **monthly** (4-week), anchored Mondays; holiday/half-day shift rule in OPS_SPEC §1 | {biweekly, monthly, 6-weekly, quarterly} |
| Buy/hold band | enter: composite top decile; hold until out of top **quartile** | {none, quintile, quartile, top-third} |
| Turnover budget | one-way < 50%/month (hard cap in selector) | — |

Rationale anchors: idiosyncratic-risk knee at 15-25 names in modern studies
(5 names in US small caps = 12-20% portfolio hit per single blowup — BIST's 5
is NOT ported); factor half-lives ~3mo (momentum) to ~5mo (quality) make
monthly + banding capture nearly all decay at a fraction of biweekly turnover;
buy/hold banding is the single most effective cost-mitigation technique
(Novy-Marx & Velikov).

**Deterministic selection algorithm** (reproducibility requirement): (1) apply
hygiene hard gates to the universe; (2) evaluate held names first — a held
name stays if it satisfies the hold band and no exit fired; (3) rank remaining
candidates by composite; (4) walk the ranked list, admitting a candidate only
if it passes the entry band, would not breach the sector cap, and passes the
correlation filter vs already-admitted + held names; (5) stop at N or
list-exhaustion (unfilled slots stay in cash); ties broken by higher
composite, then higher 21d dollar volume, then ticker lexicographic.

## 6. Exits

- **Trailing stop:** default 20-25% (grid {none, 15, 20, 25, 30, 3×ATR}),
  evaluated at EOD official close only, proceeds to cash until next rotation.
  Percent variant: stop fires when split-adjusted official close <
  max(split-adjusted close since entry) × (1 − k). **ATR variant (exact
  definition):** ATR(14), Wilder smoothing, computed on split-adjusted OHLC,
  updated daily; stop fires when close < max(close since entry) − 3 ×
  ATR14(t). Role: single-name catastrophe insurance between rotations. <15%
  is presumed value-destroying at 40%+ small-cap vol (whipsaw).
- **Thesis-break:** a red-flag hard gate firing on a held name (ATM cluster,
  going concern, delisting clock, trading suspension) ⇒ exit at next open.
- Band exit (§5) at rotation dates.
- Continuity: held positions live in one `selections` row (entry price = actual
  cost basis, never re-anchored at rotation) — BIST B1 lesson ported.

**Position termination (delisting handling in the backtest — load-bearing for
a delisted-inclusive design):** when a held name's price series ends:
(a) detectable acquisition/merger (price plateaus into the final print, or an
8-K/DEFM14A in the filing index) ⇒ exit at the final available official close;
(b) involuntary delisting (bankruptcy, exchange removal, move to OTC — OTC
counts as terminated since we do not trade OTC) ⇒ exit at the final available
official close **× 0.70** (pre-registered −30% haircut for the untradeable
gap); (c) every backtest report lists terminated positions and shows the
{0%, −30%, −100%} haircut sensitivity. The classifier defaults to
"involuntary" when ambiguous (conservative).

## 7. Execution contract (identical in backtest, paper-forward, live)

1. **Signal basis:** T-1 official closes (Nasdaq NOCP / NYSE auction close) +
   all fundamentals/macro with `available_from ≤ T-1 official session close`
   (16:00 ET; 13:00 ET half-days) — the unified availability rule of
   DATA_SPEC §1.3 / AGENTS.md rule 1.
2. **Decision:** pipeline runs Istanbul morning of T (all US data final by
   then); artifact published before US open.
3. **Fill:** T opening auction. Orders staged as **LOO (limit-on-open) with a
   collar** of T-1 close ±3-5% (MOO forbidden in small caps — auction gaps).
   Nasdaq on-open cutoff **09:28 ET** (= 16:28 TRT summer / 17:28 winter) is
   the system-wide deadline. Unfilled LOO (gapped beyond collar) ⇒ skip or
   retry next day per config — backtest models the same rule (no free fills).
4. **Costs inside the objective:** 20-40bp one-way (spread+impact) in every
   backtest/grid cell; cost sensitivity {0, 20, 40, 75}bp reported in audits.
5. T+1 settlement (post 2024-05-28): same-auction sell/buy rotation is
   self-funding in a cash account; ex-date = record date in adjustment logic.

## 8. Benchmarks & reporting (D007)

Primary: Russell 2000 total return, constructed as the IWM dividend-adjusted
(TR) series — from the EODHD snapshot pre-2016, Alpaca `adjustment=all`
thereafter (DATA_SPEC §4.2). Secondary: S&P 500 TR via SPY, same construction.
(No third style-control ETF in v1.) Every performance surface reports:
net-of-cost NAV, benchmark-relative, MaxDD, Ulcer, rolling-12m win rate —
never a lone CAGR. USD nominal is the base currency; TRY/gold translation is a
display layer only (personal context), never a decision input.
