"""Build the packaged FF12/FF49 SIC range table from official definition ZIPs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

EXPECTED_SHA256: Final = {
    "ff12": "d801141acf039f2e06e6d4d9ba2b3992e9747a1d82fabd53ef21da4a3af79fff",
    "ff49": "36761b4009107e11cd15cff0eb75f8837ef49f024524b7378634629bf90905d5",
}
SOURCE_URLS: Final = {
    "ff12": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.French/ftp/Siccodes12.zip",
    "ff49": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Siccodes49.zip",
}
HEADER = re.compile(r"^\s*(\d+)\s+([A-Za-z0-9]+)\s{2,}(.+?)\s*$")
RANGE = re.compile(r"^\s*(\d{4})-(\d{4})(?:\s+.*)?$")


@dataclass
class Group:
    code: int
    label: str
    description: str
    ranges: list[tuple[int, int]]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_zip(path: Path, *, system: str, expected_groups: int) -> list[Group]:
    digest = _sha256(path)
    if digest != EXPECTED_SHA256[system]:
        raise ValueError(f"{system} source hash changed: {digest}")
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.casefold().endswith(".txt")]
        if len(names) != 1:
            raise ValueError(f"{system} ZIP must contain exactly one text definition")
        text = archive.read(names[0]).decode("utf-8-sig")

    groups: list[Group] = []
    current: Group | None = None
    for line in text.splitlines():
        header = HEADER.match(line)
        if header:
            current = Group(int(header.group(1)), header.group(2), header.group(3), [])
            groups.append(current)
            continue
        sic_range = RANGE.match(line)
        if sic_range:
            if current is None:
                raise ValueError(f"{system} range appears before a group header")
            start, end = map(int, sic_range.groups())
            if start > end:
                raise ValueError(f"{system} contains an inverted SIC range")
            current.ranges.append((start, end))
            continue
        if line.strip():
            raise ValueError(f"{system} contains an unparsed line: {line!r}")

    if [group.code for group in groups] != list(range(1, expected_groups + 1)):
        raise ValueError(f"{system} group numbering drifted")
    claimed: dict[int, int] = {}
    for group in groups:
        for start, end in group.ranges:
            for sic in range(start, end + 1):
                if sic in claimed:
                    raise ValueError(
                        f"{system} SIC {sic:04d} overlaps groups {claimed[sic]} and {group.code}"
                    )
                claimed[sic] = group.code
    return groups


def build(ff12_zip: Path, ff49_zip: Path, output: Path) -> None:
    systems = {
        "ff12": _parse_zip(ff12_zip, system="ff12", expected_groups=12),
        "ff49": _parse_zip(ff49_zip, system="ff49", expected_groups=49),
    }
    payload = {
        "schema_version": 1,
        "source": {
            system: {"url": SOURCE_URLS[system], "sha256": EXPECTED_SHA256[system]}
            for system in systems
        },
        "systems": {
            system: {
                "default_group": 12 if system == "ff12" else None,
                "groups": [
                    {
                        "code": group.code,
                        "label": group.label,
                        "ranges": group.ranges,
                    }
                    for group in groups
                ],
            }
            for system, groups in systems.items()
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ff12", type=Path, required=True)
    parser.add_argument("--ff49", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.ff12, args.ff49, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
