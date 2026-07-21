from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from usinv.data.prices.base import PricePayloadError
from usinv.data.tiingo_lifecycle import parse_tiingo_lifecycle_zip


def _zip(body: str, *, name: str = "supported_tickers.csv") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, body)
    return output.getvalue()


HEADER = "ticker,exchange,assetType,priceCurrency,startDate,endDate\n"


def test_tiingo_lifecycle_proves_only_a_pre_cutoff_end() -> None:
    snapshot = parse_tiingo_lifecycle_zip(
        _zip(
            HEADER
            + "OLD,NASDAQ,Stock,USD,2020-01-01,2025-01-02\n"
            + "LIVE,NYSE,Stock,USD,2020-01-01,2026-07-20\n"
        )
    )

    assert snapshot.inactive_evidence("OLD", "NASDAQ", date(2026, 7, 17))
    assert not snapshot.inactive_evidence("LIVE", "NYSE", date(2026, 7, 17))
    assert not snapshot.inactive_evidence("MISSING", "NASDAQ", date(2026, 7, 17))


def test_tiingo_lifecycle_proves_only_unanimous_exact_non_stock_types() -> None:
    snapshot = parse_tiingo_lifecycle_zip(
        _zip(
            HEADER
            + "FUND,NASDAQ,ETF,USD,2020-01-01,2026-07-20\n"
            + "MIXED,NYSE,ETF,USD,2020-01-01,2026-07-20\n"
            + "MIXED,NYSE,Stock,USD,2020-01-01,2026-07-20\n"
        )
    )

    assert snapshot.non_stock_evidence("FUND", "NASDAQ")
    assert not snapshot.non_stock_evidence("MIXED", "NYSE")


def test_tiingo_lifecycle_schema_and_archive_layout_fail_closed() -> None:
    with pytest.raises(PricePayloadError, match="layout"):
        parse_tiingo_lifecycle_zip(_zip(HEADER, name="other.csv"))
    with pytest.raises(PricePayloadError, match="schema"):
        parse_tiingo_lifecycle_zip(_zip("ticker,exchange\nAAPL,NASDAQ\n"))


def test_tiingo_lifecycle_preserves_but_never_uses_exchange_less_rows() -> None:
    snapshot = parse_tiingo_lifecycle_zip(_zip(HEADER + "OLD,,Stock,USD,2020-01-01,2025-01-02\n"))

    assert snapshot.rows[0].exchange == ""
    assert not snapshot.inactive_evidence("OLD", "NASDAQ", date(2026, 7, 17))
