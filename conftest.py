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


@pytest.fixture(scope="session", autouse=True)
def isolate_us_picker_runtime_paths(tmp_path_factory):
    """Keep the runtime DB and file cache out of the repo's data/ directory.

    USInv's artifact guard fails any test run that touches data/; picker code
    defaults to data/us_picker.db and data/cache/.
    """
    import os

    import us_picker.data.cache as cache_module

    runtime = tmp_path_factory.mktemp("us_picker_runtime")
    previous_db = os.environ.get("US_PICKER_DB_PATH")
    os.environ["US_PICKER_DB_PATH"] = str(runtime / "us_picker.db")
    previous_cache = cache_module._CACHE_DIR
    cache_module._CACHE_DIR = runtime / "cache"
    yield
    cache_module._CACHE_DIR = previous_cache
    if previous_db is None:
        os.environ.pop("US_PICKER_DB_PATH", None)
    else:
        os.environ["US_PICKER_DB_PATH"] = previous_db
