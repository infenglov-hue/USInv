# Phase 0.4 feasibility evidence report

- Input manifest SHA-256: `d61234c928daecdf29fc229c1fad3919a380a9cd71720c19e7c4037b8bc318d6`
- Sample: 36 deliberately awkward securities across 9 strata
- Providers assessed: eodhd, alpha_vantage, crsp
- Metadata-only live observations: 7
- Gate: **BLOCKED** pending user selection of `research` or `audit` mode and any approved provider access/spend.

A contract marked `documented` is not treated as observed sample coverage. Only explicit
sample-scoped observations override a matrix cell. Raw responses remain under ignored
local artifacts and are represented here only by hashes and validation metadata.

## Strata

| Stratum | Securities |
|---|---:|
| acquired | 4 |
| active | 4 |
| bankrupt | 4 |
| multi_class | 4 |
| otc_moved | 4 |
| post_2018_delisted | 4 |
| pre_2018_delisted | 4 |
| reverse_split | 4 |
| ticker_recycled | 4 |

## Provider verdicts

| Provider | Evidence route | Current feasibility verdict |
|---|---|---|
| eodhd | research_candidate | Plausible low-cost research archive, not audit-grade: pre-2018 delisted securities have EOD only and the identity/action gaps remain unobserved on the 36-security sample. |
| alpha_vantage | historical_universe_candidate | Useful date-specific active/delisted universe input after 2010, but not a security master: it lacks delisting reason, permanent identifiers and explicit ticker validity intervals. |
| crsp | audit_candidate | Strong audit candidate for permanent security identity, corporate actions and delistings, but access is institutional and the documented daily file does not by itself supply a full opening-price OHLCV series. |

## Live observations

| Observation | Provider | Scope | Raw SHA-256 | Result |
|---|---|---|---|---|
| eodhd-demo-aapl-eod-2020 | eodhd | active_aapl | `65b984010beeff927e23a681666bea84bb665f7fdcedaa55ae4336c14ebb0e72` | raw_ohlcv=observed, adjusted_close=observed |
| eodhd-demo-aapl-split-2020 | eodhd | active_aapl | `7965c8570969c004e5fa5067c02d45f8109a6390f7aab844a6775401066c4dc9` | splits=observed |
| eodhd-demo-aapl-dividends-2020 | eodhd | active_aapl | `52d1ec1314086aee44b39920cc11f74bb9c851ded4f2f2314419b42860251851` | dividends=observed |
| eodhd-demo-aapl-id-mapping-blocked | eodhd | active_aapl | `bee609daff180bf6ed65f0bd9b035b2e6636561f1dde89ac2c5ebc7b652f55f4` | identifier_mapping=blocked |
| eodhd-demo-delisted-list-blocked | eodhd | provider snapshot | `78342a0905a72ce44da083dcb5d23b8ea0c16992ba2a82eece97e033d76ba3d3` | listing_date=blocked, delisting_date=blocked, historical_exchange_security_type=blocked, ticker_validity_intervals=blocked |
| alpha-vantage-demo-delisted-2014-07-10 | alpha_vantage | provider snapshot | `cccdae28ca4fd36b9a720f1acd6c22a07503da5eaa009be82b77b71e2481c0e4` | listing_date=observed, delisting_date=observed, historical_exchange_security_type=observed |
| alpha-vantage-demo-active-2014-07-10-invalid | alpha_vantage | provider snapshot | `44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a` | listing_date=invalid, historical_exchange_security_type=invalid |

## Consequence

The cheap research route is technically plausible but still has an explicit pre-2018
delisted-action gap. EODHD's public terms also require deletion within one month
after expiry, so the old one-month-then-retain plan is prohibited without a written
override. CRSP is a strong
audit-source candidate for actions, delistings and identity history, but it
requires institutional access and does not by itself document full daily OHLCV
with an opening field. No honest historical
backtest claim is permitted at this stage.
