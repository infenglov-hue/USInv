"""Strict Tiingo bulk symbol-lifecycle evidence for listing conflict checks."""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

from usinv.data.edgar.securities import SecurityMasterError, normalize_exchange, normalize_ticker
from usinv.data.prices.base import PricePayloadError

TIINGO_SUPPORTED_TICKERS_URL: Final = (
    "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
)
TIINGO_SUPPORTED_HEADER: Final = (
    "ticker",
    "exchange",
    "assetType",
    "priceCurrency",
    "startDate",
    "endDate",
)


@dataclass(frozen=True, slots=True)
class TiingoLifecycleRow:
    ticker: str
    exchange: str
    asset_type: str
    price_currency: str
    start_date: date
    end_date: date | None
    row_number: int


@dataclass(frozen=True, slots=True)
class TiingoLifecycleSnapshot:
    source_sha256: str
    rows: tuple[TiingoLifecycleRow, ...]

    def inactive_evidence(
        self,
        ticker: str,
        exchange: str,
        session: date,
    ) -> tuple[str, ...]:
        """Prove that every matching USD stock series ended before the session."""
        try:
            canonical_ticker = normalize_ticker(ticker)
            canonical_exchange = normalize_exchange(exchange)
        except SecurityMasterError:
            return ()
        tiingo_exchange = "AMEX" if canonical_exchange == "NYSEAMERICAN" else canonical_exchange
        matches = tuple(
            row
            for row in self.rows
            if row.ticker == canonical_ticker
            and row.exchange == tiingo_exchange
            and row.asset_type.casefold() == "stock"
            and row.price_currency == "USD"
        )
        if not matches or any(row.end_date is None or row.end_date >= session for row in matches):
            return ()
        return tuple(f"tiingo-supported://{self.source_sha256}/{row.row_number}" for row in matches)

    def non_stock_evidence(
        self,
        ticker: str,
        exchange: str,
    ) -> tuple[str, ...]:
        """Prove that every exact USD vendor series is an out-of-scope asset type."""
        try:
            canonical_ticker = normalize_ticker(ticker)
            canonical_exchange = normalize_exchange(exchange)
        except SecurityMasterError:
            return ()
        tiingo_exchange = "AMEX" if canonical_exchange == "NYSEAMERICAN" else canonical_exchange
        matches = tuple(
            row
            for row in self.rows
            if row.ticker == canonical_ticker
            and row.exchange == tiingo_exchange
            and row.price_currency == "USD"
        )
        if not matches or any(row.asset_type.casefold() == "stock" for row in matches):
            return ()
        return tuple(f"tiingo-supported://{self.source_sha256}/{row.row_number}" for row in matches)


def parse_tiingo_lifecycle_zip(payload: bytes) -> TiingoLifecycleSnapshot:
    """Parse the daily bulk file without accepting alternate archive layouts or schemas."""
    source_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.namelist() != ["supported_tickers.csv"]:
                raise PricePayloadError("Tiingo lifecycle ZIP layout drifted")
            raw = archive.read("supported_tickers.csv")
    except (OSError, zipfile.BadZipFile) as exc:
        raise PricePayloadError("Tiingo lifecycle response is not a valid ZIP") from exc
    try:
        stream = io.StringIO(raw.decode("utf-8-sig"), newline="")
    except UnicodeDecodeError as exc:
        raise PricePayloadError("Tiingo lifecycle CSV is not UTF-8") from exc
    reader = csv.DictReader(stream)
    if tuple(reader.fieldnames or ()) != TIINGO_SUPPORTED_HEADER:
        raise PricePayloadError("Tiingo lifecycle CSV schema drifted")
    rows: list[TiingoLifecycleRow] = []
    for row_number, item in enumerate(reader, start=2):
        if None in item or any(value is None for value in item.values()):
            raise PricePayloadError("Tiingo lifecycle CSV row width drifted")
        ticker = item["ticker"].strip().upper()
        exchange = item["exchange"].strip().upper()
        asset_type = item["assetType"].strip()
        currency = item["priceCurrency"].strip().upper()
        # Tiingo's published bulk contract contains a small number of rows with
        # no exchange.  Preserve them, but they can never corroborate an exact
        # exchange-qualified listing in ``inactive_evidence``.
        if not ticker or not asset_type or not currency:
            raise PricePayloadError("Tiingo lifecycle CSV contains an empty key field")
        try:
            start = date.fromisoformat(item["startDate"].strip())
            end_text = item["endDate"].strip()
            end = date.fromisoformat(end_text) if end_text else None
        except ValueError as exc:
            raise PricePayloadError("Tiingo lifecycle CSV contains an invalid date") from exc
        if end is not None and end < start:
            raise PricePayloadError("Tiingo lifecycle interval is reversed")
        rows.append(
            TiingoLifecycleRow(
                ticker,
                exchange,
                asset_type,
                currency,
                start,
                end,
                row_number,
            )
        )
    if not rows:
        raise PricePayloadError("Tiingo lifecycle CSV is empty")
    return TiingoLifecycleSnapshot(source_sha256, tuple(rows))


def read_tiingo_lifecycle_zip(path: str | Path) -> TiingoLifecycleSnapshot:
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise PricePayloadError("Tiingo lifecycle ZIP cannot be read") from exc
    return parse_tiingo_lifecycle_zip(payload)
