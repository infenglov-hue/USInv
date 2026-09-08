"""Golden contract and leak prevention tests for snapshot.json and delivery layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from usinv.delivery.snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    CandidateSummary,
    DataHealthSummary,
    DeliveryError,
    DeliverySnapshot,
    MacroRegimeSummary,
    OrderSummary,
    PerformancePoint,
    PositionSummary,
    export_snapshot,
    load_snapshot,
    validate_snapshot_dict,
)


def _sample_snapshot() -> DeliverySnapshot:
    return DeliverySnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        as_of_session="2026-07-17",
        generated_at="2026-07-17T20:00:00Z",
        stale_after="2026-07-20T16:00:00Z",
        config_hash="d3ecd3bd787897c9abfd450d97e33cbfa90de1d9f762d92760789e4ada17cb72",
        code_sha="80f09f3c1d4a89e9f136b6cbef5847db1d12fae2",
        data_manifest_hash="05bdd470475a6c71dd288108a97034fed37000a0492e15cc47a950c3d164ad1b",
        nav=105420.50,
        cash=10542.00,
        positions_value=94878.50,
        positions=[
            PositionSummary(
                security_id="SEC:0000320193:AAPL",
                ticker="AAPL",
                company_name="Apple Inc.",
                shares=150.0,
                cost_basis=185.20,
                current_price=220.50,
                market_value=33075.00,
                weight_pct=31.37,
                stop_price=176.40,
                high_water_mark=225.00,
                entry_session="2026-06-01",
            ),
            PositionSummary(
                security_id="SEC:0001018724:AMZN",
                ticker="AMZN",
                company_name="Amazon.com Inc.",
                shares=180.0,
                cost_basis=165.40,
                current_price=188.90,
                market_value=34002.00,
                weight_pct=32.25,
                stop_price=151.12,
                high_water_mark=192.00,
                entry_session="2026-06-01",
            ),
        ],
        candidates=[
            CandidateSummary(
                rank=1,
                security_id="SEC:0000320193:AAPL",
                ticker="AAPL",
                company_name="Apple Inc.",
                sector="Computers",
                core_or_large="large",
                composite_score=0.884,
                factor_ranks={
                    "momentum": 0.92,
                    "value": 0.54,
                    "quality": 0.96,
                    "low_vol": 0.72,
                    "size": 0.40,
                },
                red_flag_status="CLEAN",
                entry_reference_price=220.50,
            )
        ],
        macro_regime=MacroRegimeSummary(
            regime="NORMAL",
            overlay="O1",
            equity_exposure_target=1.0,
            signal_summary="SPY > 200 SMA; Drawdown < 6.0%; Overlay inactive",
            benchmark_dd=-0.024,
            as_of_session="2026-07-17",
        ),
        orders=[
            OrderSummary(
                order_id="ord_20260717_001",
                client_order_id="usinv_20260717_SEC000032019_buy_a1",
                security_id="SEC:0000320193:AAPL",
                ticker="AAPL",
                side="buy",
                quantity=150.0,
                reference_close=220.50,
                limit_price=224.91,
                collar_pct=0.02,
                status="pending",
                reason="entry",
            )
        ],
        performance_tail=[
            PerformancePoint(
                session="2026-07-16",
                nav=104800.0,
                benchmark_nav=101200.0,
                daily_return=0.0059,
                benchmark_daily_return=0.0032,
            ),
            PerformancePoint(
                session="2026-07-17",
                nav=105420.5,
                benchmark_nav=101500.0,
                daily_return=0.0059,
                benchmark_daily_return=0.0030,
            ),
        ],
        data_health=DataHealthSummary(
            status="HEALTHY",
            freshness_evaluated_at="2026-07-17T20:00:00Z",
            core_coverage_pct=91.57,
            secondary_coverage_pct=78.75,
            identity_gaps=0,
            sector_gaps=0,
            warnings=[],
        ),
    )


def test_snapshot_golden_contract():
    """Verify that a valid snapshot satisfies all required contract fields."""
    snap = _sample_snapshot()
    payload = snap.to_dict()
    assert payload["schema_version"] == "1.0.0"
    assert payload["as_of_session"] == "2026-07-17"
    assert payload["nav"] == 105420.50
    assert len(payload["positions"]) == 2
    assert len(payload["candidates"]) == 1
    assert payload["data_health"]["identity_gaps"] == 0


def test_snapshot_rejection_of_prohibited_keys():
    """Verify that any sensitive key names trigger a DeliveryError."""
    snap_dict = _sample_snapshot().to_dict()
    snap_dict["alpaca_api_key"] = "test-value"

    with pytest.raises(DeliveryError, match="Prohibited sensitive key"):
        validate_snapshot_dict(snap_dict)

    snap_dict_nested = _sample_snapshot().to_dict()
    snap_dict_nested["data_health"]["broker_secret"] = "xyz"
    with pytest.raises(DeliveryError, match="Prohibited sensitive key"):
        validate_snapshot_dict(snap_dict_nested)


def test_snapshot_rejection_of_prohibited_token_patterns():
    """Verify that raw credential strings trigger a DeliveryError."""
    snap_dict = _sample_snapshot().to_dict()
    snap_dict["data_health"]["warnings"].append("Failed key PK123456789012345678")
    with pytest.raises(DeliveryError, match="Suspicious sensitive token pattern"):
        validate_snapshot_dict(snap_dict)


def test_snapshot_export_and_load(tmp_path: Path):
    """Verify export to JSON and load round-trip fidelity."""
    snap = _sample_snapshot()
    out_file = tmp_path / "snapshot.json"
    export_snapshot(snap, out_file)
    assert out_file.exists()

    loaded = load_snapshot(out_file)
    assert loaded.config_hash == snap.config_hash
    assert loaded.nav == snap.nav
    assert len(loaded.positions) == 2
    assert loaded.macro_regime.regime == "NORMAL"
