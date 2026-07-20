from __future__ import annotations

import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from usinv.config import ConfigError, EvidenceMode, ExecutionMode, load_config

CONFIG_DIR = Path(__file__).resolve().parents[1] / "usinv" / "config"


def _copy_config(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, target, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    return target


def test_blueprint_defaults_are_typed_and_registered() -> None:
    config = load_config()

    assert config.settings.evidence_mode is EvidenceMode.RESEARCH
    assert config.settings.execution_mode is ExecutionMode.PAPER
    assert config.settings.timezones.exchange == "America/New_York"
    assert config.settings.edgar.max_requests_per_second == 8
    assert config.universe.raw_close_min_exclusive == 2.0
    assert config.universe.median_dollar_volume_min == 1_000_000.0
    assert config.universe.core_market_cap_min == 100_000_000.0
    assert config.universe.core_market_cap_max == 10_000_000_000.0
    assert config.portfolio.holdings == 15
    assert config.portfolio.large_cap_max_slots == 0
    assert config.portfolio.rotation_weeks == 4
    assert config.portfolio.entry_top_fraction == 0.10
    assert config.portfolio.hold_top_fraction == 0.25
    assert config.portfolio.sector_cap_fraction == 0.27
    assert config.portfolio.trailing_stop == "percent_20"
    assert config.portfolio.execution_cost_bps == 40
    assert config.regime.default_overlay == "O0"
    assert config.experiment_grid.full_cross_cells == 331_776
    assert config.experiment_grid.planned_max == 190


def test_config_is_immutable_and_hash_is_stable() -> None:
    first = load_config()
    second = load_config()

    assert first.config_hash == second.config_hash
    assert len(first.config_hash) == 64
    with pytest.raises(FrozenInstanceError):
        first.portfolio.holdings = 20  # type: ignore[misc]


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path)
    path = directory / "portfolio.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["holdngs"] = 20
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match=r"unknown=.*holdngs"):
        load_config(directory)


def test_experiment_count_drift_is_rejected(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path)
    path = directory / "experiment_grid.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["limits"]["planned_max"] = 191
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="planned experiment count"):
        load_config(directory)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("domestic_only", False, "domestic common stock"),
        ("common_stock_only", False, "domestic common stock"),
        ("excluded_groups", ["financials", "reits", "fpi_adr"], "scope exclusions"),
    ],
)
def test_v1_universe_scope_cannot_be_silently_weakened(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    directory = _copy_config(tmp_path)
    path = directory / "universe.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(directory)


def test_live_mode_requires_selected_evidence_mode(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path)
    path = directory / "settings.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["evidence_mode"] = "undecided"
    payload["execution_mode"] = "live"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="live execution is forbidden"):
        load_config(directory)


def test_live_mode_remains_disabled_until_activation_gates_exist(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path)
    path = directory / "settings.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["evidence_mode"] = "audit"
    payload["execution_mode"] = "live"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="D024 activation gates"):
        load_config(directory)
