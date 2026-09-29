"""Repository-wide pytest guardrails.

Tests may read canonical research artifacts, but they must never leave synthetic
backtest output in ``data/output``.  Keep this protection at repository scope so
new test modules cannot accidentally bypass it.
"""

from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent
_PROTECTED_BACKTEST_ARTIFACTS = tuple(
    _REPO_ROOT / "data" / "output" / name
    for name in (
        "backtest_daily_summary.json",
        "backtest_investor_grade_index_aware.json",
        "backtest_weekly_details.csv",
        "backtest_weekly_details_classic.csv",
        "backtest_weekly_details_index_aware.csv",
        "backtest_exit_events_classic.csv",
        "backtest_exit_events_index_aware.csv",
    )
)


@pytest.fixture(scope="session", autouse=True)
def protect_canonical_backtest_artifacts():
    """Restore and fail if a test mutates canonical backtest evidence."""

    before = {
        path: path.read_bytes() if path.exists() else None
        for path in _PROTECTED_BACKTEST_ARTIFACTS
    }
    yield

    changed: list[str] = []
    for path, original in before.items():
        current = path.read_bytes() if path.exists() else None
        if current == original:
            continue

        changed.append(str(path.relative_to(_REPO_ROOT)))
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)

    if changed:
        pytest.fail(
            "Tests mutated canonical backtest artifacts; changes were restored: "
            + ", ".join(changed),
            pytrace=False,
        )
