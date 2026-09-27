"""Weekly pipeline, snapshot, and paper-forward ledger integrity audit.

Conforms to OPS_SPEC §3, §5, and CODEX Phase 7:
- Verifies snapshot schema and uncorrupted state.
- Verifies NAV accounting identity: positions_value + cash == nav.
- Verifies frozen model/config hash and data manifest hash immutability (drift detection).
- Audits performance tail continuity.
- Verifies data health and coverage metrics.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from usinv.delivery.snapshot import DeliverySnapshot, load_snapshot

logger = logging.getLogger(__name__)

# Pre-registered Phase 5 winning configuration & frozen data manifest hashes
FROZEN_CONFIG_HASH = "d3ecd3bd787897c9abfd450d97e33cbfa90de1d9f762d92760789e4ada17cb72"
FROZEN_DATA_MANIFEST_HASH = "05bdd470475a6c71dd288108a97034fed37000a0492e15cc47a950c3d164ad1b"


class AuditError(ValueError):
    """Raised when an integrity audit fails under enforcement."""


@dataclass(frozen=True, slots=True)
class AuditReport:
    evaluated_at: str
    snapshot_path: str
    passed: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def evaluate_weekly_audit(
    snapshot: DeliverySnapshot | Path | str,
    *,
    expected_config_hash: str | None = FROZEN_CONFIG_HASH,
    expected_data_manifest_hash: str | None = FROZEN_DATA_MANIFEST_HASH,
    max_nav_drift_cents: float = 0.05,
) -> AuditReport:
    """Perform comprehensive weekly audit of delivery snapshot and paper ledger state."""
    violations: list[str] = []
    warnings: list[str] = []

    if isinstance(snapshot, (str, Path)):
        path = Path(snapshot)
        if not path.exists():
            return AuditReport(
                evaluated_at=datetime.now(UTC).isoformat(),
                snapshot_path=str(path),
                passed=False,
                violations=[f"Snapshot file not found: {path}"],
            )
        snap = load_snapshot(path)
        path_str = str(path)
    else:
        snap = snapshot
        path_str = "in-memory"

    # 1. Config hash & Data Manifest Hash immutability
    if expected_config_hash and snap.config_hash != expected_config_hash:
        violations.append(
            f"Config hash drift: found '{snap.config_hash}', expected '{expected_config_hash}'"
        )
    if expected_data_manifest_hash and snap.data_manifest_hash != expected_data_manifest_hash:
        violations.append(
            f"Data manifest hash drift: found '{snap.data_manifest_hash}', "
            f"expected '{expected_data_manifest_hash}'"
        )

    # 2. NAV Accounting Identity: positions_value + cash == nav
    calc_positions_value = sum(p.market_value for p in snap.positions)
    drift = abs((calc_positions_value + snap.cash) - snap.nav)
    if drift > max_nav_drift_cents:
        violations.append(
            f"NAV accounting violation: calculated positions (${calc_positions_value:,.2f}) + "
            f"cash (${snap.cash:,.2f}) != NAV (${snap.nav:,.2f}), drift: ${drift:,.2f}"
        )

    pv_drift = abs(calc_positions_value - snap.positions_value)
    if pv_drift > max_nav_drift_cents:
        violations.append(
            f"Positions value mismatch: sum of positions (${calc_positions_value:,.2f}) != "
            f"positions_value field (${snap.positions_value:,.2f})"
        )

    # 3. Position Weights sum check
    if snap.positions and snap.nav > 0:
        total_weight = sum(p.weight_pct for p in snap.positions)
        cash_weight = (snap.cash / snap.nav) * 100.0
        total_allocated = total_weight + cash_weight
        if abs(total_allocated - 100.0) > 1.0:
            warnings.append(
                f"Total allocation percentage drift: {total_allocated:.2f}% (expected ~100.0%)"
            )

    # 4. Data Health & Coverage Gates
    health = snap.data_health
    if health.status == "DEGRADED":
        violations.append("Data health status is DEGRADED")
    elif health.status != "HEALTHY":
        warnings.append(f"Data health status is {health.status}")

    if health.core_coverage_pct < 90.0:
        violations.append(
            f"Core fundamental coverage {health.core_coverage_pct:.1f}% below 90.0% threshold"
        )
    if health.secondary_coverage_pct < 75.0:
        violations.append(
            f"Secondary coverage {health.secondary_coverage_pct:.1f}% below 75.0% threshold"
        )
    if health.identity_gaps > 0:
        violations.append(f"Identity gaps present: {health.identity_gaps}")
    if health.sector_gaps > 0:
        violations.append(f"Sector gaps present: {health.sector_gaps}")

    # 5. Performance Tail Continuity
    if snap.performance_tail:
        sessions = [p.session for p in snap.performance_tail]
        if sessions != sorted(sessions):
            violations.append("Performance tail sessions are not strictly ascending")

    passed = len(violations) == 0
    metrics = {
        "nav": snap.nav,
        "cash": snap.cash,
        "positions_count": len(snap.positions),
        "as_of_session": snap.as_of_session,
        "config_hash": snap.config_hash,
        "data_manifest_hash": snap.data_manifest_hash,
        "core_coverage_pct": health.core_coverage_pct,
        "secondary_coverage_pct": health.secondary_coverage_pct,
    }

    return AuditReport(
        evaluated_at=datetime.now(UTC).isoformat(),
        snapshot_path=path_str,
        passed=passed,
        violations=violations,
        warnings=warnings,
        metrics=metrics,
    )
