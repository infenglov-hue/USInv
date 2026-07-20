from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from usinv.data.edgar.securities import (
    Security,
    SecurityMasterError,
    SymbolInterval,
    build_security_master,
    current_sec_symbol,
    materialize_security_master,
    mint_security_id,
    normalize_exchange,
    read_security_master_snapshot,
)


def test_exchange_normalization_is_idempotent_for_every_canonical_venue() -> None:
    for exchange in ("NASDAQ", "NYSE", "NYSEAMERICAN"):
        assert normalize_exchange(normalize_exchange(exchange)) == exchange


def _security(
    cik: int,
    anchor: str,
    title: str = "Common Stock",
    *,
    security_type: str = "common_stock",
) -> Security:
    return Security(
        security_id=mint_security_id(cik, anchor),
        cik=cik,
        class_title=title,
        security_type=security_type,
        domestic_flag=True,
        identity_anchor=anchor,
        source="sec_xbrl_cover",
        evidence_pointer=f"sec://{cik}/{anchor}",
    )


def _symbol(
    security: Security,
    ticker: str,
    start: date,
    end: date | None = None,
    *,
    exchange: str = "NASDAQ",
) -> SymbolInterval:
    return SymbolInterval(
        security_id=security.security_id,
        ticker=ticker,
        exchange=exchange,
        valid_from=start,
        valid_to=end,
        source="alpha_vantage_listing_status",
        confidence="high",
        evidence_pointer=f"av://{start}/{ticker}",
        known_at=datetime(2026, 7, 19, tzinfo=UTC),
        scope="historical_interval",
    )


def test_security_id_is_not_derived_from_recyclable_ticker() -> None:
    first = mint_security_id(100, "cusip:123456789")
    second = mint_security_id(100, "cusip:123456789")

    assert first == second and first.startswith("USINVSEC-")
    with pytest.raises(SecurityMasterError, match="ticker"):
        mint_security_id(100, "ticker:REUSED")


def test_date_valid_mapping_handles_ticker_reuse_without_entity_guessing() -> None:
    old = _security(100, "cusip:old")
    new = _security(200, "cusip:new")
    master = build_security_master(
        [old, new],
        [
            _symbol(old, "REC", date(2018, 1, 1), date(2021, 1, 1)),
            _symbol(new, "REC", date(2022, 1, 1)),
        ],
    )

    assert master.resolve("REC", "Nasdaq", date(2020, 6, 1)).security_id == old.security_id
    assert master.resolve("REC", "Nasdaq", date(2023, 6, 1)).security_id == new.security_id
    assert master.resolve("REC", "Nasdaq", date(2021, 6, 1)).status == "unmapped"


def test_current_sec_ticker_is_live_edge_only_and_cannot_leak_backwards() -> None:
    security = _security(320193, "sec-class:common")
    observed = datetime(2026, 7, 19, 12, tzinfo=UTC)
    current = current_sec_symbol(
        security_id=security.security_id,
        ticker="AAPL",
        exchange="Nasdaq",
        observed_at=observed,
        evidence_pointer="https://data.sec.gov/submissions/CIK0000320193.json",
    )
    master = build_security_master([security], [current])

    assert (
        master.resolve("AAPL", "NASDAQ", date(2025, 7, 19), minimum_confidence="medium").status
        == "unmapped"
    )
    assert (
        master.resolve("AAPL", "NASDAQ", date(2026, 7, 19), minimum_confidence="medium").security_id
        == security.security_id
    )


def test_overlapping_recycled_ticker_is_quarantined_not_guessed() -> None:
    first = _security(100, "cusip:first")
    second = _security(200, "cusip:second")
    master = build_security_master(
        [first, second],
        [
            _symbol(first, "COLL", date(2020, 1, 1), date(2024, 1, 1)),
            _symbol(second, "COLL", date(2023, 1, 1)),
        ],
    )

    result = master.resolve("COLL", "NASDAQ", date(2023, 6, 1))
    assert result.status == "quarantined"
    assert set(result.candidate_security_ids) == {first.security_id, second.security_id}
    assert master.issues[0].kind == "ticker_collision"


def test_equity_resolution_ignores_same_symbol_non_equity_cover_class() -> None:
    common = _security(100, "sec-cover:common")
    note = _security(100, "sec-cover:note", "3.125% Notes", security_type="other")
    master = build_security_master(
        [common, note],
        [
            _symbol(common, "ONE", date(2020, 1, 1)),
            _symbol(note, "ONE", date(2020, 1, 1)),
        ],
    )

    assert master.resolve("ONE", "NASDAQ", date(2023, 6, 1)).status == "quarantined"
    equity = master.resolve(
        "ONE",
        "NASDAQ",
        date(2023, 6, 1),
        required_security_type="common_stock",
    )
    assert equity.status == "mapped"
    assert equity.security_id == common.security_id


def test_one_security_cannot_have_two_simultaneous_tickers_on_one_exchange() -> None:
    security = _security(100, "cusip:single")
    master = build_security_master(
        [security],
        [
            _symbol(security, "OLD", date(2020, 1, 1), date(2024, 1, 1)),
            _symbol(security, "NEW", date(2023, 1, 1)),
        ],
    )

    assert master.issues[0].kind == "security_symbol_overlap"
    assert master.resolve("OLD", "NASDAQ", date(2023, 6, 1)).status == "quarantined"


def test_security_master_snapshot_is_content_addressed_and_verified(tmp_path: Path) -> None:
    security = _security(100, "cusip:snapshot")
    master = build_security_master(
        [security],
        [_symbol(security, "SNAP", date(2020, 1, 1))],
    )

    created = materialize_security_master(master, tmp_path)
    cached = materialize_security_master(master, tmp_path)

    assert not created.from_cache and cached.from_cache
    assert created.snapshot_id == cached.snapshot_id
    assert created.output_dir.joinpath("securities.parquet").is_file()
    reopened = read_security_master_snapshot(created.output_dir)
    assert reopened == master

    created.output_dir.joinpath("security_symbols.parquet").write_bytes(b"tampered")
    with pytest.raises(SecurityMasterError, match="failed verification"):
        materialize_security_master(master, tmp_path)
