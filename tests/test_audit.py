"""Tests for the weekly audit & paper-forward ledger integrity evaluation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from usinv.cli import main
from usinv.delivery.audit import (
    FROZEN_CONFIG_HASH,
    FROZEN_DATA_MANIFEST_HASH,
    evaluate_weekly_audit,
)
from usinv.delivery.snapshot import load_snapshot
from usinv.delivery.telegram import TelegramNotifier


def test_evaluate_weekly_audit_passes_on_valid_snapshot() -> None:
    snap_path = Path("web/snapshot.json")
    report = evaluate_weekly_audit(snap_path)
    assert report.passed
    assert len(report.violations) == 0
    assert report.metrics["nav"] > 0
    assert report.metrics["config_hash"] == FROZEN_CONFIG_HASH
    assert report.metrics["data_manifest_hash"] == FROZEN_DATA_MANIFEST_HASH


def test_evaluate_weekly_audit_detects_config_hash_drift() -> None:
    snap = load_snapshot(Path("web/snapshot.json"))
    drifted = replace(snap, config_hash="tampered_hash_value_12345")
    report = evaluate_weekly_audit(drifted)
    assert not report.passed
    assert any("Config hash drift" in v for v in report.violations)


def test_evaluate_weekly_audit_detects_data_manifest_hash_drift() -> None:
    snap = load_snapshot(Path("web/snapshot.json"))
    drifted = replace(snap, data_manifest_hash="tampered_manifest_hash_12345")
    report = evaluate_weekly_audit(drifted)
    assert not report.passed
    assert any("Data manifest hash drift" in v for v in report.violations)


def test_evaluate_weekly_audit_detects_nav_accounting_violation() -> None:
    snap = load_snapshot(Path("web/snapshot.json"))
    violated = replace(snap, cash=snap.cash + 5000.0)  # Cash changed without NAV changing
    report = evaluate_weekly_audit(violated)
    assert not report.passed
    assert any("NAV accounting violation" in v for v in report.violations)


def test_evaluate_weekly_audit_detects_coverage_threshold_failure() -> None:
    snap = load_snapshot(Path("web/snapshot.json"))
    degraded_health = replace(snap.data_health, core_coverage_pct=88.5, identity_gaps=2)
    degraded = replace(snap, data_health=degraded_health)
    report = evaluate_weekly_audit(degraded)
    assert not report.passed
    assert any("Core fundamental coverage" in v for v in report.violations)
    assert any("Identity gaps present" in v for v in report.violations)


def test_evaluate_weekly_audit_detects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "non_existent_snapshot.json"
    report = evaluate_weekly_audit(missing)
    assert not report.passed
    assert any("Snapshot file not found" in v for v in report.violations)


def test_telegram_notify_weekly_audit_unconfigured() -> None:
    notifier = TelegramNotifier(bot_token="", chat_id="")
    assert not notifier.is_configured
    report = evaluate_weekly_audit(Path("web/snapshot.json"))
    result = notifier.notify_weekly_audit(report.to_dict())
    assert result is False  # Suppressed when unconfigured


def test_cli_weekly_audit_execution(tmp_path: Path) -> None:
    output_path = tmp_path / "audit_report.json"
    exit_code = main(
        [
            "weekly-audit",
            "--snapshot",
            "web/snapshot.json",
            "--output",
            str(output_path),
            "--enforce",
        ]
    )
    assert exit_code == 0
    assert output_path.exists()
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["passed"] is True
