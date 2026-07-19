from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from usinv.data.edgar.securities import (
    Security,
    SymbolInterval,
    build_security_master,
    mint_security_id,
)
from usinv.data.prices.base import (
    PRICES_ADJUSTED_SCHEMA,
    PRICES_RAW_SCHEMA,
    PriceFetchResult,
    PriceMappingError,
    PricePayloadError,
    PriceQuery,
    PriceSecurityBinding,
    PriceSourcePage,
    PriceStoreError,
    VendorDailyBar,
    materialize_price_snapshot,
    price_bindings_from_security_master,
)

START = datetime(2020, 1, 1, tzinfo=UTC)
END = datetime(2024, 1, 1, tzinfo=UTC)
OBSERVED = datetime(2026, 7, 19, 12, tzinfo=UTC)


def _page(adjustment: str, marker: str = "same") -> PriceSourcePage:
    body = f'{{"adjustment":"{adjustment}","marker":"{marker}"}}'.encode()
    return PriceSourcePage(
        f"https://data.alpaca.markets/test?adjustment={adjustment}&marker={marker}",
        OBSERVED,
        hashlib.sha256(body).hexdigest(),
        f"request-{adjustment}-{marker}",
        body,
    )


def _bar(
    session: date,
    *,
    symbol: str = "REC",
    close: str = "10",
    page_index: int = 0,
) -> VendorDailyBar:
    zone_offset = 5 if session.month in {1, 2, 11, 12} else 4
    close_value = Decimal(close)
    return VendorDailyBar(
        symbol,
        datetime(session.year, session.month, session.day, zone_offset, tzinfo=UTC),
        session,
        close_value - 1,
        close_value + 1,
        close_value - 2,
        close_value,
        1000,
        100,
        Decimal("9.5"),
        page_index,
    )


def _result(
    adjustment: str,
    bars: tuple[VendorDailyBar, ...],
    *,
    marker: str = "same",
) -> PriceFetchResult:
    return PriceFetchResult(
        "alpaca",
        "alpaca-v2-stocks-bars-1day-sip-trade-aggregate",
        PriceQuery(("REC",), START, END, adjustment),  # type: ignore[arg-type]
        (_page(adjustment, marker),),
        bars,
    )


def _binding(
    security_id: str,
    start: date,
    end: date | None = None,
    *,
    exchange: str = "NASDAQ",
    pointer: str | None = None,
) -> PriceSecurityBinding:
    return PriceSecurityBinding(
        security_id,
        "REC",
        exchange,
        start,
        end,
        pointer or f"listing://{security_id}/{start}",
    )


def test_ticker_reuse_maps_each_session_to_immutable_security_id(tmp_path: Path) -> None:
    bars = (_bar(date(2020, 6, 1)), _bar(date(2023, 6, 1), close="20"))
    bindings = (
        _binding("old-security", date(2018, 1, 1), date(2021, 1, 1)),
        _binding("new-security", date(2022, 1, 1)),
    )
    snapshot = materialize_price_snapshot(
        _result("raw", bars),
        _result("all", bars),
        bindings,
        tmp_path,
    )

    raw = pq.read_table(snapshot.output_dir / "prices_raw.parquet")
    assert raw.schema.equals(PRICES_RAW_SCHEMA, check_metadata=True)
    assert set(raw.column("security_id").to_pylist()) == {"old-security", "new-security"}
    assert snapshot.issues == ()


def test_bindings_are_selected_from_security_master_evidence_not_ticker_guess() -> None:
    security_id = mint_security_id(100, "cusip:fixture")
    security = Security(
        security_id,
        100,
        "Common Stock",
        "common_stock",
        True,
        "cusip:fixture",
        "fixture",
        "security://fixture",
    )
    symbols = (
        SymbolInterval(
            security_id,
            "REC",
            "NASDAQ",
            date(2020, 1, 1),
            None,
            "listing_history",
            "high",
            "listing://rec",
            OBSERVED,
            "historical_interval",
        ),
        SymbolInterval(
            security_id,
            "AID",
            "NASDAQ",
            date(2010, 1, 1),
            date(2015, 1, 1),
            "filename_guess",
            "low",
            "recovery://aid",
            OBSERVED,
            "recovery_only",
        ),
    )
    master = build_security_master((security,), symbols)

    bindings = price_bindings_from_security_master(
        master,
        symbols=("REC", "AID"),
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
    )

    assert len(bindings) == 1
    assert bindings[0].security_id == security_id
    assert bindings[0].evidence_pointer == "listing://rec"


def test_raw_and_vendor_adjusted_values_can_never_share_one_table(tmp_path: Path) -> None:
    raw_bar = (_bar(date(2023, 6, 1), close="20"),)
    adjusted_bar = (_bar(date(2023, 6, 1), close="5"),)
    snapshot = materialize_price_snapshot(
        _result("raw", raw_bar),
        _result("all", adjusted_bar),
        (_binding("security", date(2022, 1, 1)),),
        tmp_path,
    )

    raw = pq.read_table(snapshot.output_dir / "prices_raw.parquet")
    adjusted = pq.read_table(snapshot.output_dir / "prices_vendor_adjusted.parquet")
    assert "adjustment" not in raw.column_names
    assert raw.column("close").to_pylist() == [Decimal("20.00000000")]
    assert adjusted.schema.equals(PRICES_ADJUSTED_SCHEMA, check_metadata=True)
    assert adjusted.column("adjustment").to_pylist() == ["all"]
    assert adjusted.column("close").to_pylist() == [Decimal("5.00000000")]


def test_unmapped_and_ambiguous_rows_are_counted_in_issue_artifact(tmp_path: Path) -> None:
    bars = (_bar(date(2021, 6, 1)), _bar(date(2023, 6, 1)))
    bindings = (
        _binding("first", date(2022, 1, 1)),
        _binding("second", date(2023, 1, 1), exchange="NYSE"),
    )
    snapshot = materialize_price_snapshot(
        _result("raw", bars),
        _result("all", bars),
        bindings,
        tmp_path,
    )

    assert [issue.kind for issue in snapshot.issues] == [
        "unmapped",
        "ambiguous_mapping",
        "unmapped",
        "ambiguous_mapping",
    ]
    issues = pq.read_table(snapshot.output_dir / "price_mapping_issues.parquet")
    assert issues.num_rows == 4
    assert pq.read_table(snapshot.output_dir / "prices_raw.parquet").num_rows == 0


def test_corroborating_evidence_for_same_security_is_not_false_ambiguity(
    tmp_path: Path,
) -> None:
    bindings = (
        _binding("security", date(2022, 1, 1), pointer="source://one"),
        _binding("security", date(2022, 1, 1), pointer="source://two"),
    )
    bars = (_bar(date(2023, 6, 1)),)
    snapshot = materialize_price_snapshot(
        _result("raw", bars),
        _result("all", bars),
        bindings,
        tmp_path,
    )

    raw = pq.read_table(snapshot.output_dir / "prices_raw.parquet")
    assert raw.num_rows == 1
    assert raw.column("mapping_evidence").to_pylist() == ["source://one|source://two"]


def test_conflicting_duplicate_canonical_key_fails_closed(tmp_path: Path) -> None:
    raw = _result(
        "raw",
        (_bar(date(2023, 6, 1), close="10"), _bar(date(2023, 6, 1), close="11")),
    )
    adjusted = _result("all", (_bar(date(2023, 6, 1)),))

    with pytest.raises(PriceMappingError, match="conflicting bars"):
        materialize_price_snapshot(
            raw,
            adjusted,
            (_binding("security", date(2022, 1, 1)),),
            tmp_path,
        )


def test_snapshot_is_content_addressed_reused_and_corruption_checked(tmp_path: Path) -> None:
    bars = (_bar(date(2023, 6, 1)),)
    bindings = (_binding("security", date(2022, 1, 1)),)
    first = materialize_price_snapshot(
        _result("raw", bars),
        _result("all", bars),
        bindings,
        tmp_path,
    )
    cached = materialize_price_snapshot(
        _result("raw", bars),
        _result("all", bars),
        bindings,
        tmp_path,
    )

    assert not first.from_cache and cached.from_cache
    assert first.snapshot_id == cached.snapshot_id
    first.output_dir.joinpath("prices_raw.parquet").write_bytes(b"tampered")
    with pytest.raises(PriceStoreError, match="failed verification"):
        materialize_price_snapshot(
            _result("raw", bars),
            _result("all", bars),
            bindings,
            tmp_path,
        )


def test_source_page_change_produces_new_insert_only_snapshot(tmp_path: Path) -> None:
    bars = (_bar(date(2023, 6, 1)),)
    bindings = (_binding("security", date(2022, 1, 1)),)
    first = materialize_price_snapshot(
        _result("raw", bars, marker="first"),
        _result("all", bars, marker="first"),
        bindings,
        tmp_path,
    )
    second = materialize_price_snapshot(
        _result("raw", bars, marker="second"),
        _result("all", bars, marker="second"),
        bindings,
        tmp_path,
    )

    assert first.snapshot_id != second.snapshot_id
    assert first.output_dir.is_dir() and second.output_dir.is_dir()


def test_materializer_rejects_mixed_or_mismatched_adjustment_contracts(tmp_path: Path) -> None:
    bars = (_bar(date(2023, 6, 1)),)
    with pytest.raises(PriceStoreError, match="separate raw and all"):
        materialize_price_snapshot(
            _result("all", bars),
            _result("all", bars),
            (_binding("security", date(2022, 1, 1)),),
            tmp_path,
        )


def test_provider_neutral_result_rejects_non_session_bar_before_storage() -> None:
    with pytest.raises(PricePayloadError, match="non-XNYS session"):
        _result("raw", (_bar(date(2023, 6, 3)),))
