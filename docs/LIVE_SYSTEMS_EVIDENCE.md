# LIVE_SYSTEMS_EVIDENCE — how comparable systems actually performed with real money

Research date 2026-07-17 (3-agent web sweep over live services, funds, and
independently tracked model portfolios). This file informs Phase 5 priors and
expectation-setting; it does NOT override the pre-registered protocol.

## The credible live records (not backtests)

| System | Recipe | Params | Live result |
|---|---|---|---|
| Stockopedia NAPS (UK) | composite Q+V+M rank, top-2 per sector | 20 stocks, ANNUAL rebalance | ~15%/yr over 11 forward-published years; beat every UK fund; worst year −16%; "7 years of suffering" below high-water inside that record |
| Validea guru models (US, paper, since 2003) | codified published strategies | 10-20 stocks, monthly-annual | winners = momentum-blended composites (+4.7%/yr over S&P); losers = pure value (Magic Formula −1.7%, Dreman −2.5%); 10-stock books blow up (−30% single year) |
| AAII Shadow Stock (REAL money) | micro-cap value | ~30 stocks, quarterly reviews | +14.9%/yr since 1993 — the most believable real-money number found |
| MTUM (ETF) | risk-adjusted 12-6mo momentum | ~125 names, semi-annual+buffers, 0.15% | +2-3pp/yr over S&P for 13 years — momentum survives live when cost-obsessed |
| AVUV (ETF) | small value + profitability | ~700 names, continuous banded migration | +4.7pp/yr over Russell 2000 Value since 2019 — best live factor alpha in sample |
| DSTL / SYLD (ETFs) | normalized-FCF value + stability / shareholder yield | ~100 names, quarterly | matched S&P while carrying a value tilt — value survives when measured as FCF/payout, not P/B |
| QVAL/QMOM (ETFs) | concentrated single-factor | ~50 names | honest factor exposure, but lagged S&P ~4pp/yr over 11 years (tracking-error pain) |
| ZIG / Magic Formula live | pure cheapness | 20-30 names | ~half the market's return over 7-20 years — pure deep value is the decade's worst live style |

## The uniform live-vs-backtest haircut

- McLean-Pontiff (97 anomalies): −26% out-of-sample, −58% post-publication.
- Suhonen et al. (215 bank strategies): median −73% Sharpe live vs backtest.
- Quantopian (888 algos): backtest Sharpe predicts live Sharpe with R² < 0.025;
  each extra backtest iteration widens the gap.
- Portfolio123 community (90 live designer models): live = 50-66% of backtest
  is considered a GOOD outcome; zero performance persistence among models.
- **Planning rule: live = backtest-excess × 0.5, and complexity is the single
  strongest decay predictor** (few-parameter systems decayed least).

## ⚠ The trend-overlay warning (directly relevant to our O1-O3 grid cells)

Academic OOS evidence favors the 200d/10-mo SMA filter (Faber's paper rule even
held out-of-sample 2006-2012: 10.5%/yr with −9.5% MaxDD). But EVERY real-money
implementation 2015-2026 paid dearly:

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
| Few parameters (survivors have <~5 free params) | ✅ small grid, plateau rule |
| No mechanism through which panic can act | ✅ frozen ruleset + paper-forward |
| Holdings: retail paper systems 20-30; live funds 100+; sub-10 books blow up | ⚠ our default 15 is at the low end — expect the grid to favor 20-25 |
| Cadence: value=annual/quarterly, momentum=semi-annual w/ buffers; no live winner rebalances monthly on price | ⚠ our monthly default may lose to 6w/quarterly cells — let the grid decide |
| Behavior gap: Greenblatt's self-directed accounts −25pp vs automated; abandonment at trough is the #1 documented killer | ✅ the whole D009/paper-forward design exists for this |

## Sources

Primary references captured in the research transcript; key public ones:
Stockopedia NAPS reviews, Validea portfolios page + 20-year retrospectives,
Zacks rank disclosure, Portfolio123 community OOS studies, stockanalysis.com
fund pages (ZIG/QVAL/GMOM/PTLC/MTUM/AVUV/DSTL/SYLD), Morningstar "This Fund
Followed the Rules. That Was the Problem" (PTLC), VMOT SEC filings +
portfolioslab, Allocate Smartly research notes, Wiecki et al. 2016 (Quantopian),
McLean-Pontiff JF 2016, Suhonen et al. JPM 2017.
