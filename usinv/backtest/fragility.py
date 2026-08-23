"""Metric-specific plateau, simplicity and multiple-testing diagnostics."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from statistics import NormalDist, median
from types import MappingProxyType
from typing import Any

from usinv.backtest.experiments import (
    CATEGORICAL_FIELDS,
    ORDINAL_FIELDS,
    ExperimentConfig,
    ExperimentGrid,
    ExperimentProtocolError,
    SearchMetrics,
)


@dataclass(frozen=True, slots=True)
class PlateauAudit:
    stable: bool
    ordinal_neighbor_count: int
    median_net_sharpe: float
    median_annualized_net_alpha: float
    median_ulcer_index: float
    median_max_drawdown_magnitude: float
    edge_axes: tuple[str, ...]
    categorical_failures: tuple[str, ...]
    reasons: tuple[str, ...]


class HoldoutVerdict(StrEnum):
    PASS = "pass"
    PRESUMED_OVERFIT = "presumed_overfit"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class HoldoutAssessment:
    net_alpha: float
    net_sharpe: float
    max_drawdown: float
    benchmark_max_drawdown: float
    ulcer_index: float
    benchmark_ulcer_index: float
    rolling_12m_win_rate: float
    no_calendar_year_worse_than_benchmark_minus_10pp: bool
    alpha_at_75bps: float
    overlay: str


@dataclass(frozen=True, slots=True)
class FragilityScenario:
    name: str
    base_config_hash: str
    overrides: Mapping[str, Any]

    @property
    def scenario_hash(self) -> str:
        payload = {
            "name": self.name,
            "base_config_hash": self.base_config_hash,
            "overrides": dict(self.overrides),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def active_layer_count(config: ExperimentConfig) -> int:
    """Count implementation layers for the registered simplicity tie-break."""
    return sum(
        (
            config.large_cap_max_slots > 0,
            config.band != "none",
            config.correlation_filter != "off",
            config.trailing_stop != "none",
            config.overlay != "O0",
            config.sector_relative_ranks,
            config.factor_weights != "equal",
        )
    )


def simplicity_key(
    config: ExperimentConfig,
    metrics: SearchMetrics,
) -> tuple[float | int | str, ...]:
    """Fewer layers, lower turnover, larger N, slower rotation, then hash."""
    return (
        active_layer_count(config),
        metrics.one_way_turnover,
        -config.holdings,
        -config.rotation_weeks,
        config.config_hash,
    )


def registered_fragility_scenarios(
    config: ExperimentConfig,
) -> tuple[FragilityScenario, ...]:
    """Return the named, non-grid robustness checks from EXPERIMENT_PLAN §3."""
    scenarios = (
        ("overlay_signal_iwm", {"overlay_signal_index": "IWM_TR"}),
        ("adv_floor_500k", {"adv_floor_usd": 500_000}),
        ("adv_floor_2m", {"adv_floor_usd": 2_000_000}),
        ("cap_band_lower_edge", {"cap_band_edge": "lower"}),
        ("cap_band_upper_edge", {"cap_band_edge": "upper"}),
        ("rotation_anchor_minus_1_week", {"rotation_anchor_offset_weeks": -1}),
        ("rotation_anchor_plus_1_week", {"rotation_anchor_offset_weeks": 1}),
    )
    return tuple(
        FragilityScenario(name, config.config_hash, MappingProxyType(overrides))
        for name, overrides in scenarios
    )


def classify_holdout(assessment: HoldoutAssessment) -> HoldoutVerdict:
    """Apply the fixed TEST pass/fail and presumed-overfit thresholds."""
    numeric = tuple(
        value
        for key, value in asdict(assessment).items()
        if key
        not in {
            "no_calendar_year_worse_than_benchmark_minus_10pp",
            "overlay",
        }
    )
    if any(not math.isfinite(value) for value in numeric):
        raise ExperimentProtocolError("holdout assessment metrics must be finite")
    if assessment.net_alpha > 0.06 or assessment.net_sharpe > 1.3:
        return HoldoutVerdict.PRESUMED_OVERFIT
    drawdown_multiplier = 1.0 if assessment.overlay == "O0" else 0.7
    passes = all(
        (
            assessment.net_alpha >= 0.02,
            abs(assessment.max_drawdown)
            <= drawdown_multiplier * abs(assessment.benchmark_max_drawdown),
            assessment.ulcer_index <= assessment.benchmark_ulcer_index,
            assessment.rolling_12m_win_rate >= 0.55,
            assessment.no_calendar_year_worse_than_benchmark_minus_10pp,
            assessment.alpha_at_75bps >= 0,
        )
    )
    return HoldoutVerdict.PASS if passes else HoldoutVerdict.FAIL


def evaluate_plateau(
    grid: ExperimentGrid,
    finalist: ExperimentConfig,
    results: dict[str, SearchMetrics],
) -> PlateauAudit:
    """Apply the exact Stage-3 ordinal and categorical tolerance rules."""
    try:
        finalist_metrics = results[finalist.config_hash]
    except KeyError as exc:
        raise ExperimentProtocolError("finalist metrics are missing") from exc

    ordinal_metrics: list[SearchMetrics] = []
    edge_axes: list[str] = []
    for field in ORDINAL_FIELDS:
        axis = grid.axes[field]
        index = axis.index(getattr(finalist, field))
        neighbor_indices = tuple(item for item in (index - 1, index + 1) if 0 <= item < len(axis))
        if len(neighbor_indices) == 1:
            edge_axes.append(field)
        for neighbor_index in neighbor_indices:
            neighbor = replace(finalist, **{field: axis[neighbor_index]})
            if neighbor.config_hash not in results:
                raise ExperimentProtocolError(
                    f"Stage 3 result missing ordinal neighbor {field}={axis[neighbor_index]}"
                )
            ordinal_metrics.append(results[neighbor.config_hash])
    if not ordinal_metrics:
        raise ExperimentProtocolError("plateau audit has no ordinal neighbors")

    median_sharpe = median(item.net_sharpe for item in ordinal_metrics)
    median_alpha = median(item.annualized_net_alpha for item in ordinal_metrics)
    median_ulcer = median(item.ulcer_index for item in ordinal_metrics)
    median_drawdown = median(abs(item.max_drawdown) for item in ordinal_metrics)
    reasons: list[str] = []
    if median_sharpe < finalist_metrics.net_sharpe - 0.15:
        reasons.append("ordinal median Sharpe is more than 0.15 below finalist")
    if median_alpha < finalist_metrics.annualized_net_alpha - 0.01:
        reasons.append("ordinal median alpha is more than 1pp below finalist")
    if median_ulcer > 1.20 * finalist_metrics.ulcer_index:
        reasons.append("ordinal median Ulcer exceeds 1.20x finalist")
    if median_drawdown > abs(finalist_metrics.max_drawdown) + 0.05:
        reasons.append("ordinal median MaxDD magnitude exceeds finalist by more than 5pp")

    categorical_failures: list[str] = []
    for field in CATEGORICAL_FIELDS:
        alternatives: list[tuple[ExperimentConfig, SearchMetrics]] = []
        for value in grid.axes[field]:
            if value == getattr(finalist, field):
                continue
            alternative = replace(finalist, **{field: value})
            try:
                metric = results[alternative.config_hash]
            except KeyError as exc:
                raise ExperimentProtocolError(
                    f"Stage 3 result missing categorical alternative {field}={value}"
                ) from exc
            alternatives.append((alternative, metric))
        best_config, best_metrics = max(
            alternatives,
            key=lambda item: (item[1].net_sharpe, item[0].config_hash),
        )
        if best_metrics.net_sharpe <= finalist_metrics.net_sharpe:
            continue
        within_tolerance = finalist_metrics.net_sharpe >= best_metrics.net_sharpe - 0.05
        chosen_is_simpler = (
            active_layer_count(finalist) < active_layer_count(best_config)
            or finalist_metrics.one_way_turnover <= best_metrics.one_way_turnover
        )
        if not (within_tolerance and chosen_is_simpler):
            categorical_failures.append(field)
    if categorical_failures:
        reasons.append("categorical alternatives beat the chosen value outside simplicity rule")

    return PlateauAudit(
        stable=not reasons,
        ordinal_neighbor_count=len(ordinal_metrics),
        median_net_sharpe=median_sharpe,
        median_annualized_net_alpha=median_alpha,
        median_ulcer_index=median_ulcer,
        median_max_drawdown_magnitude=median_drawdown,
        edge_axes=tuple(edge_axes),
        categorical_failures=tuple(categorical_failures),
        reasons=tuple(reasons),
    )


def control_selection(
    *,
    grid: ExperimentGrid,
    chosen: ExperimentConfig,
    results: dict[str, SearchMetrics],
) -> ExperimentConfig:
    """Apply C0 comparison and the within-5% simplicity ablation rule."""
    controls = grid.control_cells(chosen)
    if chosen.config_hash not in results:
        raise ExperimentProtocolError("chosen configuration metrics are missing")
    chosen_metrics = results[chosen.config_hash]
    default_metrics = results.get(grid.default.config_hash)
    if default_metrics is None:
        raise ExperimentProtocolError("C0 metrics are missing")
    if not (
        chosen_metrics.net_sharpe > default_metrics.net_sharpe
        and chosen_metrics.ulcer_index < default_metrics.ulcer_index
    ):
        raise ExperimentProtocolError("chosen configuration does not beat C0 on Sharpe and Ulcer")

    matching: list[ExperimentConfig] = [chosen]
    for control in controls:
        if control.config_hash in {chosen.config_hash, grid.default.config_hash}:
            continue
        metrics = results.get(control.config_hash)
        if metrics is None:
            raise ExperimentProtocolError("an ablation control result is missing")
        sharpe_floor = chosen_metrics.net_sharpe - 0.05 * abs(chosen_metrics.net_sharpe)
        if (
            metrics.net_sharpe >= sharpe_floor
            and metrics.ulcer_index <= chosen_metrics.ulcer_index * 1.05
        ):
            matching.append(control)
    return min(matching, key=lambda cell: simplicity_key(cell, results[cell.config_hash]))


def deflated_sharpe_probability(
    *,
    observed_sharpe: float,
    actual_distinct_trials: int,
    return_observations: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Return a Bailey/López-de-Prado-style multiple-trial Sharpe sanity score."""
    values = (observed_sharpe, skew, kurtosis)
    if any(not math.isfinite(value) for value in values):
        raise ExperimentProtocolError("deflated-Sharpe inputs must be finite")
    if not 1 <= actual_distinct_trials <= 190:
        raise ExperimentProtocolError("actual distinct trial count must be in [1, 190]")
    if return_observations < 2:
        raise ExperimentProtocolError("deflated Sharpe requires at least two observations")
    if actual_distinct_trials == 1:
        expected_maximum = 0.0
    else:
        normal = NormalDist()
        euler_gamma = 0.5772156649015329
        expected_maximum = (1 - euler_gamma) * normal.inv_cdf(
            1 - 1 / actual_distinct_trials
        ) + euler_gamma * normal.inv_cdf(1 - 1 / (actual_distinct_trials * math.e))
    variance = (1 - skew * observed_sharpe + ((kurtosis - 1) / 4) * observed_sharpe**2) / (
        return_observations - 1
    )
    if variance <= 0:
        raise ExperimentProtocolError("deflated-Sharpe variance is non-positive")
    z_score = (observed_sharpe - expected_maximum) / math.sqrt(variance)
    return NormalDist().cdf(z_score)
