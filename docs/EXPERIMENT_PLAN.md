# EXPERIMENT_PLAN — pre-registered protocol

This file IS the pre-registration. The grid, splits, metrics and pass/fail
criteria below are fixed **before** any experiment runs. Changing them after
seeing results requires a dated amendment section at the bottom of this file
explaining why — silent edits are a protocol violation.

## 1. Data windows & splits

- Fundamentals: FSDS 2009Q2+ (XBRL-complete from ~2011 for small caps).
- Prices: EODHD snapshot (delisted-inclusive, 2000+) as the backtest archive;
  Alpaca 2016+ as robustness cross-check.
- Effective backtest window: **2012-01 → present** (first ~1y consumed by
  momentum/TTM warm-up).
- **Purged holdout protocol** (static partition with embargo ≥ 1 rotation
  period at each boundary — this is NOT rolling-origin CV; `walkforward.py`
  implements this partition, plus optional rolling-origin diagnostics strictly
  INSIDE TRAIN):
  - TRAIN 2012-2018 — exploration allowed.
  - VALIDATION 2019-2022 — model selection (includes 2020 crash + 2021 froth
    + 2022 bear: the regime-overlay stress years).
  - TEST 2023-present — touched ONCE by the final configuration. A second look
    at TEST = the experiment is burned; say so in the amendment log.

## 2. Metrics (all net of costs)

CAGR; Sharpe; Sortino; MaxDD; Ulcer index; rolling-12m win rate vs benchmark;
one-way turnover; benchmark-relative alpha (Russell 2000 TR primary, S&P 500 TR
secondary); cost sensitivity at {0, 20, 40, 75}bp one-way.

**Alpha definition (pass/fail semantics):** annualized geometric excess return
of the strategy NAV over the benchmark TR series (simple excess, NOT
beta-adjusted regression alpha — with an overlay holding cash the two diverge;
regression alpha is additionally *reported* for context but never gates).

**"Steady returns" is operationalized as:** Ulcer index and MaxDD at or below
benchmark's, rolling-12m win rate ≥ 55%, no calendar year worse than
benchmark − 10pp.

## 3. The grid (pre-registered — THIS section is the single authoritative
grid; axis lists elsewhere are informative copies and yield to this one)

**Axes and cells:**

| Axis | Type | Cells (default bolded) |
|---|---|---|
| Holdings N | ordinal | 8, 10, 12, **15**, 20, 25 |
| Rotation | ordinal | 2w, **4w**, 6w, 13w |
| Buy/hold band | ordinal (none < quintile < quartile < top-third) | none, quintile, **quartile**, top-third |
| Sector cap (FF12, scaled to N) | ordinal | 3, **4**, 5 |
| Correlation filter | ordinal | off, **0.7** |
| Trailing stop | ordinal (+1 categorical cell) | none, 15%, **20%**, 25%, 30%, 3×ATR (ATR def: MODEL_SPEC §6) |
| Overlay | categorical | **O0 none**; O1 = SPY-TR 200d SMA, daily eval, 2% hysteresis, 100%→cash; O2 = SPY-TR 10-month SMA, last-session-of-month eval, 50%→cash; O3 = O1 + HY-OAS confirm (OAS > 500bp AND OAS > its 63-session MA) |
| Sector-relative ranks | binary | **off**, on |
| Factor weights | categorical | **equal**, quality-tilt (q .40/v .30/m .30), value-tilt (q .30/v .40/m .30), attribution-derived (Phase 5.2 output, registered by amendment before Stage 2 runs) |

Named robustness checks (run once on the chosen config, not grid axes):
Russell-2000-as-overlay-signal-index; ADV floor 0.5/2M; cap band edges;
±1 week rotation anchor phase.

**Search protocol (pre-registered — the full cross product is ~110k cells and
is NOT run):**

- **Stage 1 — one-axis-at-a-time screening (37 runs):** the all-default
  configuration (bolded cells) plus every single-axis deviation. Keep, per
  axis, the default and the best challenger (by VALIDATION net Sharpe subject
  to §2 constraints).
- **Stage 2 — local factorial (≤ 288 runs):** full cross of the surviving 2
  values per axis on the five highest-impact axes {N, rotation, band, stop,
  overlay} × surviving values of {factor weights} (≤ 3), with the remaining
  axes fixed at their Stage-1 winners. Total trials ≤ 37 + 288 = **325**;
  this count (actual runs executed) feeds the deflated-Sharpe computation.
- Stage boundaries are hard: no cell outside these two stages may be run
  against VALIDATION without a dated amendment.

**Interaction cells that MUST be reported** (redundant-layer check, inside
Stage 2): {overlay O1 × stop none}, {overlay O0 × stop 20%}, {overlay O1 ×
stop 20%}, {overlay O0 × stop none}.

## 4. Selection rule (anti-overfit)

1. Rank Stage-2 cells by VALIDATION net Sharpe subject to the steady-returns
   constraints (§2).
2. **Plateau rule (topology defined):** for **ordinal axes**, the neighbors of
   a cell are the cells at ±1 step on that axis (all other axes fixed); a cell
   is eligible only if the median of its ordinal neighbors is within 20% of
   its own score on every headline metric. **Categorical axes** (overlay,
   factor weights, sector-relative) are excluded from the neighbor median;
   instead the chosen categorical value must remain the best of its
   alternatives under the 40bp cost stress (stability across category).
   Choose the *center of the widest stable plateau*, never the single best
   cell.
3. Deflated-Sharpe / multiple-comparison sanity: compute with the actual trial
   count (≤325); expect the max cell to be partly noise; report alongside.
4. **Control cells (exact definitions):** C0 = all-default configuration;
   C1-C4 = the chosen configuration with exactly one layer ablated — C1:
   overlay→O0, C2: stop→none, C3: band→none, C4: weights→equal. The chosen
   configuration must beat C0 on VALIDATION net Sharpe AND Ulcer; if any
   ablation Cx matches it (within 5%), ship the simpler Cx instead
   (simplicity wins).
5. Run the final configuration on TEST exactly once. Report ALL metrics
   regardless of outcome.

## 5. Pass / fail criteria (fixed now)

**PASS (proceed to paper-forward):**
- TEST net alpha vs Russell 2000 TR ≥ +2%/yr, AND
- TEST MaxDD ≤ 1.0× benchmark MaxDD (≤ 0.7× if any overlay cell chosen), AND
- steady-returns constraints (§2) hold on TEST, AND
- results survive the 40bp cost cell (alpha ≥ 0 at 40bp).

**PRESUMED OVERFIT (mandatory re-examination, do not celebrate):**
- TEST net alpha > +6%/yr or net Sharpe > 1.3. Treat as a bug until three
  independent checks pass: (a) PIT leak audit (shuffle `available_from` +7d,
  alpha should degrade smoothly, not collapse), (b) per-year attribution shows
  no single year > 40% of total alpha, (c) top-20 contributing trades manually
  audited against as-of-date filings.

**FAIL:** anything else → document, do NOT tweak-and-rerun on TEST; go back to
TRAIN/VALIDATION with a dated amendment.

Anchors for these numbers: post-publication factor decay ~58% (McLean-Pontiff);
live value+momentum+trend funds by the researchers themselves delivered ~0
alpha (VMOT); credible disciplined-retail edge is +1-3%/yr, +2-4%/yr at the
optimistic end. A Sharpe near 1.0 net over a decade is elite; demand less.

## 6. Paper-forward window (after PASS)

- Freeze the full configuration (code SHA + config hash pinned in the ledger).
- Duration: ≥ 6 rotations (≥ 6 months at monthly cadence), spanning at least
  one earnings season.
- Judged against the same criteria (§2, §5) with slippage recorded (staged LOO
  vs assumed fill).
- Mid-window changes reset the clock. Capital decisions are out of scope for
  this repo (D009).

## 7. Amendment log

(append-only; date + what changed + why)
