"""Versioned snapshot.json delivery contract and serialization.

The delivery artifact is published for consumption by the independent mobile-first
PWA in web/. It contains only derived metrics, ranks, positions, and health summaries;
it strictly prohibits bulk licensed data, provider secrets, or broker credentials.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA_VERSION = "1.0.0"

# Sensitive keywords and patterns prohibited from ever entering snapshot.json
SENSITIVE_KEY_PATTERNS = (
    re.compile(r".*(?:key|secret|token|password|auth|credential).*", re.IGNORECASE),
)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS
    re.compile(r"PK[0-9A-Z]{18}"),  # Alpaca key
)


class DeliveryError(ValueError):
    """Raised when snapshot validation or serialization fails."""


@dataclass(frozen=True, slots=True)
class PositionSummary:
    security_id: str
    ticker: str
    company_name: str
    shares: float
    cost_basis: float
    current_price: float
    market_value: float
    weight_pct: float
    stop_price: float | None
    high_water_mark: float
    entry_session: str


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    rank: int
    security_id: str
    ticker: str
    company_name: str
    sector: str
    core_or_large: str
    composite_score: float
    factor_ranks: dict[str, float]
    red_flag_status: str
    entry_reference_price: float | None = None


@dataclass(frozen=True, slots=True)
class MacroRegimeSummary:
    regime: str
    overlay: str
    equity_exposure_target: float
    signal_summary: str
    benchmark_dd: float
    as_of_session: str


@dataclass(frozen=True, slots=True)
class OrderSummary:
    order_id: str
    client_order_id: str
    security_id: str
    ticker: str
    side: str
    quantity: float
    reference_close: float
    limit_price: float
    collar_pct: float
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class PerformancePoint:
    session: str
    nav: float
    benchmark_nav: float
    daily_return: float
    benchmark_daily_return: float


@dataclass(frozen=True, slots=True)
class DataHealthSummary:
    status: str  # HEALTHY, STALE, DEGRADED
    freshness_evaluated_at: str
    core_coverage_pct: float
    secondary_coverage_pct: float
    identity_gaps: int
    sector_gaps: int
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DeliverySnapshot:
    schema_version: str
    as_of_session: str
    generated_at: str
    stale_after: str
    config_hash: str
    code_sha: str
    data_manifest_hash: str
    nav: float
    cash: float
    positions_value: float
    positions: list[PositionSummary]
    candidates: list[CandidateSummary]
    macro_regime: MacroRegimeSummary
    orders: list[OrderSummary]
    performance_tail: list[PerformancePoint]
    data_health: DataHealthSummary

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary and validate confidentiality."""
        payload = asdict(self)
        validate_snapshot_dict(payload)
        return payload

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent)


def validate_snapshot_dict(payload: dict[str, Any]) -> None:
    """Validate that snapshot schema contract is satisfied and has no secrets.

    Raises DeliveryError if schema violation or credential leakage is detected.
    """
    if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        curr_ver = payload.get("schema_version")
        raise DeliveryError(
            f"Invalid schema version: {curr_ver} (expected {SNAPSHOT_SCHEMA_VERSION})"
        )

    required_top_keys = {
        "schema_version",
        "as_of_session",
        "generated_at",
        "stale_after",
        "config_hash",
        "code_sha",
        "data_manifest_hash",
        "nav",
        "cash",
        "positions_value",
        "positions",
        "candidates",
        "macro_regime",
        "orders",
        "performance_tail",
        "data_health",
    }
    missing = required_top_keys - set(payload.keys())
    if missing:
        raise DeliveryError(f"Snapshot missing required keys: {sorted(missing)}")

    # Audit all keys and string values for credential / secret leakage
    def _scan(obj: Any, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_str = str(k)
                for pattern in SENSITIVE_KEY_PATTERNS:
                    if pattern.match(k_str) and k_str not in {"security_id"}:
                        raise DeliveryError(
                            f"Prohibited sensitive key '{k_str}' detected at {path}"
                        )
                _scan(v, f"{path}.{k_str}" if path else k_str)
        elif isinstance(obj, list):
            for i, elem in enumerate(obj):
                _scan(elem, f"{path}[{i}]")
        elif isinstance(obj, str):
            for pattern in SENSITIVE_VALUE_PATTERNS:
                if pattern.search(obj) and obj not in {
                    payload.get("config_hash"),
                    payload.get("code_sha"),
                    payload.get("data_manifest_hash"),
                }:
                    raise DeliveryError(
                        f"Suspicious sensitive token pattern detected at {path}: '{obj[:10]}...'"
                    )

    _scan(payload)


def export_snapshot(snapshot: DeliverySnapshot, output_path: Path) -> Path:
    """Safely export snapshot to the specified path."""
    payload = snapshot.to_json()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(payload, encoding="utf-8")
    return output_path


def load_snapshot(input_path: Path) -> DeliverySnapshot:
    """Load and validate a DeliverySnapshot from disk."""
    data = json.loads(input_path.read_text(encoding="utf-8"))
    validate_snapshot_dict(data)

    positions = [PositionSummary(**p) for p in data["positions"]]
    candidates = [CandidateSummary(**c) for c in data["candidates"]]
    orders = [OrderSummary(**o) for o in data["orders"]]
    performance_tail = [PerformancePoint(**pt) for pt in data["performance_tail"]]
    macro_regime = MacroRegimeSummary(**data["macro_regime"])
    data_health = DataHealthSummary(**data["data_health"])

    return DeliverySnapshot(
        schema_version=data["schema_version"],
        as_of_session=data["as_of_session"],
        generated_at=data["generated_at"],
        stale_after=data["stale_after"],
        config_hash=data["config_hash"],
        code_sha=data["code_sha"],
        data_manifest_hash=data["data_manifest_hash"],
        nav=data["nav"],
        cash=data["cash"],
        positions_value=data["positions_value"],
        positions=positions,
        candidates=candidates,
        macro_regime=macro_regime,
        orders=orders,
        performance_tail=performance_tail,
        data_health=data_health,
    )
