from __future__ import annotations

import pytest

from usinv.scoring.sectors import (
    SectorMappingError,
    classify_sic,
    sector_source_hashes,
)


@pytest.mark.parametrize(
    ("sic", "ff12", "ff49", "excluded"),
    [
        (3571, "BusEq", "Hardw", None),
        (7372, "BusEq", "Softw", None),
        (6021, "Money", "Banks", "financials"),
        (6798, "Money", "Fin", "reits"),
        (6500, "Money", "RlEst", None),
        (1521, "Other", "Cnstr", None),
    ],
)
def test_official_ff12_ff49_sic_examples(
    sic: int,
    ff12: str,
    ff49: str,
    excluded: str | None,
) -> None:
    result = classify_sic(sic)

    assert result.ff12.label == ff12
    assert result.ff49 is not None and result.ff49.label == ff49
    assert result.excluded_group == excluded


def test_ff12_other_is_explicit_default_but_ff49_gap_stays_unmapped() -> None:
    result = classify_sic(9999)

    assert result.ff12.label == "Other"
    assert result.ff49 is None


def test_sector_definition_sources_are_hash_pinned() -> None:
    hashes = sector_source_hashes()

    assert hashes == {
        "ff12": "d801141acf039f2e06e6d4d9ba2b3992e9747a1d82fabd53ef21da4a3af79fff",
        "ff49": "36761b4009107e11cd15cff0eb75f8837ef49f024524b7378634629bf90905d5",
    }


@pytest.mark.parametrize("sic", [True, 99, 10000, "3571"])
def test_invalid_sic_is_rejected(sic: object) -> None:
    with pytest.raises(SectorMappingError):
        classify_sic(sic)  # type: ignore[arg-type]
