from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from usinv.data.prices.base import PriceConfigurationError, PricePayloadError, PriceStoreError
from usinv.data.prices.stooq import (
    STOOQ_EXPECTED_HEADER,
    StooqBasisSample,
    archive_stooq_bulk,
    check_stooq_drift,
    classify_stooq_adjustment_basis,
    read_stooq_bulk,
)

OBSERVED = datetime(2026, 7, 20, 7, tzinfo=UTC)


def _zip(
    path: Path,
    *,
    malicious: bool = False,
    fractional_volume: bool = False,
    row_ticker: str = "AAPL.US",
) -> Path:
    header = ",".join(STOOQ_EXPECTED_HEADER)
    body = "\n".join(
        (
            header,
            "AAPL.US,D,19840907,000000,1,2,1,2,100000,0",
            "AAPL.US,D,20240102,000000,185,188,183,187,1000000,0",
            (
                f"{row_ticker},D,20240103,000000,187,189,184,185,"
                f"{'1100000.5' if fractional_volume else '1100000'},0"
            ),
        )
    )
    entry = "../escape.txt" if malicious else "data/daily/us/nasdaq stocks/1/aapl.us.txt"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(entry, body)
    return path


def _sample(
    event_type: str,
    stooq: str,
    split_only: str,
    total_return: str,
    *,
    day: date,
    archive_sha256: str = "a" * 64,
) -> StooqBasisSample:
    return StooqBasisSample(
        "security",
        day,
        event_type,  # type: ignore[arg-type]
        Decimal(stooq),
        Decimal(split_only),
        Decimal(total_return),
        archive_sha256,
        f"fixture://stooq/{day}/{event_type}",
    )


def test_stooq_full_zip_is_content_addressed_and_parsed_as_adjusted_only(
    tmp_path: Path,
) -> None:
    source = _zip(tmp_path / "download.zip")
    archived = archive_stooq_bulk(source, tmp_path / "store", retrieved_at=OBSERVED)
    cached = archive_stooq_bulk(
        source,
        tmp_path / "store",
        retrieved_at=datetime(2026, 7, 21, 7, tzinfo=UTC),
    )
    result = read_stooq_bulk(archived, symbols=("AAPL",))

    assert archived.content_sha256 == cached.content_sha256
    assert archived.source_path == cached.source_path
    assert cached.retrieved_at == OBSERVED
    assert result.adjustment_basis == "unresolved"
    assert [bar.session for bar in result.bars] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert result.bars[0].close == Decimal("187")
    assert result.bars[0].volume == Decimal("1000000")
    assert result.bars[0].archive_entry.endswith("aapl.us.txt")


def test_stooq_html_challenge_and_unsafe_zip_fail_closed(tmp_path: Path) -> None:
    html = tmp_path / "challenge.zip"
    html.write_text("<html>JavaScript verification</html>", encoding="utf-8")
    with pytest.raises(PricePayloadError, match="not a ZIP"):
        archive_stooq_bulk(html, tmp_path / "store", retrieved_at=OBSERVED)

    malicious = _zip(tmp_path / "malicious.zip", malicious=True)
    with pytest.raises(PricePayloadError, match="unsafe entry"):
        archive_stooq_bulk(malicious, tmp_path / "store", retrieved_at=OBSERVED)

    absolute = tmp_path / "absolute.zip"
    with zipfile.ZipFile(absolute, "w") as archive:
        archive.writestr("/absolute.txt", "unsafe")
    with pytest.raises(PricePayloadError, match="unsafe entry"):
        archive_stooq_bulk(absolute, tmp_path / "store", retrieved_at=OBSERVED)


def test_stooq_cached_archive_corruption_is_never_silently_reused(tmp_path: Path) -> None:
    source = _zip(tmp_path / "download.zip")
    archived = archive_stooq_bulk(source, tmp_path / "store", retrieved_at=OBSERVED)
    archived.source_path.write_bytes(b"tampered")

    with pytest.raises(PriceStoreError, match="hash verification"):
        archive_stooq_bulk(source, tmp_path / "store", retrieved_at=OBSERVED)


def test_stooq_drift_check_stays_disabled_until_dividend_basis_is_identified() -> None:
    split_only = (_sample("split", "50", "50", "50", day=date(2024, 1, 4)),)
    unresolved = classify_stooq_adjustment_basis(split_only)
    assert not unresolved.drift_check_enabled
    with pytest.raises(PriceConfigurationError, match="disabled"):
        check_stooq_drift(split_only, unresolved)


def test_stooq_dividend_payer_empirically_selects_total_return_basis() -> None:
    samples = (
        _sample("split", "50", "50", "50", day=date(2024, 1, 4)),
        _sample("cash_dividend", "98", "100", "98", day=date(2024, 1, 5)),
    )
    assessment = classify_stooq_adjustment_basis(samples)

    assert assessment.basis == "splits_and_dividends"
    assert assessment.drift_check_enabled
    assert check_stooq_drift(samples, assessment) == ()


def test_stooq_basis_samples_from_multiple_archives_remain_unresolved() -> None:
    samples = (
        _sample("split", "1", "1", "1", day=date(2024, 1, 4)),
        _sample(
            "cash_dividend",
            "0.98",
            "0.98",
            "0.99",
            day=date(2024, 1, 5),
            archive_sha256="b" * 64,
        ),
    )

    assessment = classify_stooq_adjustment_basis(samples)

    assert assessment.basis == "unresolved"
    assert assessment.archive_sha256 is None
    assert "one immutable" in assessment.reason


def test_stooq_drift_rejects_samples_from_a_different_archive() -> None:
    assessed_samples = (
        _sample("split", "1", "1", "1", day=date(2024, 1, 4)),
        _sample("cash_dividend", "0.98", "0.98", "0.99", day=date(2024, 1, 5)),
    )
    assessment = classify_stooq_adjustment_basis(assessed_samples)
    other_archive_samples = tuple(
        _sample(
            sample.event_type,
            str(sample.stooq_return),
            str(sample.split_only_return),
            str(sample.total_return),
            day=sample.session,
            archive_sha256="b" * 64,
        )
        for sample in assessed_samples
    )

    with pytest.raises(PriceConfigurationError, match="assessed archive"):
        check_stooq_drift(other_archive_samples, assessment)


def test_stooq_basis_is_enabled_only_for_the_assessed_archive(tmp_path: Path) -> None:
    source = _zip(tmp_path / "download.zip")
    archived = archive_stooq_bulk(source, tmp_path / "store", retrieved_at=OBSERVED)
    samples = (
        _sample(
            "split",
            "1",
            "1",
            "1",
            day=date(2024, 1, 4),
            archive_sha256=archived.content_sha256,
        ),
        _sample(
            "cash_dividend",
            "0.98",
            "0.98",
            "0.99",
            day=date(2024, 1, 5),
            archive_sha256=archived.content_sha256,
        ),
    )
    assessment = classify_stooq_adjustment_basis(samples)

    result = read_stooq_bulk(
        archived,
        symbols=("AAPL",),
        basis_assessment=assessment,
    )
    assert result.adjustment_basis == "splits_only"

    different = _zip(tmp_path / "different.zip")
    with zipfile.ZipFile(different, "a") as archive:
        archive.writestr("extra.txt", "different")
    other = archive_stooq_bulk(different, tmp_path / "other", retrieved_at=OBSERVED)
    with pytest.raises(PriceConfigurationError, match="different archive"):
        read_stooq_bulk(other, symbols=("AAPL",), basis_assessment=assessment)


def test_stooq_header_drift_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "drift.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "data/daily/us/aapl.us.txt",
            "DATE,OPEN,HIGH,LOW,CLOSE,VOLUME\n20240102,1,2,1,2,100\n",
        )
    archived = archive_stooq_bulk(source, tmp_path / "store", retrieved_at=OBSERVED)

    with pytest.raises(PricePayloadError, match="header drifted"):
        read_stooq_bulk(archived, symbols=("AAPL",))


def test_stooq_fractional_adjusted_volume_is_preserved_exactly(tmp_path: Path) -> None:
    source = _zip(tmp_path / "fractional.zip", fractional_volume=True)
    archived = archive_stooq_bulk(source, tmp_path / "store", retrieved_at=OBSERVED)

    result = read_stooq_bulk(archived, symbols=("AAPL",))

    assert result.bars[-1].volume == Decimal("1100000.5")


def test_stooq_row_ticker_must_match_archive_entry(tmp_path: Path) -> None:
    source = _zip(tmp_path / "mismatch.zip", row_ticker="MSFT.US")
    archived = archive_stooq_bulk(source, tmp_path / "store", retrieved_at=OBSERVED)

    with pytest.raises(PricePayloadError, match="does not match"):
        read_stooq_bulk(archived, symbols=("AAPL",))
