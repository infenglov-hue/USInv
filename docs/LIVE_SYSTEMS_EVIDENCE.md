# LIVE_SYSTEMS_EVIDENCE — comparable live and forward-published evidence

Research date 2026-07-17; methodology repaired 2026-07-18. This is an evidence
map, not a performance audit. It separates registered funds, sponsor-maintained
model portfolios and academic studies because they do not have equal evidentiary
weight. Exact returns are not experiment inputs until a reproducible capture
(source URL/file, as-of date, benchmark, dividend treatment and calculation)
is stored in the Phase-5 report. This file informs priors only and never
overrides EXPERIMENT_PLAN.md.

## Evidence classes

| Example | Evidence class | Design observation | Permitted inference |
|---|---|---|---|
| Stockopedia NAPS (UK) | Sponsor-published model portfolio; forward selections, not an audited fund | Q+V+M, sector diversification, ~20 stocks, annual rebalance | Useful behavioral/design example; its sponsor-reported ~15% annualized record is not direct US evidence or independent proof |
| Validea guru models | Publisher-maintained paper/model portfolios | 10-20 stocks, monthly-to-annual; blended models often compare better than pure cheapness | Hypothesis generator only until transactions and benchmark series are independently reproduced |
| AAII Shadow Stock | Sponsor says performance is based on actual tracked trades; still a model product, not an external fund audit | ~30 micro/small-value stocks, quarterly review | Evidence that a simple ruleset can be maintained for decades; exact CAGR must carry an as-of date |
| MTUM, AVUV, DSTL, SYLD, QVAL/QMOM, PTLC, former VMOT | Registered live funds with SEC filings/NAV histories, but different universes, capacity and mandates | Buffers, broad diversification and implementation cost matter; concentrated styles can endure long relative drawdowns | Stronger implementation evidence, not an apples-to-apples test of USInv alpha |
| Academic OOS studies | Peer-reviewed or working-paper datasets | Published anomalies and optimized backtests commonly decay | Supports conservative expectations and multiple-testing controls, not a fixed live/backtest conversion factor |

## Live-vs-backtest decay prior — not a law

- McLean-Pontiff (97 anomalies): −26% out-of-sample, −58% post-publication.
- Suhonen et al. (215 bank strategies): median −73% Sharpe live vs backtest.
- Quantopian (888 algos): backtest Sharpe predicts live Sharpe with R² < 0.025;
  each extra backtest iteration widens the gap.
- Portfolio123 community observations are useful but are not peer-reviewed and
  must not be presented at the same evidence grade as the studies above.
- **Planning prior:** haircut backtested *excess* return by roughly 50% in
  expectation-setting and show 30/50/70% haircut scenarios. This is a scenario,
  not a forecast or pass/fail transformation. Complexity and researcher
  iteration count are risk flags; their effect is dataset-dependent.

## ⚠ The trend-overlay warning (directly relevant to our O1-O3 grid cells)

Academic evidence has supported 200d/10-month trend rules in some periods, but
several prominent live implementations since 2015 lagged badly or captured
less upside than intended. The examples below are warnings, not proof that
every trend overlay fails:

- PTLC (mechanical 200d SMA on S&P, multi-billion AUM): **−5pp/yr vs S&P over
  10 live years**; 2020: −1.1% vs +18.4% (exited after crash, re-entered after
  rebound). Drawdown relief real but modest (−26.6% vs low-30s).
- VMOT (value+momentum+trend by the trend literature's own champions):
  ~2.3%/yr for 7.7 years, 72% downside capture / 49% upside capture — inverse
  of design; sponsor DELETED the trend overlay Jan 2025.
- VAMO (CAPE-triggered hedge): hedged through an entire bull market; ~2.6%/yr.
- GEM dual momentum: 17%/yr backtest → ~6%/yr live 2014-2022 with a WORSE
  drawdown than the backtest.

Interpretation for us: the 2015-2026 regime (V-shaped recoveries) was the
overlay's worst case, and monthly-confirmation signals were structurally too
slow. This is precisely why O0 (no overlay) is a control cell and why the
pass criterion demands overlay cells EARN their MaxDD improvement. Expect O0
to be competitive; do not romanticize the overlay. Valuation-based (CAPE)
triggers are disqualified outright — they never re-admit you.

## Convergent design of live winners (vs our blueprint)

| Live-winner pattern | USInv blueprint status |
|---|---|
| Momentum blended INTO value composite (never cheapness alone) | ✅ core design |
| Q+V+M composite in less-efficient small/mid caps = best surviving class | ✅ core design |
| Value measured as FCF/EBIT-TEV/payout, not P/B | ✅ MODEL_SPEC §1 |
| Buffers/banding instead of hard rank tracking (universal among winners) | ✅ buy/hold band |
| Cost model treated as part of alpha | ✅ inside objective |
| Believable live edge +2-7%/yr; >30% CAGR claims unsustainable | ✅ +2-4% target, >6% = presumed overfit |
| Few economically motivated parameters; complexity increases decay risk | ⚠ USInv still has nine axes; staged search, ablations and simplicity tie-breaks reduce but do not erase this risk |
| No mechanism through which panic can act | ✅ frozen ruleset + paper-forward |
| Observed model portfolios often hold 20-30; scalable funds often hold far more | ⚠ 15 is a hypothesis, not a conclusion; concentration and capacity differ across examples |
| Many implementations rebalance more slowly and use buffers | ⚠ monthly may lose to 6w/quarterly, but the sample does not prove a universal optimum |
| Behavior gap: Greenblatt's self-directed accounts −25pp vs automated; abandonment at trough is the #1 documented killer | ✅ the whole D009/paper-forward design exists for this |

## Sources and reproducibility

The durable source register is `SOURCE_REGISTER.md`. Minimum core references:

- Stockopedia's own NAPS description (explicitly a model portfolio):
  https://www.stockopedia.com/academy/events/inside-the-naps-portfolio-how-a-simple-rules-based-strategy-has-delivered/
- AAII Shadow Stock support/method description:
  https://aaiiweb.atlassian.net/wiki/spaces/APS/pages/155549740/Shadow+Stock+Portfolio
- Wiecki et al., 888 Quantopian algorithms:
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2745220
- McLean & Pontiff, 97 published predictors:
  https://doi.org/10.1111/jofi.12365
- VMOT strategy change filing (SEC, effective 2025-01-31):
  https://www.sec.gov/Archives/edgar/data/1592900/000159290025000034/ck0001592900-20240930.htm

Secondary fund aggregators may help discovery but cannot support an exact
performance claim in the final report. Fund comparisons must be reproduced
from frozen adjusted NAV/price data with identical dates and dividend treatment.
