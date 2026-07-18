"""Global test guards."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROTECTED_RUNTIME_PATHS = (ROOT / "data", ROOT / "artifacts")


def _snapshot(paths: tuple[Path, ...]) -> dict[Path, tuple[int, int]]:
    return {
        item.relative_to(ROOT): (item.stat().st_size, item.stat().st_mtime_ns)
        for path in paths
        if path.exists()
        for item in path.rglob("*")
        if item.is_file()
    }


@pytest.fixture(scope="session", autouse=True)
def artifact_guard() -> Iterator[None]:
    """Fail if a test writes into production runtime directories."""
    before = _snapshot(PROTECTED_RUNTIME_PATHS)
    yield
    after = _snapshot(PROTECTED_RUNTIME_PATHS)
    changed = sorted(
        set(before) ^ set(after)
        | {path for path in set(before) & set(after) if before[path] != after[path]}
    )
    assert after == before, f"tests modified production runtime paths: {changed}"
