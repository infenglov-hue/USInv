# EXPERIMENT_PLAN — pre-registered protocol

This file IS the pre-registration. The grid, splits, metrics and pass/fail
criteria below are fixed **before** any experiment runs. Changing them after
seeing results requires a dated amendment section at the bottom of this file
explaining why — silent edits are a protocol violation. No real experiment may
run until the historical-data mode and frozen input manifest described in
DATA_SPEC §4.2 have been approved and hashed.

## 1. Data windows & splits

- Fundamentals: FSDS 2009Q2+ (XBRL-complete from ~2011 for small caps).
- Prices: EODHD snapshot (delisted-inclusive, 2000+) as the backtest archive;
  Alpaca 2016+ as robustness cross-check.
- Effective backtest window: **2012-01 → present** (first ~1y consumed by
  momentum/TTM warm-up).
- **Purged static holdout protocol** (embargo = the maximum configured holding
  horizon at each boundary; never merely one rotation). This is deliberately
  NOT called walk-forward validation. `splits.py` implements the locked
  partition; optional rolling-origin diagnostics are allowed strictly inside
  TRAIN and cannot select the final configuration:
  - TRAIN 2012-2018 — exploration allowed.
  - VALIDATION 2019-2022 — model selection (includes 2020 crash + 2021 froth
    + 2022 bear: the regime-overlay stress years).
  - TEST **2023-01-03 through 2026-06-30** — touched ONCE by the registered
    final configuration. The end date is frozen rather than moving with
    "present". A second look at TEST = the experiment is burned; say so in the
    amendment log and create a new future holdout instead of pretending.

Before any run, commit a `data_manifest.json` containing source/batch hashes,
coverage mode (`research` or `audit`), unresolved-action counts, unmapped-
security counts, and the exact first/last session. The TEST runner refuses to
run unless (a) a final config hash has been registered, (b) TRAIN/VALIDATION
outputs are sealed, and (c) a one-time user-provided unlock token is present.

## 2. Metrics (all net of costs)

CAGR; Sharpe; Sortino; MaxDD; Ulcer index; rolling-12m win rate vs benchmark;
one-way turnover; benchmark-relative alpha (IWM total-return proxy primary, SPY
total-return proxy secondary). Headline/model-selection cost is fixed at 40bp
one-way; sensitivity at {0, 20, 40, 75}bp is also reported.

**Alpha definition (pass/fail semantics):** annualized geometric excess return
of the strategy NAV over the benchmark TR series (simple excess, NOT
beta-adjusted regression alpha — with an overlay holding cash the two diverge;
regression alpha is additionally *reported* for context but never gates).

**"Steady returns" is operationalized as:** Ulcer index and MaxDD at or below
benchmark's, rolling-12m win rate ≥ 55%, no calendar year worse than
benchmark − 10pp.

Rolling-12m win rate is sampled at month-end (one observation per month), not
daily. Because those windows overlap, it is descriptive and is never treated
as a set of independent Bernoulli trials. Report stationary-block-bootstrap
80% and 95% confidence intervals for annualized active return and Sharpe using
monthly returns; confidence intervals inform the verdict but do not replace
the fixed gates in §5.

## 3. The grid (pre-registered — THIS section is the single authoritative
grid; axis lists elsewhere are informative copies and yield to this one)

**Axes and cells:**

| Axis | Type | Cells (default bolded) |
|---|---|---|
| Holdings N | ordinal | 8, 10, 12, **15**, 20, 25 |
| Large-cap extension | categorical | **S0 = 0 max slots**, S3 = 3 max slots, S5 = 5 max slots; separately ranked, never forced |
| Rotation | ordinal | 2w, **4w**, 6w, 13w |
| Buy/hold band | ordinal (none < quintile < quartile < top-third) | none, quintile, **quartile**, top-third |
| Sector cap (FF12 fraction) | ordinal | 20%, **27%**, 34%; integer cap = round-half-up(N×fraction), minimum 1 |
| Correlation filter | ordinal | off, **0.7** |
| Trailing stop | ordinal (+1 categorical cell) | none, 15%, **20%**, 25%, 30%, 3×ATR (ATR def: MODEL_SPEC §6) |
| Overlay | categorical | **O0 none**; O1 = SPY-TR 200d SMA, daily eval, 2% hysteresis, 100%→cash; O2 = SPY-TR 10-month SMA, last-session-of-month eval, 50%→cash; O3 = O1 + HY-OAS confirm (OAS > 500bp AND OAS > its 63-session MA) |
| Sector-relative ranks | binary | **off**, on |
| Factor weights | categorical | **equal**, quality-tilt (q .40/v .30/m .30), value-tilt (q .30/v .40/m .30), attribution-derived (Phase 5.2 output, registered by amendment before Stage 2 runs) |

Named robustness checks (run once on the chosen config, not grid axes):
Russell-2000-as-overlay-signal-index; ADV floor 0.5/2M; cap band edges;
±1 week rotation anchor phase.

**Search protocol (pre-registered — the full cross product is exactly 331,776
cells and is NOT run):**

- **Stage 1 — one-axis-at-a-time screening (29 runs):** the all-default
  configuration (bolded cells) plus every single-axis deviation. Keep, per
  axis, the default and the best challenger (by VALIDATION net Sharpe subject
  to §2 constraints). Exception: factor weights retain equal, the best fixed
  tilt, and the attribution-derived candidate when distinct (≤3 values). The
  arithmetic is `1 + Σ(cells_on_axis − 1) = 29`.
- **Stage 2 — reduced factorial (≤ 96 runs):** full cross of the surviving 2
  values per axis on the five highest-impact axes {N, rotation, band, stop,
  overlay} × surviving values of {factor weights} (≤ 3), with the remaining
  axes fixed at their Stage-1 winners. Arithmetic: `2^5 × 3 = 96`.
- **Stage 3 — finalist neighborhood audit (≤ 60 new runs):** take at most the
  top three eligible Stage-2 cells. With all other coordinates fixed, run
  every missing immediate neighbor on the ORIGINAL ordinal axes {N, rotation,
  band, sector cap, stop} and every alternative value on the categorical axes
  {large-cap extension, correlation filter, overlay, sector-relative ranks,
  factor weights}. There are at most 20 deviations per finalist; deduplicate
  identical cells. This stage therefore has **≤60 new runs**.
- **Controls (≤ 5 new runs):** C0-C4 of §4, again deduplicated.

The planned maximum is therefore **190 distinct runs** (29 + 96 + 60 + 5),
usually fewer after deduplication. The manifest records the actual unique
configuration count; that actual count, not 190, feeds deflated Sharpe.
- Stage boundaries are hard: no cell outside these three stages may be run
  against VALIDATION without a dated amendment.

**Interaction cells that MUST be reported** (redundant-layer check, inside
Stage 2): {overlay O1 × stop none}, {overlay O0 × stop 20%}, {overlay O1 ×
stop 20%}, {overlay O0 × stop none}.

## 4. Selection rule (anti-overfit)

1. Rank Stage-2 cells by VALIDATION net Sharpe subject to the steady-returns
   constraints (§2).
2. **Plateau rule (implementable topology):** Stage 3 supplies every neighbor
   used by this rule. An ordinal neighbor differs by exactly ±1 step on one
   ORIGINAL grid axis, all other coordinates fixed. A finalist is stable only
   when the median of the available ordinal neighbors satisfies all of:
   net Sharpe ≥ finalist Sharpe − 0.15; annualized net alpha ≥ finalist alpha
   − 1.0 percentage point; Ulcer ≤ 1.20× finalist Ulcer; and MaxDD magnitude ≤
   finalist magnitude + 5 percentage points. At an axis edge the one available
   neighbor is used and the edge status is reported. For categorical axes,
   all alternatives are run with other coordinates fixed; the chosen value
   must remain best on net Sharpe at 40bp, or be within 0.05 while being the
   simpler/lower-turnover alternative. Tie-break order: fewer active layers,
   lower turnover, larger N, slower rotation, then lexicographic config hash.
3. Deflated-Sharpe / multiple-comparison sanity: compute with the actual
   distinct trial count (≤190); expect the max cell to be partly noise; report
   alongside.
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
- TEST net alpha vs the IWM total-return proxy ≥ +2%/yr, AND
- TEST MaxDD ≤ 1.0× benchmark MaxDD (≤ 0.7× if any overlay cell chosen), AND
- steady-returns constraints (§2) hold on TEST, AND
- results survive the 75bp stress (alpha ≥ 0 at 75bp; headline alpha above is
  already measured at the fixed 40bp baseline).

**PRESUMED OVERFIT (mandatory re-examination, do not celebrate):**
- TEST net alpha > +6%/yr or net Sharpe > 1.3. Treat as a bug until three
  independent checks pass: (a) structural PIT audit proves every row read has
  `available_from ≤ as_of`, plus delay perturbations of {+1,+3,+5,+10}
  sessions are reported without prescribing a performance shape (delays may
  legitimately change discrete rotation decisions); (b) per-year attribution
  shows no single year > 40% of total alpha; (c) top-20 contributing trades are
  manually audited against archived as-of filings and price/action records.

**FAIL:** anything else → document, do NOT tweak-and-rerun on TEST; go back to
TRAIN/VALIDATION with a dated amendment.

Anchors for these numbers: post-publication factor decay ~58% (McLean-Pontiff);
live value+momentum+trend funds by the researchers themselves delivered ~0
alpha (VMOT); credible disciplined-retail edge is +1-3%/yr, +2-4%/yr at the
optimistic end. A Sharpe near 1.0 net over a decade is elite; demand less.

## 6. Paper-forward window (after PASS)

- Freeze the full configuration (code SHA + config hash pinned in the ledger).
- Duration: **≥12 rotations and ≥12 calendar months**, spanning at least two
  earnings seasons; 18 months is preferred before any capital discussion.
- Judged against the same criteria (§2, §5) with slippage recorded (staged LOO
  vs assumed fill).
- Mid-window changes reset the clock. Live capital stays disabled until D024's
  explicit approval, funding, evidence and kill-switch gates all pass.

## 7. Amendment log

- **2026-07-18 — pre-run protocol repair (no results had been produced):**
  froze the moving TEST end date; corrected Stage-1/Stage-2 arithmetic;
  introduced an executable Stage-3 neighborhood audit; replaced ambiguous
  percentage-based plateau language with metric-specific tolerances; replaced
  the invalid "alpha should degrade smoothly" PIT heuristic with structural
  access assertions and neutral delay perturbations; extended paper-forward
  minimum from 6 to 12 months; required a frozen data manifest and one-time
  TEST unlock. This amendment reacts to a document audit, not observed
  performance.
