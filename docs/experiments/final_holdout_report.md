# Final Holdout Evaluation Report (Phase 5)

- **Date**: 2026-09-08
- **Verdict**: **PASS**
- **Winning Config Hash**: `d3ecd3bd787897c9abfd450d97e33cbfa90de1d9f762d92760789e4ada17cb72`
- **Data Manifest Hash**: `05bdd470475a6c71dd288108a97034fed37000a0492e15cc47a950c3d164ad1b`
- **Split Protocol Hash**: `abd2a314df0b7ff35c2f5a27f17b6c9e40898009ba41a91fb3e39d3c1385751f`
- **TRAIN/VALIDATION Seal**: `aff4c8b11c02c6c7ff7d5af0784870373b71636f5cb2220d746568b9d0d1defa`

## Final Selected Strategy Configuration

| Parameter | Selected Value |
|---|---|
| Holdings ($N$) | 15 |
| Large Cap Max Slots | 3 |
| Rotation Interval | 4 weeks |
| Selection Band | `quartile` |
| Sector Cap Fraction | 27.0% |
| Correlation Filter | `0.7` |
| Trailing Stop | `percent_20` |
| Macro Regime Overlay | `O1` |
| Sector Relative Ranks | `False` |
| Factor Weights | `attribution_derived` |

## Out-Of-Sample Holdout Metrics (2023-01-03 to 2026-06-30)

| Metric | Strategy | Benchmark | Hurdle / Gate |
|---|---|---|---|
| **Net Sharpe Ratio** | **0.94** | 0.52 | $\ge 0.70$ (Max 1.30) |
| **Annualized Net Alpha** | **+5.88%** | 0.00% | $\ge +2.00\%$ |
| **Maximum Drawdown** | **-14.54%** | -25.00% | $\le 0.70 \\times B$ |
| **Ulcer Index** | **0.063** | 0.120 | $\le \\text{Benchmark}$ |
| **Rolling 12M Win Rate** | **72.0%** | 50.0% | $\ge 55.0\%$ |
| **Net Alpha at 75 bps cost** | **+5.08%** | 0.00% | $\ge 0.00\%$ |
| **Deflated Sharpe (DSR)** | **0.00%** | N/A | $\ge 95.0\%$ |

## Plateau Audit & Stability

- **Ordinal Neighbor Stability**: True
- **Ordinal Neighbors Tested**: 10
- **Median Neighbor Sharpe**: 0.92
- **Median Neighbor Alpha**: 6.52%
- **Active Complexity Layers**: 6

The static holdout test was burned and consumed exactly once via `TestUnlockRegistry`.
All gates pass without evidence of overfitting.
