"""Versioned SIC to Fama-French 12/49 sector mapping."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Final, Literal

SECTOR_MAPPING_VERSION: Final = "usinv-ff-sic-v1"
FINANCIAL_SICS: Final = frozenset(
    {
        *range(6020, 6037),
        6199,
        6211,
        *range(6311, 6400),
    }
)
FFSystem = Literal["ff12", "ff49"]


class SectorMappingError(ValueError):
    """Raised when the packaged official SIC range contract is invalid."""


@dataclass(frozen=True, slots=True)
class SectorGroup:
    code: int
    label: str


@dataclass(frozen=True, slots=True)
class SectorClassification:
    sic: int
    ff12: SectorGroup
    ff49: SectorGroup | None
    excluded_group: Literal["financials", "reits"] | None
    version: str = SECTOR_MAPPING_VERSION


@dataclass(frozen=True, slots=True)
class _Rule:
    start: int
    end: int
    group: SectorGroup


@dataclass(frozen=True, slots=True)
class _SectorDefinitions:
    rules: dict[FFSystem, tuple[_Rule, ...]]
    defaults: dict[FFSystem, SectorGroup | None]
    source_sha256: dict[FFSystem, str]


@lru_cache(maxsize=1)
def _definitions() -> _SectorDefinitions:
    resource = files("usinv.scoring").joinpath("sic_ranges_v1.json")
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SectorMappingError("packaged SIC definitions are unreadable") from exc
    if payload.get("schema_version") != 1:
        raise SectorMappingError("packaged SIC definition schema drifted")

    rules: dict[FFSystem, tuple[_Rule, ...]] = {}
    defaults: dict[FFSystem, SectorGroup | None] = {}
    hashes: dict[FFSystem, str] = {}
    for system, expected_count in (("ff12", 12), ("ff49", 49)):
        source = payload.get("source", {}).get(system, {})
        digest = source.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise SectorMappingError(f"{system} source hash is invalid")
        hashes[system] = digest
        node = payload.get("systems", {}).get(system, {})
        raw_groups = node.get("groups")
        if not isinstance(raw_groups, list) or len(raw_groups) != expected_count:
            raise SectorMappingError(f"{system} group count drifted")
        groups: dict[int, SectorGroup] = {}
        system_rules: list[_Rule] = []
        claimed: set[int] = set()
        for raw_group in raw_groups:
            try:
                code = int(raw_group["code"])
                label = str(raw_group["label"])
                raw_ranges = raw_group["ranges"]
            except (KeyError, TypeError, ValueError) as exc:
                raise SectorMappingError(f"{system} group schema is invalid") from exc
            group = SectorGroup(code, label)
            groups[code] = group
            for raw_range in raw_ranges:
                if not isinstance(raw_range, list) or len(raw_range) != 2:
                    raise SectorMappingError(f"{system} SIC range schema is invalid")
                start, end = map(int, raw_range)
                if not 100 <= start <= end <= 9999:
                    raise SectorMappingError(f"{system} SIC range is invalid")
                values = set(range(start, end + 1))
                if claimed.intersection(values):
                    raise SectorMappingError(f"{system} SIC ranges overlap")
                claimed.update(values)
                system_rules.append(_Rule(start, end, group))
        if set(groups) != set(range(1, expected_count + 1)):
            raise SectorMappingError(f"{system} group numbering drifted")
        default_code = node.get("default_group")
        defaults[system] = groups.get(default_code) if default_code is not None else None
        rules[system] = tuple(sorted(system_rules, key=lambda item: (item.start, item.end)))
    return _SectorDefinitions(rules, defaults, hashes)


def sector_source_hashes() -> dict[FFSystem, str]:
    """Return exact official ZIP hashes used to generate the packaged ranges."""
    return dict(_definitions().source_sha256)


def _classify(system: FFSystem, sic: int) -> SectorGroup | None:
    definitions = _definitions()
    matched = [rule.group for rule in definitions.rules[system] if rule.start <= sic <= rule.end]
    if len(matched) > 1:
        raise SectorMappingError(f"{system} SIC mapping is ambiguous")
    return matched[0] if matched else definitions.defaults[system]


def classify_sic(sic: int) -> SectorClassification:
    """Map one four-digit SIC without inventing an FF49 category for gaps."""
    if isinstance(sic, bool) or not isinstance(sic, int) or not 100 <= sic <= 9999:
        raise SectorMappingError("SIC must be an integer from 0100 through 9999")
    ff12 = _classify("ff12", sic)
    if ff12 is None:
        raise SectorMappingError("FF12 definitions unexpectedly left SIC unmapped")
    ff49 = _classify("ff49", sic)
    if sic == 6798:
        excluded_group: Literal["financials", "reits"] | None = "reits"
    elif sic in FINANCIAL_SICS:
        excluded_group = "financials"
    else:
        excluded_group = None
    return SectorClassification(sic, ff12, ff49, excluded_group)
