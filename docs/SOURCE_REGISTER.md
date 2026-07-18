# SOURCE_REGISTER — external contracts and evidence grades

This is the durable replacement for claims that sources were "captured in a
research transcript." A future coding agent must be able to reproduce every
load-bearing external fact from the repository alone.

## Evidence grades

- **P — primary contract:** regulator, exchange, API provider or official fund
  filing. Suitable for implementation after the recheck date.
- **A — academic primary:** peer-reviewed paper or authors' working paper.
  Suitable for priors/methodology, not an API contract.
- **S — sponsor self-report:** model-portfolio/service owner. Useful context;
  exact performance needs independent reproduction.
- **X — secondary discovery:** aggregator, press article or community post.
  May locate a source but cannot support a final numeric claim.

## Data and operations contracts

| Contract | Grade | Primary source | What the blueprint relies on | Checked | Recheck |
|---|---:|---|---|---|---|
| SEC Financial Statement Data Sets | P | https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets | As-filed quarterly archives begin in 2009; Dec-2024 reprocessing added NUM `segments`; archives can be replaced | 2026-07-18 | Before Phase 1 and each quarterly refresh |
| FSDS field definitions | P | https://www.sec.gov/files/fsds.pdf | `accepted` is the filing acceptance timestamp; `ddate` is rounded to nearest month-end; `qtrs` is rounded quarter count; NUM values are unscaled | 2026-07-18 | Pin and hash the PDF used by ingestion tests |
| SEC EDGAR APIs | P | https://www.sec.gov/search-filings/edgar-application-programming-interfaces | `submissions` is current entity metadata/filing history; `companyfacts` provides facts but not per-fact acceptance; both endpoints were live-smoked on 2026-07-18; bulk ZIPs refresh nightly | 2026-07-18 | Before Phase 1.5 and on schema drift |
| SEC automated-access policy | P | https://www.sec.gov/about/developer-resources | Declare a monitored contact in User-Agent, cache/back off, and stay at ≤10 requests/s; USInv hard-caps itself at 8 | 2026-07-18 | Before every deployment environment |
| exchange-calendars 4.13.2 | P (package) | https://github.com/gerrymanoim/exchange_calendars/releases/tag/4.13.2 | Version-pinned XNYS sessions, official UTC open/close instants and special closes; USInv golden tests remain authoritative for declared traps | 2026-07-18 | Before dependency upgrades and annually |
| secfsdstools 2.4.3 | P (package) | https://pypi.org/project/secfsdstools/2.4.3/ | Apache-2.0, Python ≥3.10 and Python 3.12 classifier; wheel hash is published | 2026-07-18 | Pin exact version/hash in Phase 0 |
| Alpaca Basic market data | P | https://docs.alpaca.markets/docs/about-market-data-api | Free Basic: historical data since 2016, 200 historical requests/minute, latest 15 minutes restricted; equities real-time coverage is IEX while delayed SIP is available | 2026-07-18 | Phase 0.4 and quarterly |
| EODHD price/plan | P | https://eodhd.com/pricing | Personal EOD Historical Data—All World was $19.99/month with 100k calls/day and splits/dividends/delisted features at the plan level | 2026-07-18 | Immediately before purchase |
| EODHD delisted coverage | P | https://eodhd.com/financial-apis/delisted-stock-companies-data-2 | Pre-2018 delisted securities: EOD only; post-2018: EOD+fundamentals+dividends+splits; post-2021 also intraday | 2026-07-18 | Phase 0.4 and before snapshot |
| EODHD recycled symbols | P | https://eodhd.com/financial-academy/financial-faq/survivorship-bias-free-financial-analysis | Delisted/recycled tickers may use `_old`; active and `delisted=1` lists must both be captured | 2026-07-18 | Phase 0.4/5.1 |
| EODHD identifier mapping | P | https://eodhd.com/financial-apis/id-mapping-api-cusip-isin-figi-lei-cik-%E2%86%94-symbol | Endpoint can return symbol/ISIN/FIGI/LEI/CUSIP/CIK mappings; plan entitlement and historical validity are not assumed | 2026-07-18 | Verify entitlement and date semantics in Phase 0.4 |
| Alpha Vantage listing status | P | https://www.alphavantage.co/documentation/ | `LISTING_STATUS` accepts a date after 2010-01-01 and returns active/delisted US stocks/ETFs for that historical date | 2026-07-18 | Phase 0.4, then archive every used CSV |
| Tiingo EOD schema | P | https://www.tiingo.com/documentation/end-of-day | Raw+adjusted fields, `divCash`, `splitFactor`, exchange/start/end metadata; account token/limits require empirical check | 2026-07-18 | Phase 0.4/2.2 |
| Nasdaq minimum-bid/reverse-split rule | P | https://listingcenter.nasdaq.com/material_search.aspx?cid=14&mcd=lq | Prior-one-year reverse split and cumulative two-year 1-for-250 conditions affect eligibility for the normal compliance period | 2026-07-18 | Before implementing listing-risk gate |
| NYSE American continued-listing changes | P | https://www.nyse.com/publicdocs/nyse/markets/nyse-american/NYSE_American_2026_Annual_Guidance_Letter.pdf | Rules differ from Nasdaq; two-year cumulative 200-for-1 and other conditions must be encoded exchange-specifically | 2026-07-18 | Annual rule review + before Phase 3.1 |
| Cash-account funding/Regulation T | P | https://www.finra.org/sites/default/files/NoticeDocument/p003091.pdf | Same-day sale proceeds are not automatically sufficient settled funds for a separate cash-account purchase; broker/account treatment matters | 2026-07-18 | Before paper orders and on broker/account change |

## Research/evidence references

| Claim family | Grade | Source | Permitted use |
|---|---:|---|---|
| 97 anomalies: OOS and post-publication decay | A | https://doi.org/10.1111/jofi.12365 | Conservative priors and publication-decay discussion; not a fixed haircut law |
| 888 Quantopian algorithms: common backtest metrics weakly predict OOS | A | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2745220 | Motivation for iteration-count logging and one-shot holdout |
| 215 alternative-beta strategies: backtest/live decay | A | https://doi.org/10.3905/jpm.2017.43.2.090 | Additional decay evidence; population differs from long-only stock selection, so no deterministic haircut |
| NAPS forward-published model | S | https://www.stockopedia.com/academy/events/inside-the-naps-portfolio-how-a-simple-rules-based-strategy-has-delivered/ | Design/behavior example; sponsor performance is not an audited US fund result |
| AAII Shadow Stock method/actual tracked trades | S | https://aaiiweb.atlassian.net/wiki/spaces/APS/pages/155549740/Shadow+Stock+Portfolio | Long-running ruleset example; exact CAGR requires dated independent calculation |
| VMOT strategy change | P | https://www.sec.gov/Archives/edgar/data/1592900/000159290025000034/ck0001592900-20240930.htm | Confirms the 2025 fund objective/strategy/ticker change; return comparisons still require reproduced NAV data |

## Update rule

When an external contract changes, append the old observation to the amendment
section of the affected spec, update this row's checked date, archive the source
or its hash where licensing permits, and add a regression fixture. Never edit a
provider adapter merely to fit a new response while leaving the declared
contract stale.
