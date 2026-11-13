"""Run an independent US equity factor diagnostic on the registered TRAIN period."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

START = "2017-01-09"
END = "2021-12-27"
PRICE_START = "2016-01-04"
HOLDINGS = 15
MAX_PER_SIC_GROUP = 4
COSTS_BPS_PER_SIDE = (10, 20, 40)
MIN_DOLLAR_VOLUME = 20_000_000
MIN_PRICE = 5
FACTOR_NAMES = ("quality", "value", "momentum")


def factors_for_signal(signal, closes, raw_close, adv, metadata, memberships, financials, splits):
    members = memberships[
        (memberships.start_date <= signal)
        & (memberships.end_date.isna() | (signal < memberships.end_date))
    ].ticker
    pool = metadata[metadata.ticker.isin(members)].copy()
    pool["adv"] = adv.loc[signal]
    signal_close = raw_close.loc[signal]
    pool = pool[(pool.adv >= MIN_DOLLAR_VOLUME) & (signal_close.reindex(pool.index) >= MIN_PRICE)]
    # One share class per issuer, based on the liquidity visible at signal time.
    pool = pool.sort_values(["adv", "ticker"], ascending=[False, True]).drop_duplicates("cik")
    facts = (
        financials[
            (financials.period_end <= financials.publication_date)
            & (financials.publication_date <= signal)
            & (financials.period_end >= signal - pd.Timedelta(days=180))
        ]
        .drop_duplicates("company_id", keep="last")
        .set_index("company_id")
    )
    split_ratios = (
        splits[splits.action_date <= signal].groupby("company_id").adjustment_factor.prod()
    )
    base_price = signal_close * split_ratios.reindex(signal_close.index, fill_value=1)
    signal_index = closes.index.get_loc(signal)
    if signal_index < 252:
        raise ValueError("12-month momentum requires 252 prior sessions")
    pool["quality"] = facts.roa_adjusted
    pool["value"] = facts.eps_adjusted / base_price
    pool["momentum"] = closes.iloc[signal_index - 21] / closes.iloc[signal_index - 252] - 1
    pool = pool.replace([np.inf, -np.inf], np.nan).dropna(subset=list(FACTOR_NAMES))
    return pool.assign(**{factor: pool[factor].rank(pct=True) for factor in FACTOR_NAMES})


def targets_for_signal(pool):
    if len(pool) < HOLDINGS:
        raise ValueError("Insufficient factor coverage at a scheduled signal")
    targets = {"equal_weight_eligible": pool.index.to_list()}
    scores = pool.assign(composite=pool[list(FACTOR_NAMES)].mean(axis=1))
    for name in ("composite", *FACTOR_NAMES):
        ranked = scores.sort_values([name, "ticker"], ascending=[False, True])
        within_group = ranked.groupby("sic_group").cumcount() < MAX_PER_SIC_GROUP
        targets[name] = ranked[within_group].index[:HOLDINGS].to_list()
        if len(targets[name]) < HOLDINGS:
            raise ValueError(f"Insufficient sector coverage for {name}")
    return targets


@dataclass
class Portfolio:
    positions: dict[int, float] = field(default_factory=dict)
    cash: float = 1.0
    traded: float = 0.0
    missing_open_positions: int = 0
    missing_open_orders: int = 0
    large_open_moves: int = 0
    greatest_mark_staleness_sessions: int = 0

    def value_at_open(self, marks, actual, ratios, staleness, targets=None, cost_bps=0):
        held = pd.Series(self.positions, dtype=float)
        before = held * marks.reindex(held.index)
        if before.isna().any():
            raise ValueError("Held position lacks an available valuation")
        open_available = actual.reindex(held.index).notna()
        self.missing_open_positions += int((~open_available).sum())
        if not open_available.all():
            self.greatest_mark_staleness_sessions = max(
                self.greatest_mark_staleness_sessions,
                int(staleness.reindex(held.index[~open_available]).max()),
            )
        changes = ratios.reindex(held.index)
        self.large_open_moves += int(((changes > 2) | (changes < 0.5)).sum())
        nav = self.cash + before.sum()
        if targets is None:
            return nav

        slots = actual.reindex(targets).dropna().index
        self.missing_open_orders += len(targets) - len(slots)
        frozen = before[~open_available]
        tradable = before[open_available]
        trade_ids = tradable.index.union(slots)
        old = tradable.reindex(trade_ids, fill_value=0.0)
        cost = 0.0
        for _ in range(12):
            each = (nav - frozen.sum() - cost) / len(targets)
            target = pd.Series(each, index=slots).reindex(trade_ids, fill_value=0.0)
            traded = (target - old).abs().sum()
            cost = traded * cost_bps / 10_000
        self.traded += traded
        self.cash = nav - frozen.sum() - target.sum() - cost
        self.positions = (frozen / marks.reindex(frozen.index)).to_dict()
        buys = target.loc[slots]
        buys = buys[buys > 0]
        self.positions.update((buys / actual.loc[buys.index]).to_dict())
        return nav - cost


def _summary(values, days, portfolio=None):
    nav = np.asarray(values)
    return {
        "total_return": float(nav[-1] - 1),
        "cagr": float(nav[-1] ** (365.25 / (days[-1] - days[0]).days) - 1),
        "max_drawdown_open_marks": float(
            (nav / np.maximum.accumulate(np.r_[1, nav])[1:] - 1).min()
        ),
        **(
            {
                "annualized_one_way_turnover": portfolio.traded
                * 365.25
                / (2 * nav.mean() * (days[-1] - days[0]).days),
                "missing_open_position_days": portfolio.missing_open_positions,
                "missing_open_orders": portfolio.missing_open_orders,
                "large_open_moves_in_held_positions": portfolio.large_open_moves,
                "greatest_mark_staleness_sessions": portfolio.greatest_mark_staleness_sessions,
            }
            if portfolio
            else {}
        ),
    }


def run(database: Path):
    # Expect a detached snapshot. Ignore transient SQLite sidecar metadata.
    wal = database.with_name(database.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("Make a detached SQLite snapshot before running the TRAIN diagnostic")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as source:
        membership = pd.read_sql_query(
            "SELECT ticker, start_date, end_date FROM index_memberships "
            "WHERE index_name='SP500' AND start_date <= ? AND (end_date > ? OR end_date IS NULL)",
            source,
            params=(END, PRICE_START),
            parse_dates=["start_date", "end_date"],
        )
        companies = pd.read_sql_query(
            "SELECT id, ticker, cik, sic, company_type FROM companies",
            source,
        ).set_index("id")
        members = companies[companies.ticker.isin(membership.ticker)]
        metadata = members[
            members.cik.notna()
            & members.sic.str.fullmatch(r"\d{4}", na=False)
            & ~members.sic.str.startswith("6", na=False)
            & ~members.company_type.isin(["BANK", "INSURANCE", "REIT", "INDEX"])
        ].assign(sic_group=lambda frame: frame.sic.str[:2])
        spy = companies.index[companies.ticker.eq("SPY")].item()
        ids = [*metadata.index, spy]
        placeholders = ",".join("?" for _ in ids)
        bars = pd.read_sql_query(
            "SELECT company_id, date, open, close, adjusted_close, volume FROM daily_prices "
            f"WHERE company_id IN ({placeholders}) AND date BETWEEN ? AND ? "
            "AND source='ALPACA' AND open > 0 AND close > 0 AND adjusted_close > 0",
            source,
            params=(*ids, PRICE_START, END),
            parse_dates=["date"],
        )
        financials = pd.read_sql_query(
            "SELECT company_id, period_end, publication_date, roa_adjusted, eps_adjusted "
            f"FROM adjusted_metrics WHERE company_id IN ({placeholders}) "
            "AND period_end >= '2016-01-01' AND publication_date <= ? "
            "AND calculation_basis IN ('TTM', 'ANNUAL') "
            "ORDER BY period_end, publication_date",
            source,
            params=(*ids, END),
            parse_dates=["period_end", "publication_date"],
        )
        splits = pd.read_sql_query(
            "SELECT company_id, action_date, adjustment_factor FROM corporate_actions "
            f"WHERE company_id IN ({placeholders}) AND action_date <= ? "
            "AND action_type='SPLIT' AND adjustment_factor > 0",
            source,
            params=(*ids, END),
            parse_dates=["action_date"],
        )

    calendar = xcals.get_calendar("XNYS")
    dates = calendar.sessions_in_range(PRICE_START, END)
    days = dates[dates >= START]
    bars["adjusted_open"] = bars.open * bars.adjusted_close / bars.close
    bars["dollar_volume"] = bars.close * bars.volume
    prices = bars.pivot(index="date", columns="company_id").reindex(dates)
    opens, closes, raw_close = prices.adjusted_open, prices.adjusted_close, prices.close
    adv = prices.dollar_volume.rolling(20, min_periods=20).mean()
    marks = opens.fillna(closes.ffill().shift())
    session_number = pd.Series(np.arange(len(dates)), index=dates)
    last_close_number = (
        closes.notna()
        .astype(float)
        .where(closes.notna())
        .mul(session_number, axis=0)
        .ffill()
        .shift()
    )
    staleness = last_close_number.rsub(session_number, axis=0)
    ratios = marks / marks.shift()
    spy_opens = opens.loc[days, spy]
    if spy_opens.isna().any():
        raise ValueError("Missing SPY open in TRAIN window")
    fills = set(days.to_series().groupby(days.to_period("M")).first())
    portfolios = {
        (variant, cost): Portfolio()
        for variant in ("equal_weight_eligible", "composite", *FACTOR_NAMES)
        for cost in COSTS_BPS_PER_SIDE
    }
    daily_values = {key: [] for key in portfolios}
    coverage = []
    for day in days:
        targets = {}
        if day in fills:
            signal = calendar.previous_session(day)
            pool = factors_for_signal(
                signal, closes, raw_close, adv, metadata, membership, financials, splits
            )
            targets = targets_for_signal(pool)
            coverage.append(len(pool))
        for (variant, cost), portfolio in portfolios.items():
            nav = portfolio.value_at_open(
                marks.loc[day],
                opens.loc[day],
                ratios.loc[day],
                staleness.loc[day],
                targets.get(variant),
                cost,
            )
            daily_values[variant, cost].append(nav)
    return {
        "scope": "TRAIN diagnostic from a survivor-tilted universe; no deployable evidence",
        "window": [str(days[0].date()), str(days[-1].date())],
        "holdings": HOLDINGS,
        "max_per_sic_group": MAX_PER_SIC_GROUP,
        "min_20_session_mean_dollar_volume": MIN_DOLLAR_VOLUME,
        "min_raw_close": MIN_PRICE,
        "execution": "prior close signal, next XNYS open proxy, costs per side on trades",
        "factors": [
            "TTM ROA",
            "TTM earnings yield in split-consistent units",
            "252-to-21 session total return",
        ],
        "rotations": len(coverage),
        "eligible_count_min_max": [min(coverage), max(coverage)],
        "membership_tickers_mapped_total": [len(members), membership.ticker.nunique()],
        "spy_total_return": _summary(spy_opens / spy_opens.iloc[0], days),
        "variants": {
            f"{variant}_{cost}bps_per_side": _summary(values, days, portfolios[variant, cost])
            for (variant, cost), values in daily_values.items()
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = run(args.database)
    with args.database.open("rb") as db:
        report["input_sha256"] = hashlib.file_digest(db, "sha256").hexdigest()
    report["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
