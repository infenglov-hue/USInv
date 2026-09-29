"""Data fetcher orchestrator for the US picker.

Same contract as BIST Picker's fetcher (the CLI, cleaning, scoring and
backtest stages are unchanged); only the sources differ:

| BIST Picker        | US picker                                        |
|--------------------|--------------------------------------------------|
| IsYatirim prices   | Alpaca daily bars (raw + total-return adjusted)  |
| IsYatirim/KAP list | SEC ticker list x Alpaca tradable assets         |
| IsYatirim tables   | SEC companyfacts -> IsYatirim-shaped statements  |
| BIST 100 list      | S&P 500 membership intervals                     |
| TCMB macro         | FRED (fed funds, 10y, CPI, breakevens, HY OAS)   |
| XU100 benchmark    | SPY total return                                 |
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from sqlalchemy.orm import Session

from us_picker.classification.sic import is_excluded_sic
from us_picker.data.sources import fred as fred_series
from us_picker.data.sources.sec_statements import build_statements
from us_picker.data.sources.sp500 import members_on, normalize_ticker
from us_picker.db.schema import (
    Company,
    CorporateAction,
    CpiHistory,
    DailyPrice,
    FinancialStatement,
    IndexMembership,
    MacroRegime,
)
from us_picker.utils.rate_limiter import RateLimiter

logger = logging.getLogger("us_picker.data.fetcher")

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
_BENCHMARK_TICKER = "SPY"
_BENCHMARK_NAME = "SPDR S&P 500 ETF (total return)"
_INDEX_NAME = "SP500"
_LISTING_EXCHANGES = {"NYSE", "Nasdaq"}
_ALPACA_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
# Instruments that are not common stock of an operating company.
_NON_COMMON_NAME = re.compile(
    r"\b(warrants?|units?|rights?|preferred|depositary|notes due|debentures|"
    r"trust preferred|acquisition corp|% )\b",
    re.IGNORECASE,
)


def _load_settings() -> dict:
    """Load settings.yaml configuration."""
    if _SETTINGS_PATH.exists():
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


class _PipeProgress:
    """Minimal Progress-like wrapper for pipe/subprocess mode."""

    def __init__(self, description: str) -> None:
        self._description = description
        self._tasks: dict[int, dict] = {}
        self._next_id = 0

    def _emit(self, msg: str) -> None:
        print(msg, flush=True)

    def __enter__(self) -> "_PipeProgress":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def add_task(self, name: str, total: int = 0) -> int:
        task_id = self._next_id
        self._next_id += 1
        self._tasks[task_id] = {"name": name, "completed": 0, "total": total}
        self._emit(f"{self._description} — {name}: 0/{total}")
        return task_id

    def advance(self, task_id: int, advance: int = 1) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task["completed"] += advance
        done, total = task["completed"], task["total"]
        if done == total or done % max(total // 10, 1) == 0:
            pct = int(done * 100 / total) if total else 0
            self._emit(f"{self._description} — {task['name']}: {done}/{total} ({pct}%)")

    def update(self, task_id: int, **kwargs) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        for key in ("completed", "total"):
            if key in kwargs:
                task[key] = kwargs[key]


def _alpaca_symbol(sec_ticker: str) -> str:
    return normalize_ticker(sec_ticker)


class DataFetcher:
    """Orchestrates data fetching from all sources into the database.

    Network clients are created lazily so constructing a fetcher (tests,
    dry runs) never requires credentials.
    """

    def __init__(
        self,
        session: Session,
        console: Optional[Console] = None,
        *,
        sec_client=None,
        alpaca_client=None,
        fred_client=None,
    ) -> None:
        self._session = session
        self._console = console or Console()
        settings = _load_settings()
        self._rate_limits = settings.get("rate_limits", {})
        self._fetch_settings = settings.get("fetch", {})
        self._universe_settings = settings.get("universe", {})
        self._sec = sec_client
        self._alpaca = alpaca_client
        self._fred = fred_client

    # -- lazy clients --------------------------------------------------------

    @property
    def sec(self):
        if self._sec is None:
            from us_picker.data.sources.sec import SECClient

            self._sec = SECClient(
                rate_limiter=RateLimiter(
                    min_delay=float(self._rate_limits.get("sec_delay_sec", 0.12)), name="sec"
                )
            )
        return self._sec

    @property
    def alpaca(self):
        if self._alpaca is None:
            from us_picker.data.sources.alpaca import AlpacaClient

            self._alpaca = AlpacaClient(
                rate_limiter=RateLimiter(
                    min_delay=float(self._rate_limits.get("alpaca_delay_sec", 0.31)),
                    name="alpaca",
                )
            )
        return self._alpaca

    @property
    def fred(self):
        if self._fred is None:
            from us_picker.data.sources.fred import FREDClient

            self._fred = FREDClient()
        return self._fred

    # -- universe ------------------------------------------------------------

    def fetch_universe(self) -> dict:
        """Refresh the investable universe on ``companies``.

        Candidates are SEC registrants listed on NYSE/Nasdaq that Alpaca can
        trade, minus non-common instruments and non-operating SIC codes.  A
        liquidity screen over the last month keeps the pool to names a
        five-stock portfolio can actually trade; current S&P 500 members are
        always kept.  Names already in the pool stay unless their liquidity
        falls below ``keep_ratio`` of the entry bar (hysteresis).

        Returns:
            Stats dict: {total, new, updated, delisted, bist100_count}.
        """
        self._console.print("[bold]Fetching company universe...[/bold]")
        stats = {"total": 0, "new": 0, "updated": 0, "delisted": 0, "bist100_count": 0}

        cfg = self._universe_settings
        min_price = float(cfg.get("min_price_usd", 5.0))
        min_adv = float(cfg.get("min_avg_dollar_volume_usd", 20_000_000))
        keep_ratio = float(cfg.get("keep_ratio", 0.5))
        lookback = int(cfg.get("liquidity_lookback_days", 30))

        from us_picker.data.sources.sp500 import fetch_intervals

        intervals = fetch_intervals()
        sp500_now = members_on(intervals, date.today())
        self._store_index_intervals(intervals)

        sec_df = self.sec.fetch_company_tickers()
        if sec_df.empty:
            raise RuntimeError("SEC ticker list came back empty; refusing to rebuild universe")
        sec_df = sec_df[sec_df["exchange"].isin(_LISTING_EXCHANGES)].copy()
        sec_df["symbol"] = sec_df["ticker"].map(_alpaca_symbol)
        sec_df = sec_df.drop_duplicates("symbol")

        assets = {
            a["symbol"]: a
            for a in self.alpaca.fetch_assets(status="active")
            if a.get("tradable") and a.get("exchange") in _ALPACA_EXCHANGES
        }
        candidates = sec_df[sec_df["symbol"].isin(assets)]
        candidates = candidates[
            ~candidates["symbol"].map(lambda s: bool(_NON_COMMON_NAME.search(assets[s].get("name") or "")))
        ]

        existing = {c.ticker: c for c in self._session.query(Company).all()}
        liquidity = self._recent_liquidity(list(candidates["symbol"]), lookback)

        kept: list[tuple[pd.Series, dict]] = []
        for row in candidates.itertuples(index=False):
            symbol = row.symbol
            liq = liquidity.get(symbol)
            in_pool = symbol in existing and existing[symbol].is_active
            bar = min_adv * (keep_ratio if in_pool else 1.0)
            liquid = liq is not None and liq["price"] >= min_price and liq["adv"] >= bar
            if liquid or symbol in sp500_now:
                kept.append((row, liq or {}))

        # SIC / profile for names we have not classified yet.
        need_profile = [
            row for row, _ in kept
            if symbol_missing_profile(existing.get(row.symbol), int(row.cik))
        ]
        profiles = self._fetch_profiles([int(r.cik) for r in need_profile])

        seen: set[str] = set()
        for row, _liq in kept:
            symbol = row.symbol
            company = existing.get(symbol)
            profile = profiles.get(int(row.cik), {})
            sic = profile.get("sic") if profile else (company.sic if company else None)
            if sic and is_excluded_sic(sic):
                continue
            is_member = symbol in sp500_now
            if company is None:
                company = Company(ticker=symbol, is_active=True)
                self._session.add(company)
                existing[symbol] = company
                stats["new"] += 1
            else:
                stats["updated"] += 1
            company.cik = int(row.cik)
            company.exchange = row.exchange
            company.name = (profile.get("name") if profile else None) or company.name or row.name
            if profile:
                company.sic = sic
                company.sector_bist = profile.get("sic_description") or company.sector_bist
            company.free_float_pct = 100.0  # no free-float concept on US listings
            company.is_bist100 = is_member
            company.is_active = True
            seen.add(symbol)
            if is_member:
                stats["bist100_count"] += 1

        for symbol, company in existing.items():
            if symbol == _BENCHMARK_TICKER:
                continue
            if symbol not in seen and company.is_active:
                company.is_active = False
                company.is_bist100 = False
                stats["delisted"] += 1

        self._session.flush()
        stats["total"] = len(seen)
        self._console.print(
            f"  Universe: {stats['total']} companies "
            f"({stats['new']} new, {stats['updated']} updated, "
            f"{stats['delisted']} dropped, {stats['bist100_count']} in S&P 500)"
        )
        return stats

    def _recent_liquidity(self, symbols: list[str], lookback_days: int) -> dict[str, dict]:
        end = date.today()
        start = end - timedelta(days=lookback_days + 10)
        bars = self.alpaca.fetch_daily_bars(symbols, start, end, adjustment="raw")
        result: dict[str, dict] = {}
        if bars.empty:
            return result
        bars = bars.dropna(subset=["close", "volume"])
        for symbol, group in bars.groupby("symbol"):
            tail = group.sort_values("date").tail(20)
            if tail.empty:
                continue
            result[symbol] = {
                "price": float(tail["close"].iloc[-1]),
                "adv": float((tail["close"] * tail["volume"]).mean()),
            }
        return result

    def _fetch_profiles(self, ciks: list[int]) -> dict[int, dict]:
        from us_picker.data.sources.sec import company_profile

        profiles: dict[int, dict] = {}
        if not ciks:
            return profiles
        with self._progress_bar("SEC company profiles") as progress:
            task = progress.add_task("Profiles", total=len(ciks))
            for cik in ciks:
                try:
                    payload = self.sec.fetch_submissions(cik)
                    if payload:
                        profiles[cik] = company_profile(payload)
                except Exception as exc:  # one bad profile must not sink the run
                    logger.warning("SEC submissions failed for CIK %s: %s", cik, exc)
                progress.advance(task)
        return profiles

    def _store_index_intervals(self, intervals) -> None:
        self._session.query(IndexMembership).filter(
            IndexMembership.index_name == _INDEX_NAME
        ).delete(synchronize_session=False)
        self._session.add_all(
            IndexMembership(
                index_name=_INDEX_NAME,
                ticker=i.ticker,
                start_date=i.start_date,
                end_date=i.end_date,
            )
            for i in intervals
        )
        self._session.flush()

    # -- prices --------------------------------------------------------------

    def fetch_prices(
        self,
        tickers: Optional[list[str]] = None,
        days_back: int = 1500,
    ) -> dict:
        """Fetch daily bars into ``daily_prices`` (raw close + total-return
        adjusted close) and refresh corporate actions for the same window.

        Alpaca back-adjusts the whole history whenever a split or dividend
        goes ex.  A symbol whose stored adjusted/raw ratio no longer matches
        the vendor's on the overlap day gets its full adjusted history
        rewritten; raw ``close`` is never modified.
        """
        ticker_list = tickers or self._get_active_tickers()
        stats = {"tickers_processed": 0, "rows_inserted": 0, "rows_skipped": 0,
                 "failed": [], "readjusted": 0}
        if not ticker_list:
            self._console.print("[yellow]No tickers to fetch prices for[/yellow]")
            return stats

        end_date = _last_complete_session_day()
        start_date = end_date - timedelta(days=days_back)
        history_start = date.fromisoformat(
            str(self._fetch_settings.get("price_history_start", "2016-01-01"))
        )
        start_date = max(start_date, history_start)
        companies = {
            c.ticker: c.id
            for c in self._session.query(Company).filter(Company.ticker.in_(ticker_list)).all()
        }

        chunk = 100
        with self._progress_bar("Fetching prices") as progress:
            task = progress.add_task("Prices", total=len(ticker_list))
            for i in range(0, len(ticker_list), chunk):
                batch = [t for t in ticker_list[i : i + chunk] if t in companies]
                if not batch:
                    progress.advance(task, len(ticker_list[i : i + chunk]))
                    continue
                try:
                    df = self.alpaca.fetch_price_history(batch, start_date, end_date)
                except Exception as exc:
                    logger.warning("Price fetch failed for %s..: %s", batch[0], exc)
                    stats["failed"].extend(batch)
                    progress.advance(task, len(batch))
                    continue
                got = set(df["symbol"]) if not df.empty else set()
                stats["failed"].extend(sorted(set(batch) - got))
                stale = self._stale_adjustment_symbols(df, companies)
                rows: list[dict] = []
                for symbol, group in df.groupby("symbol") if not df.empty else []:
                    stats["rows_inserted"] += self._prepare_prices_batch(
                        companies[symbol], group, rows
                    )
                self._bulk_insert_prices(rows)
                for symbol in stale:
                    self._rewrite_adjusted_history(companies[symbol], symbol, history_start, end_date)
                    stats["readjusted"] += 1
                stats["tickers_processed"] += len(batch)
                progress.advance(task, len(batch))

        self._console.print(
            f"  Prices: {stats['tickers_processed']} tickers, "
            f"{stats['rows_inserted']} rows fetched, {stats['readjusted']} re-adjusted"
        )
        if stats["failed"]:
            self._console.print(f"  [yellow]No bars: {len(stats['failed'])} tickers[/yellow]")

        try:
            stats["corporate_actions"] = self.fetch_corporate_actions(
                ticker_list, start=start_date - timedelta(days=7), end=end_date
            )
        except Exception as exc:
            logger.warning("Corporate action refresh failed: %s", exc)

        benchmark_stats = self.fetch_benchmark_prices(days_back=days_back)
        if benchmark_stats.get("rows_inserted"):
            stats["benchmark_rows_inserted"] = benchmark_stats["rows_inserted"]
        return stats

    def _stale_adjustment_symbols(self, df: pd.DataFrame, companies: dict[str, int]) -> set[str]:
        """Symbols whose stored adjustment factor diverged from the vendor's."""

        stale: set[str] = set()
        if df.empty:
            return stale
        for symbol, group in df.groupby("symbol"):
            first = group.sort_values("date").iloc[0]
            if not first["close"] or pd.isna(first.get("adjusted_close")):
                continue
            stored = (
                self._session.query(DailyPrice.close, DailyPrice.adjusted_close)
                .filter(DailyPrice.company_id == companies[symbol], DailyPrice.date == first["date"])
                .first()
            )
            if stored is None or not stored[0] or stored[1] is None:
                continue
            vendor_ratio = float(first["adjusted_close"]) / float(first["close"])
            stored_ratio = float(stored[1]) / float(stored[0])
            if abs(vendor_ratio / stored_ratio - 1.0) > 1e-4:
                stale.add(symbol)
        return stale

    def _rewrite_adjusted_history(self, company_id: int, symbol: str, start: date, end: date) -> None:
        adjusted = self.alpaca.fetch_daily_bars([symbol], start, end, adjustment="all")
        if adjusted.empty:
            return
        mapping = {row.date: row.close for row in adjusted.itertuples(index=False)}
        rows = (
            self._session.query(DailyPrice)
            .filter(DailyPrice.company_id == company_id, DailyPrice.date >= start)
            .all()
        )
        for price in rows:
            value = mapping.get(price.date)
            if value is not None:
                price.adjusted_close = float(value)
        self._session.commit()

    def fetch_corporate_actions(
        self, tickers: list[str], start: date, end: date
    ) -> dict:
        """Upsert splits and cash dividends into ``corporate_actions``."""

        actions = self.alpaca.fetch_corporate_actions(tickers, start, end)
        stats = {"actions": 0}
        if actions.empty:
            return stats
        companies = {
            c.ticker: c.id
            for c in self._session.query(Company).filter(Company.ticker.in_(tickers)).all()
        }
        existing = {
            (a.company_id, a.action_date, a.action_type)
            for a in self._session.query(CorporateAction)
            .filter(CorporateAction.action_date >= start)
            .all()
        }
        for row in actions.itertuples(index=False):
            cid = companies.get(row.symbol)
            if cid is None:
                continue
            key = (cid, row.action_date, row.action_type)
            if key in existing:
                continue
            self._session.add(
                CorporateAction(
                    company_id=cid,
                    action_date=row.action_date,
                    action_type=row.action_type,
                    adjustment_factor=row.adjustment_factor,
                    details_json=json.dumps(row.details, ensure_ascii=False),
                    source="ALPACA",
                )
            )
            existing.add(key)
            stats["actions"] += 1
        self._session.commit()
        if stats["actions"]:
            from us_picker.utils.splits import invalidate_split_cache

            invalidate_split_cache()
        return stats

    # -- financials ----------------------------------------------------------

    def fetch_financials(self, tickers: Optional[list[str]] = None) -> dict:
        """Fetch SEC companyfacts and store IsYatirim-shaped statements.

        When ``US_PICKER_COMPANYFACTS_ZIP`` points at EDGAR's bulk
        companyfacts.zip, payloads are read from it instead of the API.
        """
        ticker_list = tickers or self._get_active_tickers()
        stats = {"tickers_processed": 0, "statements_inserted": 0, "failed": []}
        companies = [
            c
            for c in self._session.query(Company).filter(Company.ticker.in_(ticker_list)).all()
            if c.cik
        ]
        if not companies:
            self._console.print("[yellow]No tickers with a CIK to fetch financials for[/yellow]")
            return stats
        by_cik: dict[int, list[Company]] = {}
        for company in companies:
            by_cik.setdefault(int(company.cik), []).append(company)

        bulk_zip = os.environ.get("US_PICKER_COMPANYFACTS_ZIP", "").strip()
        with self._progress_bar("Fetching financials") as progress:
            task = progress.add_task("Financials", total=len(by_cik))
            if bulk_zip:
                from us_picker.data.sources.sec import iter_bulk_companyfacts

                done: set[int] = set()
                for cik, payload in iter_bulk_companyfacts(Path(bulk_zip), by_cik):
                    stats["statements_inserted"] += self._store_companyfacts(by_cik[cik], payload)
                    done.add(cik)
                    stats["tickers_processed"] += 1
                    progress.advance(task)
                    if stats["tickers_processed"] % 100 == 0:
                        self._session.commit()
                stats["failed"].extend(
                    c.ticker for cik in set(by_cik) - done for c in by_cik[cik]
                )
            else:
                def fetch_one(cik: int):
                    try:
                        return cik, self.sec.fetch_companyfacts(cik), None
                    except Exception as exc:
                        return cik, None, str(exc)

                workers = int(self._fetch_settings.get("sec_workers", 4))
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = [executor.submit(fetch_one, cik) for cik in by_cik]
                    for future in concurrent.futures.as_completed(futures):
                        cik, payload, error = future.result()
                        if error or not payload:
                            if error:
                                logger.warning("companyfacts failed for CIK %s: %s", cik, error)
                            stats["failed"].extend(c.ticker for c in by_cik[cik])
                        else:
                            stats["statements_inserted"] += self._store_companyfacts(
                                by_cik[cik], payload
                            )
                        stats["tickers_processed"] += 1
                        progress.advance(task)
                        if stats["tickers_processed"] % 50 == 0:
                            self._session.commit()
        self._session.commit()
        self._console.print(
            f"  Financials: {stats['tickers_processed']} filers, "
            f"{stats['statements_inserted']} statements inserted/updated"
        )
        if stats["failed"]:
            self._console.print(f"  [yellow]Failed: {len(stats['failed'])} tickers[/yellow]")
        return stats

    def _store_companyfacts(self, companies: list[Company], payload: dict) -> int:
        try:
            statements = build_statements(payload)
        except Exception as exc:
            logger.warning("Statement build failed for %s: %s", companies[0].ticker, exc)
            return 0
        coverage_start = date.fromisoformat(
            str(self._fetch_settings.get("price_history_start", "2016-01-01"))
        )
        for company in companies:
            self._record_inferred_splits(company.id, statements, coverage_start)
        return sum(self._upsert_financials(c.id, statements) for c in companies)

    def _record_inferred_splits(
        self, company_id: int, statements: dict, coverage_start: date
    ) -> int:
        """Add splits that predate the vendor's corporate-action history.

        Alpaca's split records start in 2016, but statements go back to 2009.
        A pre-2016 split shows up as a quarter-over-quarter share-count jump
        by an exact split ratio; it is stored as a ``SEC_INFERRED`` split so
        base-unit share counts (utils/splits.py) stay continuous.
        """
        observations = []
        for period_end, row in sorted((statements.get("BALANCE") or {}).items()):
            for item in row["items"]:
                if item.get("item_code") == "2OA" and item.get("value"):
                    as_of_raw = item.get("as_of")
                    as_of = date.fromisoformat(as_of_raw) if as_of_raw else period_end
                    observations.append((as_of, float(item["value"])))
        observations.sort()
        known = {
            a.action_date
            for a in self._session.query(CorporateAction)
            .filter(CorporateAction.company_id == company_id, CorporateAction.action_type == "SPLIT")
            .all()
        }
        added = 0
        for i in range(1, len(observations)):
            prev_date, prev_shares = observations[i - 1]
            cur_date, cur_shares = observations[i]
            if cur_date > coverage_start or prev_shares <= 0:
                continue
            ratio = infer_split_ratio(cur_shares / prev_shares)
            if ratio is None or cur_date in known:
                continue
            # A real split moves the count to a new level that persists; two
            # share concepts alternating in the filings (class A only vs total,
            # cover vs weighted) flip back and are not splits.
            if not _level_is_stable(observations, i):
                continue
            self._session.add(
                CorporateAction(
                    company_id=company_id,
                    action_date=cur_date,
                    action_type="SPLIT",
                    adjustment_factor=ratio,
                    details_json=json.dumps({"inferred_from": "sec_share_count", "previous_as_of": prev_date.isoformat()}),
                    source="SEC_INFERRED",
                )
            )
            known.add(cur_date)
            added += 1
        if added:
            self._session.flush()
            from us_picker.utils.splits import invalidate_split_cache

            invalidate_split_cache()
        return added

    def fetch_recent_financials(self, tickers: Optional[list[str]] = None) -> dict:
        """Refresh only filers with a new 10-K/10-Q in the last ``days``.

        Cheap enough for the scheduled financial-refresh workflow: EDGAR's
        daily form index tells us who filed, and only those companyfacts are
        re-downloaded.
        """
        days = int(self._fetch_settings.get("recent_filing_days", 10))
        since = date.today() - timedelta(days=days)
        filers = self.sec.recent_periodic_filers(since)
        query = self._session.query(Company).filter(Company.is_active.is_(True))
        if tickers:
            query = query.filter(Company.ticker.in_(tickers))
        targets = [c.ticker for c in query.all() if c.cik and int(c.cik) in filers]
        self._console.print(
            f"  EDGAR: {len(filers)} periodic filers since {since}; "
            f"{len(targets)} in the universe"
        )
        if not targets:
            return {"tickers_processed": 0, "statements_inserted": 0, "failed": []}
        return self.fetch_financials(targets)

    def fetch_history(self, tickers: Optional[list[str]] = None, num_years: int = 10) -> dict:
        """companyfacts already carries the full filing history."""
        return self.fetch_financials(tickers)

    # -- macro ---------------------------------------------------------------

    def fetch_macro(self) -> dict:
        """FRED macro history into ``macro_regime`` / ``cpi_history``.

        Column mapping (units follow BIST Picker: decimals, except CDS bps):
        policy_rate_pct = fed funds, bond_yield_10y_pct = 10y Treasury,
        cpi_yoy_pct = CPI YoY visible from the ~15th of the next month,
        inflation_expectation_24m_pct = 5y5y forward breakeven,
        turkey_cds_5y = Baa-10y corporate credit spread in bps (credit stress),
        equity_risk_premium_pct = Damodaran US ERP (today's row only).
        """
        self._console.print("[bold]Fetching macro data...[/bold]")
        start = date.fromisoformat(str(self._fetch_settings.get("macro_history_start", "2014-01-01")))
        series = {}
        for name, series_id in (
            ("policy", fred_series.POLICY_RATE),
            ("ten_year", fred_series.TEN_YEAR),
            ("breakeven", fred_series.LONG_RUN_INFLATION),
            ("credit_spread", fred_series.CREDIT_SPREAD),
        ):
            try:
                series[name] = self.fred.fetch_series(series_id, start)
            except Exception as exc:
                logger.warning("FRED %s failed: %s", series_id, exc)
                series[name] = pd.Series(dtype=float)
        try:
            cpi = self.fred.fetch_series(fred_series.CPI, start - timedelta(days=400))
        except Exception as exc:
            logger.warning("FRED CPI failed: %s", exc)
            cpi = pd.Series(dtype=float)

        cpi_yoy = _cpi_yoy_by_availability(cpi)
        frame = pd.DataFrame(
            {
                "policy_rate_pct": series["policy"] / 100.0,
                "bond_yield_10y_pct": series["ten_year"] / 100.0,
                "inflation_expectation_24m_pct": series["breakeven"] / 100.0,
                "turkey_cds_5y": series["credit_spread"] * 100.0,
            }
        )
        if not cpi_yoy.empty:
            frame = frame.join(cpi_yoy.rename("cpi_yoy_pct"), how="outer")
        # Series publish on different calendars (fed funds daily incl.
        # weekends, OAS/10y on trading days, CPI monthly). Carry each last
        # known value forward so every row is complete; only past values move
        # forward, so this stays point-in-time.
        frame = frame.sort_index().ffill()
        frame = frame[frame.index >= start]

        existing = {row.date: row for row in self._session.query(MacroRegime).filter(MacroRegime.date >= start).all()}
        written = 0
        for day, values in frame.iterrows():
            row = existing.get(day)
            if row is None:
                row = MacroRegime(date=day)
                self._session.add(row)
            for column, value in values.items():
                if pd.notna(value):
                    setattr(row, column, float(value))
            written += 1
        self._session.flush()

        erp = None
        try:
            from us_picker.data.sources.damodaran import fetch_us_erp

            payload = fetch_us_erp()
            erp = payload.equity_risk_premium_pct if payload else None
        except Exception as exc:
            logger.warning("Damodaran US ERP fetch failed: %s", exc)
        if erp is not None:
            # Attach to the newest complete row rather than creating a
            # today-only row that would lack every other field.
            row = self._session.query(MacroRegime).order_by(MacroRegime.date.desc()).first()
            if row is None:
                row = MacroRegime(date=date.today())
                self._session.add(row)
            row.equity_risk_premium_pct = erp
            row.erp_source = "damodaran_html"
            self._session.flush()

        self._upsert_cpi_history(cpi)

        latest = frame.iloc[-1] if not frame.empty else pd.Series(dtype=float)
        stats = {
            "cpi_points": int(len(cpi)),
            "fx_points": 0,
            "macro_rows": written,
            "policy_rate": _float_or_none(latest.get("policy_rate_pct")),
            "inflation_rate": _float_or_none(frame["cpi_yoy_pct"].dropna().iloc[-1]) if "cpi_yoy_pct" in frame and frame["cpi_yoy_pct"].notna().any() else None,
            "damodaran_erp": erp,
        }
        pr = f"{stats['policy_rate']:.2%}" if stats["policy_rate"] is not None else "N/A"
        ir = f"{stats['inflation_rate']:.2%}" if stats["inflation_rate"] is not None else "N/A"
        er = f"{erp:.2%}" if erp is not None else "N/A (YAML fallback)"
        self._console.print(
            f"  Macro: {written} daily rows, fed funds={pr}, CPI YoY={ir}, ERP={er}"
        )
        return stats

    def _upsert_cpi_history(self, cpi_series: "pd.Series") -> None:
        """Idempotently upsert a CPI index series into cpi_history."""
        if cpi_series is None or cpi_series.empty:
            return
        existing = {row.date: row for row in self._session.query(CpiHistory).all()}
        for raw_date, raw_val in cpi_series.items():
            if pd.isna(raw_val) or float(raw_val) <= 0:
                continue
            d = pd.Timestamp(raw_date).date()
            row = existing.get(d)
            if row is None:
                self._session.add(CpiHistory(date=d, cpi_index=float(raw_val)))
            else:
                row.cpi_index = float(raw_val)
        self._session.flush()

    def fetch_insiders(self, tickers: Optional[list[str]] = None, days_back: int = 180) -> dict:
        """No insider feed in the US port yet (Form 4 parsing is future work)."""
        return {"tickers_processed": 0, "documents_found": 0, "failed": []}

    def fetch_all(
        self,
        tickers: Optional[list[str]] = None,
        limit: int = 0,
        price_days: Optional[int] = None,
    ) -> dict:
        """Run all fetch stages in order."""
        all_stats: dict = {}
        all_stats["universe"] = self.fetch_universe()
        self._session.commit()

        fetch_tickers = tickers
        if not fetch_tickers and limit > 0:
            fetch_tickers = self._get_active_tickers()[:limit]

        days_back = price_days or int(self._fetch_settings.get("price_history_days", 3800))
        all_stats["prices"] = self.fetch_prices(fetch_tickers, days_back=days_back)
        self._session.commit()

        all_stats["financials"] = self.fetch_financials(fetch_tickers)
        self._session.commit()

        all_stats["macro"] = self.fetch_macro()
        self._session.commit()

        self._print_summary(all_stats)
        return all_stats

    def validate_prices(self, sample_pct: float = 0.10) -> dict:
        """Single-vendor setup: nothing to cross-validate against yet."""
        return {}

    # -- benchmark / helpers -------------------------------------------------

    def fetch_benchmark_prices(self, days_back: int = 730) -> dict:
        """Refresh SPY daily bars (full history each run).

        The total-return adjusted close changes every time SPY goes
        ex-dividend, so the whole (small) history is upserted.  Stops at
        yesterday so a partial session bar is never stored as final.
        """
        company = self._ensure_benchmark_company()
        end_date = date.today() - timedelta(days=1)
        start_date = date.fromisoformat(
            str(self._fetch_settings.get("price_history_start", "2016-01-01"))
        )
        df = self.alpaca.fetch_price_history([_BENCHMARK_TICKER], start_date, end_date)
        if df.empty:
            logger.warning("Failed to refresh benchmark prices for %s", _BENCHMARK_TICKER)
            return {"rows_inserted": 0}
        batch_data: list[dict] = []
        count = self._prepare_prices_batch(company.id, df, batch_data)
        self._bulk_insert_prices(batch_data, update_existing=True)
        self._console.print(f"  Benchmark: {_BENCHMARK_TICKER} refreshed ({count} rows)")
        return {"rows_inserted": count}

    def _get_active_tickers(self) -> list[str]:
        companies = (
            self._session.query(Company.ticker)
            .filter(Company.is_active.is_(True))
            .order_by(Company.ticker)
            .all()
        )
        return [c.ticker for c in companies]

    def _get_or_create_company(self, ticker: str) -> Company:
        company = self._session.query(Company).filter(Company.ticker == ticker.upper()).first()
        if company:
            return company
        company = Company(ticker=ticker.upper(), is_active=True)
        self._session.add(company)
        self._session.flush()
        return company

    def _ensure_benchmark_company(self) -> Company:
        company = self._session.query(Company).filter(Company.ticker == _BENCHMARK_TICKER).first()
        if company is None:
            company = Company(ticker=_BENCHMARK_TICKER)
            self._session.add(company)
        company.name = _BENCHMARK_NAME
        company.company_type = "INDEX"
        company.is_active = False
        self._session.flush()
        return company

    def _prepare_prices_batch(self, company_id: int, price_df: pd.DataFrame, batch_list: list) -> int:
        if price_df.empty:
            return 0
        count = 0
        for row in price_df.itertuples(index=False):
            row_date = pd.Timestamp(row.date).date()
            volume = getattr(row, "volume", None)
            adjusted = getattr(row, "adjusted_close", None)
            batch_list.append({
                "company_id": company_id,
                "date": row_date,
                "open": _float_or_none(getattr(row, "open", None)),
                "high": _float_or_none(getattr(row, "high", None)),
                "low": _float_or_none(getattr(row, "low", None)),
                "close": _float_or_none(getattr(row, "close", None)),
                "volume": int(volume) if volume is not None and pd.notna(volume) else None,
                "adjusted_close": _float_or_none(adjusted),
                "source": getattr(row, "source", "ALPACA"),
            })
            count += 1
        return count

    def _bulk_insert_prices(self, batch_data: list, update_existing: bool = False):
        """Insert-or-ignore price rows; ``update_existing`` upserts instead."""
        if not batch_data:
            return
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        try:
            for i in range(0, len(batch_data), 2000):
                chunk = batch_data[i : i + 2000]
                stmt = sqlite_insert(DailyPrice).values(chunk)
                if update_existing:
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["company_id", "date"],
                        set_={
                            "open": stmt.excluded.open,
                            "high": stmt.excluded.high,
                            "low": stmt.excluded.low,
                            "close": stmt.excluded.close,
                            "volume": stmt.excluded.volume,
                            "adjusted_close": stmt.excluded.adjusted_close,
                            "source": stmt.excluded.source,
                        },
                    )
                else:
                    stmt = stmt.on_conflict_do_nothing(index_elements=["company_id", "date"])
                self._session.execute(stmt)
            self._session.commit()
        except Exception as e:
            logger.error(f"Bulk insert failed: {e}", exc_info=True)
            self._session.rollback()
            raise

    def _upsert_financials(self, company_id: int, fin_data: dict) -> int:
        """Upsert statements from ``build_statements`` output.

        ``fin_data`` maps statement type -> {period_end: {period_type,
        publication_date, items}}.  Values are as-first-filed, so an existing
        row only changes when a previously unknown item became visible; its
        publication date is never moved earlier.
        """
        inserted = 0
        existing = {
            (s.period_end, s.statement_type): s
            for s in self._session.query(FinancialStatement)
            .filter(FinancialStatement.company_id == company_id, FinancialStatement.version == 1)
            .all()
        }
        for stmt_type in ("INCOME", "BALANCE", "CASHFLOW"):
            for period_end, row in (fin_data.get(stmt_type) or {}).items():
                items = row["items"]
                if not any(item["value"] is not None for item in items):
                    continue
                if stmt_type == "BALANCE":
                    items = _normalize_share_units(self._session, company_id, items, period_end)
                data_json = json.dumps(items, ensure_ascii=False)
                current = existing.get((period_end, stmt_type))
                if current is not None:
                    if current.data_json != data_json:
                        current.data_json = data_json
                    if current.publication_date is None or row["publication_date"] > current.publication_date:
                        current.publication_date = row["publication_date"]
                    continue
                self._session.add(
                    FinancialStatement(
                        company_id=company_id,
                        period_end=period_end,
                        period_type=row["period_type"],
                        statement_type=stmt_type,
                        is_consolidated=True,
                        is_inflation_adj=False,
                        publication_date=row["publication_date"],
                        version=1,
                        data_json=data_json,
                    )
                )
                inserted += 1
        if inserted:
            self._session.flush()
        return inserted

    @staticmethod
    def _parse_period(period_str: str) -> tuple[Optional[date], Optional[str]]:
        """Parse 'YYYY/PP' (PP in 3/6/9/12) into (period_end, period_type)."""
        try:
            year_raw, period_raw = period_str.split("/")
            year, period = int(year_raw), int(period_raw)
        except (ValueError, AttributeError):
            return None, None
        mapping = {
            3: ("Q1", date(year, 3, 31)),
            6: ("Q2", date(year, 6, 30)),
            9: ("Q3", date(year, 9, 30)),
            12: ("ANNUAL", date(year, 12, 31)),
        }
        if period not in mapping:
            return None, None
        ptype, pend = mapping[period]
        return pend, ptype

    def _progress_bar(self, description: str) -> Progress:
        if os.environ.get("PIPE_MODE") == "1" or os.environ.get("CI"):
            return _PipeProgress(description)
        return Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=self._console,
        )

    def _print_summary(self, all_stats: dict) -> None:
        table = Table(title="Fetch Summary")
        table.add_column("Stage", style="bold")
        table.add_column("Detail")
        if "universe" in all_stats:
            u = all_stats["universe"]
            table.add_row("Universe", f"{u['total']} companies ({u['new']} new, {u['bist100_count']} S&P 500)")
        if "prices" in all_stats:
            p = all_stats["prices"]
            table.add_row("Prices", f"{p['tickers_processed']} tickers, {p['rows_inserted']} rows")
        if "financials" in all_stats:
            f = all_stats["financials"]
            table.add_row("Financials", f"{f['tickers_processed']} filers, {f['statements_inserted']} statements")
        if "macro" in all_stats:
            m = all_stats["macro"]
            pr = f"{m['policy_rate']:.2%}" if m.get("policy_rate") is not None else "N/A"
            table.add_row("Macro", f"{m.get('macro_rows', 0)} rows, fed funds={pr}")
        self._console.print()
        self._console.print(table)


def _last_complete_session_day() -> date:
    """Today once the New York close is final (16:20 ET), else yesterday.

    Rows are insert-only, so a partial intraday bar must never be stored.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("America/New_York"))
    if (now.hour, now.minute) >= (16, 20):
        return now.date()
    return now.date() - timedelta(days=1)


def symbol_missing_profile(company: Optional[Company], cik: int) -> bool:
    """True when a company still needs its SEC profile (SIC) fetched."""
    if company is None:
        return True
    return not company.sic or company.cik != cik


_SPLIT_RATIOS = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 100.0)


def infer_split_ratio(share_ratio: float, tolerance: float = 0.04) -> Optional[float]:
    """Return a split ratio (new/old) if ``share_ratio`` is one, else None."""
    if share_ratio <= 0:
        return None
    for ratio in _SPLIT_RATIOS:
        if abs(share_ratio / ratio - 1.0) <= tolerance:
            return ratio
        if abs(share_ratio * ratio - 1.0) <= tolerance:
            return 1.0 / ratio
    return None


def _level_is_stable(observations: list[tuple[date, float]], i: int, band: float = 0.15) -> bool:
    """True when the count before ``i`` and from ``i`` on are both steady."""
    before = observations[i - 2][1] if i >= 2 else None
    after = observations[i + 1][1] if i + 1 < len(observations) else None
    if after is None:
        return False
    prev, cur = observations[i - 1][1], observations[i][1]
    if abs(after / cur - 1.0) > band:
        return False
    if before is not None and abs(prev / before - 1.0) > band:
        return False
    return True


def _normalize_share_units(
    session: Session, company_id: int, items: list[dict], period_end: date
) -> list[dict]:
    """Store ``2OA`` in split-free base units (see utils/splits.py)."""
    from us_picker.utils.splits import to_base_shares

    out = []
    for item in items:
        if item.get("item_code") == "2OA" and item.get("value") is not None:
            as_of_raw = item.get("as_of")
            as_of = date.fromisoformat(as_of_raw) if as_of_raw else period_end
            item = dict(item)
            item["value"] = to_base_shares(session, company_id, float(item["value"]), as_of)
            item["unit"] = "split_base_shares"
        out.append(item)
    return out


def _float_or_none(value) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cpi_yoy_by_availability(cpi: pd.Series) -> pd.Series:
    """CPI YoY indexed by the date the print became public.

    BLS publishes month M's CPI around the middle of month M+1; the value is
    stamped on the 15th of M+1 so no scoring date sees it early.
    """
    if cpi is None or cpi.empty:
        return pd.Series(dtype=float)
    monthly = cpi.copy()
    monthly.index = pd.to_datetime(monthly.index)
    yoy = monthly / monthly.shift(12) - 1.0
    yoy = yoy.dropna()
    visible = [
        (ts + pd.offsets.MonthBegin(1) + pd.Timedelta(days=14)).date() for ts in yoy.index
    ]
    return pd.Series(yoy.values, index=visible, dtype=float)
