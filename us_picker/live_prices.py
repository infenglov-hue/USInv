"""Build the public near-live price feed consumed by the PWA.

GitHub Pages cannot safely call Yahoo Finance from the browser because the
quote endpoints do not allow cross-origin requests. The scheduled workflow
runs this module server-side and publishes the resulting JSON on a separate
public branch.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

from us_picker.db.connection import get_session
from us_picker.db.schema import Company, DailyPrice, PortfolioSelection

logger = logging.getLogger(__name__)

YAHOO_SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark"
DEFAULT_BATCH_SIZE = 20
MIN_SUCCESS_RATIO = 0.60
MAX_ATTEMPTS = 3
LIVE_TICKER_SCHEMA_VERSION = 1
RECENT_PRICE_CALENDAR_DAYS = 21


def build_live_ticker_universe(session=None) -> dict:
    """Return a compact, auditable near-live ticker universe.

    KAP discovery can leave active company records with no tradable price
    history. They remain useful for audit, but querying them from Yahoo every
    30 minutes wastes runtime. Keep active names with a recent print, always
    retain open positions, and add XU100.
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
                Company.ticker != "XU100",
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
                and row.ticker != "XU100"
                and row.id in include_ids
                and (bool(row.is_active) or row.id in open_ids)
            ]
    finally:
        if owns_session:
            session.close()

    tickers = list(dict.fromkeys([*tickers, "XU100"]))
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
    """Return the compact tradable/recent ticker universe plus XU100."""
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


def _yahoo_symbol(ticker: str) -> str:
    ticker = ticker.strip().upper()
    return "XU100.IS" if ticker == "XU100" else f"{ticker}.IS"


def _local_ticker(symbol: str) -> str:
    symbol = symbol.strip().upper()
    return "XU100" if symbol == "XU100.IS" else symbol.removesuffix(".IS")


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _request_batch(
    http: requests.Session,
    symbols: list[str],
) -> dict:
    params = {
        "symbols": ",".join(symbols),
        "range": "1d",
        "interval": "1d",
    }
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = http.get(
                YAHOO_SPARK_URL,
                params=params,
                headers={"User-Agent": "Mozilla/5.0 MobileInv/1.0"},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Yahoo response is not a JSON object")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(attempt)
    raise RuntimeError(f"Yahoo batch request failed: {last_error}") from last_error


def build_live_price_feed(
    tickers: Iterable[str],
    *,
    http: requests.Session | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    generated_at: datetime | None = None,
) -> dict:
    """Fetch and normalize Yahoo prices for the requested local tickers."""
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

    client = http or requests.Session()
    symbols = [_yahoo_symbol(ticker) for ticker in normalized]
    prices: dict[str, dict] = {}
    failures: list[str] = []

    for symbol_batch in _chunks(symbols, batch_size):
        try:
            payload = _request_batch(client, symbol_batch)
            results = payload.get("spark", {}).get("result", [])
            if not isinstance(results, list):
                results = []
        except RuntimeError as exc:
            logger.error("Live price batch failed for %s: %s", symbol_batch, exc)
            failures.extend(_local_ticker(symbol) for symbol in symbol_batch)
            continue

        returned_symbols: set[str] = set()
        for item in results:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            returned_symbols.add(symbol)
            responses = item.get("response")
            if not isinstance(responses, list) or not responses:
                continue
            first = responses[0] if isinstance(responses[0], dict) else {}
            meta = first.get("meta") if isinstance(first.get("meta"), dict) else {}
            price = meta.get("regularMarketPrice")
            quote_epoch = meta.get("regularMarketTime")
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            try:
                quote_epoch = int(quote_epoch)
                quote_time = datetime.fromtimestamp(
                    quote_epoch,
                    tz=timezone.utc,
                ).isoformat()
            except (TypeError, ValueError, OSError):
                quote_epoch = None
                quote_time = None

            ticker = _local_ticker(symbol)
            prices[ticker] = {
                "price": price,
                "quote_time": quote_time,
                "quote_epoch": quote_epoch,
                "currency": meta.get("currency") or "TRY",
                "exchange": meta.get("exchangeName") or "IST",
            }

        failures.extend(
            _local_ticker(symbol)
            for symbol in symbol_batch
            if symbol not in returned_symbols or _local_ticker(symbol) not in prices
        )

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
        "source": "Yahoo Finance",
        "delay_notice": "BIST quotes may be delayed by approximately 15 minutes.",
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
