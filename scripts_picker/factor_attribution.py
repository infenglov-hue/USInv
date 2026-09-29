"""A4 factor attribution: does each factor's score predict forward returns?

For every scoring date, rank the scored universe by each factor and measure
the N-calendar-day forward return (14 = live biweekly hold). Report per-factor
mean Spearman IC, IC information ratio (mean/std), t-stat, hit rate (share of
dates with IC>0), and top-minus-bottom decile spread.

Pure analysis over a clean point-in-time-scored DB — no rescoring.

Usage:
    python scripts/factor_attribution.py <scored_db.sqlite> [horizon_days=14]

Findings (2026-07-05, clean skip=30 archive): value factors (Graham, DCF,
Magic Formula) carry 2-3x the IC of quality factors; momentum and technical
are net-negative at the hold horizon (short-term reversal). Full writeup +
the backtest showing you must NOT naively cut the momentum tilt:
docs/factor_attribution_2026-07.md.
"""
import sqlite3
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

DB = sys.argv[1]
HORIZON_DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 14

FACTORS = [
    ("buffett_score", "Buffett kalite"),
    ("graham_score", "Graham değer"),
    ("piotroski_fscore", "Piotroski F"),
    ("magic_formula_rank", "Magic Formula"),
    ("lynch_peg_score", "Lynch PEG"),
    ("momentum_score", "Momentum"),
    ("technical_score", "Teknik"),
    ("dcf_margin_of_safety_pct", "DCF MoS"),
    ("dividend_score", "Temettü"),
    ("insider_score", "İçeriden alım"),
    ("composite_alpha", "COMPOSITE (blend)"),
]

con = sqlite3.connect(DB)

# Investable-ish universe: exclude the index/mock/test rows and require a
# composite so we only score names that could actually be picked.
cols = ", ".join(sorted({f for f, _ in FACTORS} | {"company_id", "scoring_date"}))
scores = pd.read_sql(
    f"""SELECT {cols} FROM scoring_results sr
        JOIN companies c ON c.id = sr.company_id
        WHERE (c.company_type IS NULL OR c.company_type != 'INDEX')
          AND c.ticker NOT LIKE 'MOCK%' AND c.ticker NOT LIKE 'TEST%'
    """,
    con,
)

# Adjusted close prices for forward-return lookup.
prices = pd.read_sql(
    "SELECT company_id, date, COALESCE(adjusted_close, close) AS px "
    "FROM daily_prices WHERE COALESCE(adjusted_close, close) > 0",
    con,
)
con.close()

prices["date"] = pd.to_datetime(prices["date"])
prices = prices.sort_values(["company_id", "date"])
# Per-company sorted arrays for as-of lookup.
by_company = {}
for cid, grp in prices.groupby("company_id"):
    by_company[cid] = (grp["date"].values, grp["px"].values)


def price_asof(cid, when):
    arr = by_company.get(cid)
    if arr is None:
        return None
    dates, px = arr
    idx = np.searchsorted(dates, np.datetime64(when), side="right") - 1
    if idx < 0:
        return None
    # Reject stale prints (>10 days before target -> likely delisted/suspended)
    if (np.datetime64(when) - dates[idx]) / np.timedelta64(1, "D") > 10:
        return None
    return float(px[idx])


scores["scoring_date"] = pd.to_datetime(scores["scoring_date"])
rows = []
for d, grp in scores.groupby("scoring_date"):
    fwd_date = d + timedelta(days=HORIZON_DAYS)
    fwd = []
    for cid in grp["company_id"].values:
        p0 = price_asof(cid, d)
        p1 = price_asof(cid, fwd_date)
        fwd.append(p1 / p0 - 1.0 if (p0 and p1 and p0 > 0) else np.nan)
    g = grp.copy()
    g["fwd_ret"] = fwd
    rows.append(g)

panel = pd.concat(rows, ignore_index=True)
panel = panel.dropna(subset=["fwd_ret"])

print(f"Panel: {len(panel):,} obs, {panel['scoring_date'].nunique()} dates, "
      f"horizon {HORIZON_DAYS}d\n")
print(f"{'Faktör':<20}{'ort.IC':>8}{'IC-IR':>8}{'t-stat':>8}{'IC>0%':>7}{'D1-D10%':>9}{'n/tarih':>9}")
print("-" * 69)

results = []
for col, label in FACTORS:
    ics, spreads, ns = [], [], []
    for d, grp in panel.groupby("scoring_date"):
        sub = grp[[col, "fwd_ret"]].dropna()
        if len(sub) < 20 or sub[col].nunique() < 5:
            continue
        ic, _ = spearmanr(sub[col], sub["fwd_ret"])
        if not np.isnan(ic):
            ics.append(ic)
        ns.append(len(sub))
        # Decile spread (top decile minus bottom decile mean fwd return)
        try:
            sub = sub.copy()
            sub["dec"] = pd.qcut(sub[col].rank(method="first"), 10, labels=False)
            top = sub.loc[sub["dec"] == 9, "fwd_ret"].mean()
            bot = sub.loc[sub["dec"] == 0, "fwd_ret"].mean()
            spreads.append(top - bot)
        except Exception:
            pass
    if not ics:
        continue
    ics = np.array(ics)
    mean_ic = ics.mean()
    ic_ir = mean_ic / ics.std() if ics.std() > 0 else 0.0
    tstat = ic_ir * np.sqrt(len(ics))
    hit = (ics > 0).mean() * 100
    spread = np.nanmean(spreads) * 100 if spreads else float("nan")
    results.append((label, mean_ic, ic_ir, tstat, hit, spread, np.mean(ns)))

for label, mean_ic, ic_ir, tstat, hit, spread, n in sorted(
    results, key=lambda r: -r[1]
):
    print(f"{label:<20}{mean_ic:>8.4f}{ic_ir:>8.3f}{tstat:>8.2f}{hit:>7.1f}{spread:>9.2f}{n:>9.0f}")

print("\nNot: IC = date-bazında Spearman(faktör skoru, 14g ileri getiri) ortalaması.")
print("t-stat > 2 anlamlı; IC-IR sinyal istikrarı; D1-D10 en iyi decile - en kötü decile.")
