# Phase 0.4 feasibility evidence report

- Input manifest SHA-256: `a25783cb38525cc46e4d69cd94b09f3295865048bed54d91c2bde106e60180d5`
- Sample: 36 deliberately awkward securities across 9 strata
- Providers assessed: eodhd, alpha_vantage, crsp
- Metadata-only live observations: 43
- Gate: **BLOCKED** pending a retention-permitted research price archive and its full sample probe.

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
| alpha_vantage | historical_universe_candidate | Useful date-specific active/delisted universe input after 2010, with 24/36 awkward samples matched. It is not a security master: OTC and pre-2010 cases were absent, two recycled symbols matched both snapshots, and permanent identifiers, delisting reasons and explicit ticker validity intervals are absent. |
| crsp | audit_candidate | Strong audit candidate for permanent security identity, corporate actions and delistings, but access is institutional and the documented daily file does not by itself supply a full opening-price OHLCV series. |

## Live observations

| Observation | Provider | Scope | Evidence SHA-256 | Result |
|---|---|---|---|---|
| eodhd-demo-aapl-eod-2020 | eodhd | active_aapl | `65b984010beeff927e23a681666bea84bb665f7fdcedaa55ae4336c14ebb0e72` | raw_ohlcv=observed, adjusted_close=observed |
| eodhd-demo-aapl-split-2020 | eodhd | active_aapl | `7965c8570969c004e5fa5067c02d45f8109a6390f7aab844a6775401066c4dc9` | splits=observed |
| eodhd-demo-aapl-dividends-2020 | eodhd | active_aapl | `52d1ec1314086aee44b39920cc11f74bb9c851ded4f2f2314419b42860251851` | dividends=observed |
| eodhd-demo-aapl-id-mapping-blocked | eodhd | active_aapl | `bee609daff180bf6ed65f0bd9b035b2e6636561f1dde89ac2c5ebc7b652f55f4` | identifier_mapping=blocked |
| eodhd-demo-delisted-list-blocked | eodhd | provider snapshot | `78342a0905a72ce44da083dcb5d23b8ea0c16992ba2a82eece97e033d76ba3d3` | listing_date=blocked, delisting_date=blocked, historical_exchange_security_type=blocked, ticker_validity_intervals=blocked |
| alpha-vantage-demo-delisted-2014-07-10 | alpha_vantage | provider snapshot | `cccdae28ca4fd36b9a720f1acd6c22a07503da5eaa009be82b77b71e2481c0e4` | listing_date=observed, delisting_date=observed, historical_exchange_security_type=observed |
| alpha-vantage-demo-active-2014-07-10-invalid | alpha_vantage | provider snapshot | `44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a` | listing_date=invalid, historical_exchange_security_type=invalid |
| alpha-vantage-personal-listing-2026-07-18--active_aapl | alpha_vantage | active_aapl | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--active_msft | alpha_vantage | active_msft | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--active_cost | alpha_vantage | active_cost | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--active_mnst | alpha_vantage | active_mnst | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--acquired_twtr | alpha_vantage | acquired_twtr | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--acquired_atvi | alpha_vantage | acquired_atvi | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--acquired_vmw | alpha_vantage | acquired_vmw | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--acquired_splk | alpha_vantage | acquired_splk | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--bankrupt_bbbyq | alpha_vantage | bankrupt_bbbyq | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--bankrupt_sivbq | alpha_vantage | bankrupt_sivbq | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--bankrupt_revq | alpha_vantage | bankrupt_revq | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--bankrupt_yellq | alpha_vantage | bankrupt_yellq | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--otc_fnma | alpha_vantage | otc_fnma | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--otc_fmcc | alpha_vantage | otc_fmcc | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--otc_lkncy | alpha_vantage | otc_lkncy | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--otc_didiy | alpha_vantage | otc_didiy | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--recycled_gm | alpha_vantage | recycled_gm | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--recycled_s | alpha_vantage | recycled_s | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--recycled_czr | alpha_vantage | recycled_czr | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--recycled_life | alpha_vantage | recycled_life | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--multiclass_brkb | alpha_vantage | multiclass_brkb | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--multiclass_goog | alpha_vantage | multiclass_goog | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--multiclass_fox | alpha_vantage | multiclass_fox | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--multiclass_nws | alpha_vantage | multiclass_nws | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--reverse_ge | alpha_vantage | reverse_ge | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--reverse_c | alpha_vantage | reverse_c | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--reverse_aig | alpha_vantage | reverse_aig | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--reverse_bkng | alpha_vantage | reverse_bkng | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--pre2018_enrnq | alpha_vantage | pre2018_enrnq | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--pre2018_lehmq | alpha_vantage | pre2018_lehmq | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--pre2018_bsc | alpha_vantage | pre2018_bsc | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--pre2018_cfc | alpha_vantage | pre2018_cfc | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--post2018_jcpnq | alpha_vantage | post2018_jcpnq | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed_gap, delisting_date=observed_gap, historical_exchange_security_type=observed_gap |
| alpha-vantage-personal-listing-2026-07-18--post2018_chkaq | alpha_vantage | post2018_chkaq | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--post2018_wll | alpha_vantage | post2018_wll | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed, historical_exchange_security_type=observed |
| alpha-vantage-personal-listing-2026-07-18--post2018_cbl | alpha_vantage | post2018_cbl | `5e2d6be6ef12588c2647a16e42833182aa68ad99c27c5abfd147ac8d6ffdeeda` | listing_date=observed, delisting_date=observed_gap, historical_exchange_security_type=observed |

## Consequence

The cheap research route is technically plausible but still has an explicit pre-2018
delisted-action gap. EODHD's public terms also require deletion within one month
after expiry, so the old one-month-then-retain plan is prohibited without a written
override. CRSP is a strong
audit-source candidate for actions, delistings and identity history, but it
requires institutional access and does not by itself document full daily OHLCV
with an opening field. No honest historical
backtest claim is permitted at this stage.
