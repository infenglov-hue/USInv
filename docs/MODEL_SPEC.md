# MODEL_SPEC — signals, gates, portfolio construction

Design targets **steady returns**: risk-adjusted consistency and drawdown
control are first-class objectives, not afterthoughts. Every default below is a
*blueprint hypothesis* — final values come from the pre-registered staged
static-holdout search (EXPERIMENT_PLAN.md), chosen on plateaus, never single
best cells.

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
| Active ATM dilution | S-3 shelf + ATM sales agreement/prospectus supplement + evidence of issuance/capacity use from later 8-K/10-Q equity roll-forward or repeated supplements/share-count change. A 424B5 can establish or amend capacity and is not proof of completed selling by itself. | exclude only on corroborated active issuance; otherwise penalize/unknown |
| Going concern | efts.sec.gov full-text: `"substantial doubt" "going concern"` in latest 10-K/10-Q (coverage 2001+, page by date slices — 10k result cap) | exclude |
| Listing-compliance risk | Exchange-specific rules, never a single "US rule": Nasdaq—minimum-bid deficiency after a reverse split in the prior year can remove the normal compliance period; Nasdaq also has a two-year cumulative 1-for-250 condition. NYSE American has distinct two-year cumulative-ratio and low-price rules. Operational heuristic: contemporaneous raw close < $1.50 plus any reverse split in 12m = exclude; any reverse split in 24m = penalty. The $1.50 threshold is our conservative risk policy, not exchange law. | exclude / penalize |
| Variable-price financing | Filing text/terms show an outstanding convertible or equity line whose conversion/purchase price is explicitly discounted to future market price, corroborated by a resale registration or subsequent issuance evidence | exclude while instrument is outstanding; unknown maturity/status = quarantine |
| Enforcement/pivot markers | Active SEC trading suspension = exclude. Two or more filing-time legal-name changes in 36 months = penalty. Unexplained price/volume spikes are diagnostic output only in v1; without a reliable promotion-data source they cannot gate selection. | exclude / penalize / diagnostic as stated |
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
| Large-cap extension | **0** maximum slots; slots are not reserved | {0, 3, 5} |
| Cash return | 0% in the primary backtest; BIL total-return sensitivity reported | fixed |
| Sector cap (FF12) | **27%**; integer cap = round-half-up(N×fraction), minimum 1 | {20%, 27%, 34%} (gives 3/4/5 when N=15) |
| Correlation filter | pairwise 252d return corr < 0.70 vs held names | {off, 0.7} |
| Rotation cadence | **monthly** (4-week), anchored Mondays; holiday/half-day shift rule in OPS_SPEC §1 | {biweekly, monthly, 6-weekly, quarterly} |
| Buy/hold band | enter: composite top decile; hold until out of top **quartile** | {none, quintile, quartile, top-third} |
| Turnover budget | rolling 21-session one-way traded notional ≤50% of pre-trade NAV; forced exits may exceed it but replacement buys wait | — |

Rationale anchors: idiosyncratic-risk knee at 15-25 names in modern studies
(5 names in US small caps = 12-20% portfolio hit per single blowup — BIST's 5
is NOT ported); factor half-lives ~3mo (momentum) to ~5mo (quality) make
monthly + banding capture nearly all decay at a fraction of biweekly turnover;
buy/hold banding is the single most effective cost-mitigation technique
(Novy-Marx & Velikov).

Market-cap architecture: compute factor percentiles independently inside the
core `$100M-$10B` bucket and the `>$10B` extension so scale/coverage differences
do not let mega-caps rewrite small-cap ranks. Merge candidates by their
within-bucket composite percentile, but admit no more than the configured
large-cap maximum. A large-cap slot is a ceiling, never a quota; weak large
names cannot displace stronger eligible core names merely to fill it.

**Deterministic selection algorithm** (reproducibility requirement): (1) apply
hygiene hard gates to both size buckets; (2) calculate and merge separately
ranked candidates; (3) evaluate held names first — a held
name stays if it satisfies the hold band and no exit fired; (4) rank remaining
candidates by composite; (5) walk the ranked list, admitting a candidate only
if it passes the entry band, would not breach the large-cap or sector cap, and
passes the correlation filter vs already-admitted + held names; (6) stop at N or
list-exhaustion (unfilled slots stay in cash); ties broken by higher
composite, then higher 21d dollar volume, then ticker lexicographic.

Turnover accounting includes buys and sells as separate one-way notional but
the 50% cap is applied to their average (`0.5 × (buys+sells) / pre-trade NAV`)
over the trailing 21 sessions. Forced safety exits always execute; if they
consume the budget, new entries remain cash until capacity returns. Initial
portfolio formation is reported separately and exempt from the ongoing cap.
Cash created by overlays, stops, unfilled orders or funding reserves earns zero
in the primary run. A BIL total-return proxy sensitivity shows the opportunity
without letting an assumed broker sweep drive model selection; paper-forward
uses the account's actual credited interest.

## 6. Exits

- **Trailing stop:** default 20-25% (grid {none, 15, 20, 25, 30, 3×ATR}),
  evaluated at EOD official close only, proceeds to cash until next rotation.
  Percent variant: on each effective split, position quantity, cost basis and
  stored high-water mark are transformed into the new share basis; then stop
  fires when contemporaneous official close < transformed high-water mark ×
  (1 − k). **ATR variant (exact definition):** ATR(14), Wilder smoothing,
  computed on an as-of split-continuous OHLC series anchored to that session,
  updated daily; stop fires when close < max(close since entry) − 3 ×
  ATR14(t). Future corporate actions never alter a past stop decision. Role:
  single-name catastrophe insurance between rotations. <15% is presumed
  value-destroying at 40%+ small-cap vol (whipsaw).
- **Thesis-break:** a red-flag hard gate firing on a held name (ATM cluster,
  going concern, delisting clock, trading suspension) ⇒ exit at next open.
- Band exit (§5) at rotation dates.
- Continuity: held positions live in one `selections` row (entry price = actual
  cost basis, never re-anchored at rotation) — BIST B1 lesson ported.

**Position termination (delisting handling in the backtest):** a price series
ending is evidence of missing data, not proof of a merger. Classification uses
exchange delisting notices/Form 25, issuer filings and frozen corporate-action
records; price shape may corroborate but never decide. Treatment:

- verified cash/stock acquisition: book the documented consideration and its
  effective date; if stock consideration cannot be valued, quarantine;
- announced exchange-to-OTC removal: model the first executable exit permitted
  by the T-1/T contract, then terminate because OTC is out of scope;
- bankruptcy/liquidation: use documented recovery; absent recovery data, the
  primary run uses −100% from the last valid marked value;
- unknown termination: primary run uses −100%, with final-close and −30%
  haircut sensitivities shown separately.

Every report lists terminations, evidence pointers and the effect of alternative
assumptions. Research-mode unresolved-action trades are additionally handled
per DATA_SPEC §4.2. This is intentionally more conservative than assuming every
untradeable name can be sold at its last print.

## 7. Execution contract (identical in backtest, paper-forward, live)

1. **Signal basis:** T-1 official closes (Nasdaq NOCP / NYSE auction close) +
   all fundamentals/macro with `available_from ≤ T-1 official session close`
   (16:00 ET; 13:00 ET half-days) — the unified availability rule of
   DATA_SPEC §1.3 / AGENTS.md rule 1.
2. **Decision:** pipeline runs Istanbul morning of T (all US data final by
   then); artifact published before US open.
3. **Fill:** T opening auction. Orders staged as **LOO (limit-on-open) with a
   collar**: default buy limit = T-1 official close ×1.05; sell limit = close
   ×0.95. This 5% is fixed for model selection and reported once at 3%/8%
   robustness; it is not a hidden grid axis. MOO is forbidden for routine
   small-cap rotation orders.
   Nasdaq on-open cutoff **09:28 ET** (= 16:28 TRT summer / 17:28 winter) is
   the system-wide deadline. Unfilled entry LOO is cancelled and its slot stays
   cash until the next rotation. An unfilled routine exit is retried at each of
   the next three eligible opens using 5% from the latest official close; if
   still unfilled, the run/operation raises a manual-execution exception rather
   than inventing a fill. A thesis-break/suspension exit uses a separately
   logged 10% sell collar and the same three-session exception rule. Backtest,
   paper and live use the same state machine.
   Historical daily `open` is only an auction-price proxy until validated
   against timestamped SIP/auction prints on a stratified 2016+ sample. The
   data manifest records `fill_quality=auction_verified|daily_open_proxy`;
   proxy-mode reports include ±25/50bp opening-price stress and cannot make an
   audit-grade execution claim.
4. **Costs inside the objective:** model selection and every headline result
   use **40bp one-way** (spread+impact) as the fixed baseline. Sensitivity
   {0, 20, 40, 75}bp is reported; no configuration may be selected at a cheaper
   cost and merely stressed afterward.
5. **Funding is broker/account specific.** Same-auction sales do not
   automatically make buys self-funding in a Regulation-T cash account; sale
   proceeds are unsettled and broker treatment varies. Configuration records
   account type and a settled-cash reserve. The default cash-account model
   sizes buys only from settled cash available before the auction; margin or a
   broker-confirmed unsettled-proceeds workflow requires an explicit override.
   Settlement is T+1 for most US equities, but corporate-action logic always
   consumes declared ex-dates rather than deriving them from record dates.

## 8. Benchmarks & reporting (D007)

Primary: **IWM total return, an investable Russell 2000 proxy** — from the
frozen historical snapshot pre-2016 and Alpaca `adjustment=all` thereafter
(DATA_SPEC §4.2). Secondary: **SPY total return, an investable S&P 500 proxy**,
same construction. Do not label either as the licensed index total-return
series; ETF fees and tracking differences are reported. (No third style-
control ETF in v1; BIL is used only for the cash-yield sensitivity.) Every
performance surface reports:
net-of-cost NAV, benchmark-relative, MaxDD, Ulcer, rolling-12m win rate —
never a lone CAGR. USD nominal is the base currency; TRY/gold translation is a
display layer only (personal context), never a decision input.
