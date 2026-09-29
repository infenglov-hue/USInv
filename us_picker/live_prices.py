"""Build the public near-live price feed consumed by the PWA.

The PWA cannot hold market-data credentials, so a scheduled workflow fetches
quotes server-side (Alpaca, free IEX feed) and publishes the JSON on a
separate public branch.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from us_picker.db.connection import get_session
from us_picker.db.schema import Company, DailyPrice, PortfolioSelection

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100
MIN_SUCCESS_RATIO = 0.60
MAX_ATTEMPTS = 3
LIVE_TICKER_SCHEMA_VERSION = 1
RECENT_PRICE_CALENDAR_DAYS = 21


def build_live_ticker_universe(session=None) -> dict:
    """Return a compact, auditable near-live ticker universe.

    KAP discovery can leave active company records with no tradable price
    history. They remain useful for audit, but querying them from the quote API every
    30 minutes wastes runtime. Keep active names with a recent print, always
    retain open positions, and add the SPY benchmark.
    """
    owns_session = session is None
    session = session or get_session()
    latest_price_date = None
    cutoff = None
    try:
        latest_price_date = (
            session.query(DailyPrice.date)
            .join(Company, Company.id == DailyPrice.company_id)
            .filter(
                Company.ticker != "SPY",
                DailyPrice.close.isnot(None),
                DailyPrice.close > 0,
            )
            .order_by(DailyPrice.date.desc())
            .limit(1)
            .scalar()
        )
        cutoff = (
            latest_price_date - timedelta(days=RECENT_PRICE_CALENDAR_DAYS)
            if latest_price_date is not None
            else None
        )
        recent_ids: set[int] = set()
        if cutoff is not None:
            recent_ids = {
                int(row[0])
                for row in (
                    session.query(DailyPrice.company_id)
                    .filter(
                        DailyPrice.date >= cutoff,
                        DailyPrice.close.isnot(None),
                        DailyPrice.close > 0,
                    )
                    .distinct()
                    .all()
                )
            }
        open_ids = {
            int(row[0])
            for row in (
                session.query(PortfolioSelection.company_id)
                .filter(PortfolioSelection.exit_date.is_(None))
                .distinct()
                .all()
            )
        }
        include_ids = recent_ids | open_ids
        rows = (
            session.query(Company.id, Company.ticker, Company.is_active)
            .order_by(Company.ticker)
            .all()
        )
        if latest_price_date is None:
            tickers = [
                str(row.ticker).strip().upper()
                for row in rows
                if row.ticker and bool(row.is_active)
            ]
        else:
            tickers = [
                str(row.ticker).strip().upper()
                for row in rows
                if row.ticker
                and row.ticker != "SPY"
                and row.id in include_ids
                and (bool(row.is_active) or row.id in open_ids)
            ]
    finally:
        if owns_session:
            session.close()

    tickers = list(dict.fromkeys([*tickers, "SPY"]))
    return {
        "schema_version": LIVE_TICKER_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "latest_price_date": (
            latest_price_date.isoformat()
            if isinstance(latest_price_date, date)
            else None
        ),
        "recent_price_cutoff": cutoff.isoformat() if cutoff is not None else None,
        "ticker_count": len(tickers),
        "tickers": tickers,
    }


def get_active_tickers() -> list[str]:
    """Return the compact tradable/recent ticker universe plus SPY."""
    return list(build_live_ticker_universe()["tickers"])


def write_live_ticker_universe(
    output_path: str | Path,
    *,
    session=None,
) -> dict:
    """Atomically write the compact ticker contract consumed by CI."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_live_ticker_universe(session=session)
    temp_path = output.with_suffix(output.suffix + ".tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(output)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return payload


def load_live_tickers(path: str | Path) -> list[str]:
    """Load and validate a published live-ticker contract."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != LIVE_TICKER_SCHEMA_VERSION:
        raise ValueError("Unsupported live ticker schema")
    tickers = payload.get("tickers")
    if not isinstance(tickers, list):
        raise ValueError("Live ticker payload has no tickers list")
    normalized = [
        str(value).strip().upper()
        for value in tickers
        if str(value).strip()
    ]
    if not normalized:
        raise ValueError("Live ticker payload is empty")
    return list(dict.fromkeys(normalized))


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _parse_quote_time(raw) -> tuple[str | None, int | None]:
    if not raw:
        return None, None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None, None
    ts = ts.astimezone(timezone.utc)
    return ts.isoformat(), int(ts.timestamp())


def build_live_price_feed(
    tickers: Iterable[str],
    *,
    client=None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    generated_at: datetime | None = None,
) -> dict:
    """Fetch latest trades from Alpaca (free IEX feed) for the tickers.

    ``client`` is anything with ``fetch_snapshots(symbols, feed=...)``;
    defaults to :class:`AlpacaClient`.  The JSON contract is unchanged from
    BIST Picker so the PWA reads it the same way.
    """
    normalized_values: list[str] = []
    for ticker in tickers:
        if ticker is None:
            continue
        text = str(ticker).strip().upper()
        if text:
            normalized_values.append(text)
    normalized = list(dict.fromkeys(normalized_values))
    if not normalized:
        raise ValueError("No tickers supplied for live price export")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    if client is None:
        from us_picker.data.sources.alpaca import AlpacaClient

        client = AlpacaClient(feed="iex")

    prices: dict[str, dict] = {}
    failures: list[str] = []
    for batch in _chunks(normalized, batch_size):
        snapshots = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                snapshots = client.fetch_snapshots(batch, feed="iex")
                break
            except Exception as exc:  # transport errors retry, then give up
                logger.error("Live price batch failed (%s) for %s: %s", attempt, batch[0], exc)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(attempt)
        if snapshots is None:
            failures.extend(batch)
            continue
        for ticker in batch:
            snap = snapshots.get(ticker) or {}
            trade = snap.get("latestTrade") or {}
            price = trade.get("p")
            try:
                price = float(price)
            except (TypeError, ValueError):
                failures.append(ticker)
                continue
            if price <= 0:
                failures.append(ticker)
                continue
            quote_time, quote_epoch = _parse_quote_time(trade.get("t"))
            prices[ticker] = {
                "price": price,
                "quote_time": quote_time,
                "quote_epoch": quote_epoch,
                "currency": "USD",
                "exchange": "IEX",
            }

    success_ratio = len(prices) / len(normalized)
    if success_ratio < MIN_SUCCESS_RATIO:
        raise RuntimeError(
            "Live price export rejected: "
            f"{len(prices)}/{len(normalized)} quotes succeeded"
        )

    generated = generated_at or datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "generated_at": generated.astimezone(timezone.utc).isoformat(),
        "source": "Alpaca (IEX)",
        "delay_notice": "Last IEX trade; thin names can lag the consolidated tape.",
        "requested_count": len(normalized),
        "success_count": len(prices),
        "failed_tickers": sorted(set(failures)),
        "prices": prices,
    }


def write_live_price_feed(
    output_path: str | Path,
    *,
    tickers: Iterable[str] | None = None,
) -> dict:
    """Build the active-universe feed and atomically write it to disk."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_live_price_feed(tickers or get_active_tickers())
    temp_path = output.with_suffix(output.suffix + ".tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(output)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return payload
