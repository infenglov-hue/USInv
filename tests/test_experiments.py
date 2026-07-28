"""Phase 4.4 locked splits, staged search, fragility and TEST gate."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from usinv.backtest import (
    ExperimentGrid,
    ExperimentLedger,
    ExperimentProtocolError,
    ExperimentRunner,
    FrozenDataManifest,
    HoldoutAssessment,
    HoldoutVerdict,
    SearchMetrics,
    TestUnlockError,
    TestUnlockRegistry,
    active_layer_count,
    build_locked_splits,
    classify_holdout,
    control_selection,
    deflated_sharpe_probability,
    evaluate_plateau,
    registered_fragility_scenarios,
    rolling_train_diagnostics,
    run_locked_test,
)
from usinv.config import load_config

DATA_HASH = "d" * 64
SPLIT_HASH = "e" * 64
CODE_SHA = "a" * 40


def _grid() -> ExperimentGrid:
    return ExperimentGrid.from_config(load_config().experiment_grid)


def _metrics(
    sharpe: float = 0.8,
    *,
    alpha: float = 0.03,
    ulcer: float = 0.10,
    drawdown: float = -0.20,
    turnover: float = 0.30,
    steady: bool = True,
) -> SearchMetrics:
    return SearchMetrics(sharpe, alpha, ulcer, drawdown, turnover, steady)


def test_locked_splits_purge_full_horizon_and_keep_exact_test_dates():
    horizon = 126
    splits = build_locked_splits(maximum_holding_horizon_sessions=horizon)
    alternative = build_locked_splits(maximum_holding_horizon_sessions=horizon + 1)
    assert splits.test[0] == date(2023, 1, 3)
    assert splits.test[-1] == date(2026, 6, 30)
    assert splits.raw_validation[0] == date(2019, 1, 2)
    assert splits.protocol_hash != alternative.protocol_hash
    assert all(len(boundary.purged_sessions) == horizon for boundary in splits.boundaries)

    for usable, raw, boundary in (
        (splits.train, splits.raw_train, splits.boundaries[0]),
        (splits.validation, splits.raw_validation, splits.boundaries[1]),
    ):
        last_index = raw.index(usable[-1])
        assert raw[last_index + 1 : last_index + 1 + horizon] == boundary.purged_sessions
        assert boundary.last_usable_session == usable[-1]


def test_rolling_diagnostics_are_train_only_and_cannot_select_final_config():
    splits = build_locked_splits(maximum_holding_horizon_sessions=126)
    diagnostics = rolling_train_diagnostics(
        splits,
        minimum_fit_sessions=756,
        evaluation_sessions=126,
        step_sessions=126,
    )
    assert diagnostics
    assert all(item.evaluation_sessions[-1] <= date(2018, 12, 31) for item in diagnostics)
    assert all(len(item.purged_sessions) == 126 for item in diagnostics)
    assert all(item.may_select_final_config is False for item in diagnostics)


def test_grid_counts_stage1_and_stage2_and_mandated_interactions():
    grid = _grid()
    stage1 = grid.stage1_cells()
    assert len(stage1) == 29
    results = {cell.config_hash: _metrics(0.5 + index / 100) for index, cell in enumerate(stage1)}
    selection = grid.select_stage1(results)
    stage2 = grid.stage2_cells(selection)
    assert len(stage2) == 96
    assert set(selection.surviving_values["trailing_stop"]) == {"percent_20", "none"}
    assert set(selection.surviving_values["overlay"]) == {"O0", "O1"}
    assert {(cell.overlay, cell.trailing_stop) for cell in stage2} >= {
        ("O1", "none"),
        ("O0", "percent_20"),
        ("O1", "percent_20"),
        ("O0", "none"),
    }


def test_stage3_uses_original_neighbors_deduplicates_and_stays_under_cap():
    grid = _grid()
    stage1 = grid.stage1_cells()
    stage1_results = {cell.config_hash: _metrics() for cell in stage1}
    stage2 = grid.stage2_cells(grid.select_stage1(stage1_results))
    stage2_results = {
        cell.config_hash: _metrics(1 - index / 1000) for index, cell in enumerate(stage2)
    }
    finalists = grid.finalists(stage2, stage2_results)
    completed = frozenset(cell.config_hash for cell in (*stage1, *stage2))
    stage3 = grid.stage3_cells(finalists, completed_config_hashes=completed)
    assert len(finalists) == 3
    assert len(stage3) <= 60
    assert len({cell.config_hash for cell in stage3}) == len(stage3)
    assert not completed.intersection(cell.config_hash for cell in stage3)
    total = {
        cell.config_hash for cell in (*stage1, *stage2, *stage3, *grid.control_cells(finalists[0]))
    }
    assert len(total) <= 190


def test_plateau_uses_metric_specific_tolerances_and_reports_axis_edges():
    grid = _grid()
    finalist = replace(
        grid.default,
        holdings=8,
        rotation_weeks=2,
        band="none",
        sector_cap_fraction=0.20,
        trailing_stop="none",
    )
    neighbors = grid.stage3_cells((finalist,))
    results = {finalist.config_hash: _metrics(sharpe=1.0)}
    results.update({cell.config_hash: _metrics(sharpe=0.9) for cell in neighbors})
    audit = evaluate_plateau(grid, finalist, results)
    assert audit.stable
    assert set(audit.edge_axes) == {
        "holdings",
        "rotation_weeks",
        "band",
        "sector_cap_fraction",
        "trailing_stop",
    }

    ordinal_neighbor = replace(finalist, holdings=10)
    results[ordinal_neighbor.config_hash] = _metrics(
        sharpe=0.1,
        alpha=-0.10,
        ulcer=0.40,
        drawdown=-0.50,
    )
    for field, value in (
        ("rotation_weeks", 4),
        ("band", "quintile"),
        ("sector_cap_fraction", 0.27),
        ("trailing_stop", "percent_15"),
    ):
        cell = replace(finalist, **{field: value})
        results[cell.config_hash] = results[ordinal_neighbor.config_hash]
    assert not evaluate_plateau(grid, finalist, results).stable


def test_control_rule_ships_matching_simpler_ablation():
    grid = _grid()
    chosen = replace(
        grid.default,
        overlay="O1",
        trailing_stop="percent_25",
        band="top_third",
        factor_weights="quality_tilt",
    )
    results = {
        chosen.config_hash: _metrics(1.0, ulcer=0.10, turnover=0.40),
        grid.default.config_hash: _metrics(0.8, ulcer=0.12),
    }
    for control in grid.control_cells(chosen):
        results.setdefault(
            control.config_hash,
            _metrics(0.97, ulcer=0.104, turnover=0.35),
        )
    selected = control_selection(grid=grid, chosen=chosen, results=results)
    assert active_layer_count(selected) < active_layer_count(chosen)


def test_runner_resumes_after_failure_and_deduplicates_exact_cells(tmp_path: Path):
    cells = _grid().stage1_cells()[:3]
    ledger = ExperimentLedger(tmp_path / "runs.jsonl")
    runner = ExperimentRunner(
        ledger=ledger,
        data_manifest_hash=DATA_HASH,
        split_protocol_hash=SPLIT_HASH,
        code_sha=CODE_SHA,
    )
    calls: list[str] = []
    fail_once = {cells[1].config_hash}

    def interrupted(cell):
        calls.append(cell.config_hash)
        if cell.config_hash in fail_once:
            fail_once.remove(cell.config_hash)
            raise RuntimeError("synthetic interruption")
        return _metrics()

    with pytest.raises(RuntimeError, match="interruption"):
        runner.run_cells(
            stage="stage1",
            split="validation",
            cells=cells,
            evaluator=interrupted,
        )
    resumed = runner.run_cells(
        stage="stage1",
        split="validation",
        cells=cells,
        evaluator=interrupted,
    )
    assert len(resumed) == 2
    assert (
        len(
            runner.run_cells(
                stage="stage1",
                split="validation",
                cells=(*cells, cells[0]),
                evaluator=interrupted,
            )
        )
        == 0
    )
    records = ledger.records()
    assert [item.status for item in records] == ["complete", "failed", "complete", "complete"]
    assert all(item.data_manifest_hash == DATA_HASH for item in records)
    assert all(item.split_protocol_hash == SPLIT_HASH for item in records)
    assert all(item.code_sha == CODE_SHA for item in records)
    assert all(
        item.config_hash and item.output_hash for item in records if item.status == "complete"
    )
    different_split_runner = ExperimentRunner(
        ledger=ledger,
        data_manifest_hash=DATA_HASH,
        split_protocol_hash="f" * 64,
        code_sha=CODE_SHA,
    )
    assert (
        len(
            different_split_runner.run_cells(
                stage="stage1",
                split="validation",
                cells=(cells[0],),
                evaluator=interrupted,
            )
        )
        == 1
    )


def test_ledger_detects_tampering(tmp_path: Path):
    ledger = ExperimentLedger(tmp_path / "runs.jsonl")
    runner = ExperimentRunner(
        ledger=ledger,
        data_manifest_hash=DATA_HASH,
        split_protocol_hash=SPLIT_HASH,
        code_sha=CODE_SHA,
    )
    runner.run_cells(
        stage="stage1",
        split="train",
        cells=(_grid().default,),
        evaluator=lambda _cell: _metrics(),
    )
    payload = json.loads(ledger.path.read_text(encoding="utf-8"))
    payload["status"] = "failed"
    ledger.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ExperimentProtocolError, match="hash"):
        ledger.records()


def test_frozen_data_manifest_hashes_required_quality_and_range():
    manifest = FrozenDataManifest(
        {"prices": "1" * 64, "fundamentals": "2" * 64},
        "research",
        unresolved_action_count=3,
        unmapped_security_count=4,
        first_session=date(2012, 1, 3),
        last_session=date(2026, 6, 30),
    )
    assert len(manifest.manifest_hash) == 64
    changed = replace(manifest, unresolved_action_count=4)
    assert changed.manifest_hash != manifest.manifest_hash


def test_test_runner_requires_registration_seal_hashes_and_one_time_unlock(tmp_path: Path):
    grid = _grid()
    config = grid.default
    splits = build_locked_splits(maximum_holding_horizon_sessions=126)
    ledger = ExperimentLedger(tmp_path / "runs.jsonl")
    runner = ExperimentRunner(
        ledger=ledger,
        data_manifest_hash=DATA_HASH,
        split_protocol_hash=splits.protocol_hash,
        code_sha=CODE_SHA,
    )
    runner.run_cells(
        stage="train_diagnostic",
        split="train",
        cells=(config,),
        evaluator=lambda _cell: _metrics(),
    )
    runner.run_cells(
        stage="stage2",
        split="validation",
        cells=(config,),
        evaluator=lambda _cell: _metrics(),
    )
    seal = ledger.train_validation_seal()
    registry = TestUnlockRegistry(tmp_path / "test_registry.json")
    unregistered = TestUnlockRegistry(tmp_path / "unregistered.json")
    with pytest.raises(TestUnlockError, match="not registered"):
        run_locked_test(
            splits=splits,
            config=config,
            evaluator=lambda _cell: _metrics(),
            runner=runner,
            registry=unregistered,
            unlock_token="user-secret-token",
        )
    registry.register(
        final_config_hash=config.config_hash,
        train_validation_seal=seal,
        data_manifest_hash=DATA_HASH,
        split_protocol_hash=splits.protocol_hash,
        code_sha=CODE_SHA,
        unlock_token="user-secret-token",
    )
    with pytest.raises(TestUnlockError, match="token"):
        run_locked_test(
            splits=splits,
            config=config,
            evaluator=lambda _cell: _metrics(),
            runner=runner,
            registry=registry,
            unlock_token="wrong",
        )
    record = run_locked_test(
        splits=splits,
        config=config,
        evaluator=lambda _cell: _metrics(0.7),
        runner=runner,
        registry=registry,
        unlock_token="user-secret-token",
    )
    assert record.split == "test"
    assert record.config_hash == config.config_hash
    with pytest.raises(TestUnlockError, match="already"):
        run_locked_test(
            splits=splits,
            config=config,
            evaluator=lambda _cell: _metrics(),
            runner=runner,
            registry=registry,
            unlock_token="user-secret-token",
        )


def test_unlock_is_burned_before_failed_test_evaluator_runs(tmp_path: Path):
    config = _grid().default
    splits = build_locked_splits(maximum_holding_horizon_sessions=126)
    ledger = ExperimentLedger(tmp_path / "runs.jsonl")
    runner = ExperimentRunner(
        ledger=ledger,
        data_manifest_hash=DATA_HASH,
        split_protocol_hash=splits.protocol_hash,
        code_sha=CODE_SHA,
    )
    for split in ("train", "validation"):
        runner.run_cells(
            stage=split,
            split=split,
            cells=(config,),
            evaluator=lambda _cell: _metrics(),
        )
    registry = TestUnlockRegistry(tmp_path / "registry.json")
    registry.register(
        final_config_hash=config.config_hash,
        train_validation_seal=ledger.train_validation_seal(),
        data_manifest_hash=DATA_HASH,
        split_protocol_hash=splits.protocol_hash,
        code_sha=CODE_SHA,
        unlock_token="burn",
    )

    def fail(_cell):
        raise RuntimeError("test evaluator failed")

    with pytest.raises(RuntimeError, match="failed"):
        run_locked_test(
            splits=splits,
            config=config,
            evaluator=fail,
            runner=runner,
            registry=registry,
            unlock_token="burn",
        )
    with pytest.raises(TestUnlockError, match="already"):
        run_locked_test(
            splits=splits,
            config=config,
            evaluator=lambda _cell: _metrics(),
            runner=runner,
            registry=registry,
            unlock_token="burn",
        )


def test_deflated_sharpe_uses_actual_distinct_trial_count():
    one_trial = deflated_sharpe_probability(
        observed_sharpe=1.0,
        actual_distinct_trials=1,
        return_observations=120,
    )
    many_trials = deflated_sharpe_probability(
        observed_sharpe=1.0,
        actual_distinct_trials=190,
        return_observations=120,
    )
    assert many_trials < one_trial
    with pytest.raises(ExperimentProtocolError, match="190"):
        deflated_sharpe_probability(
            observed_sharpe=1.0,
            actual_distinct_trials=191,
            return_observations=120,
        )


def test_named_fragility_scenarios_are_fixed_hashed_and_not_grid_axes():
    scenarios = registered_fragility_scenarios(_grid().default)
    assert {item.name for item in scenarios} == {
        "overlay_signal_iwm",
        "adv_floor_500k",
        "adv_floor_2m",
        "cap_band_lower_edge",
        "cap_band_upper_edge",
        "rotation_anchor_minus_1_week",
        "rotation_anchor_plus_1_week",
    }
    assert len({item.scenario_hash for item in scenarios}) == len(scenarios)


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        (
            HoldoutAssessment(
                0.03,
                1.0,
                -0.18,
                -0.20,
                0.08,
                0.10,
                0.60,
                True,
                0.005,
                "O0",
            ),
            HoldoutVerdict.PASS,
        ),
        (
            HoldoutAssessment(
                0.07,
                1.0,
                -0.18,
                -0.20,
                0.08,
                0.10,
                0.60,
                True,
                0.005,
                "O0",
            ),
            HoldoutVerdict.PRESUMED_OVERFIT,
        ),
        (
            HoldoutAssessment(
                0.01,
                0.5,
                -0.18,
                -0.20,
                0.08,
                0.10,
                0.60,
                True,
                -0.001,
                "O0",
            ),
            HoldoutVerdict.FAIL,
        ),
    ],
)
def test_holdout_verdict_uses_fixed_pass_and_overfit_thresholds(
    assessment: HoldoutAssessment,
    expected: HoldoutVerdict,
):
    assert classify_holdout(assessment) is expected
