"""Strict YAML-to-dataclass loader for the registered USInv defaults."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(ValueError):
    """Raised when configuration violates a declared contract."""


class EvidenceMode(StrEnum):
    UNDECIDED = "undecided"
    RESEARCH = "research"
    AUDIT = "audit"


class ExecutionMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class PathsConfig:
    data_dir: str
    cache_dir: str
    artifacts_dir: str


@dataclass(frozen=True, slots=True)
class TimezoneConfig:
    exchange: str
    display: str


@dataclass(frozen=True, slots=True)
class EdgarConfig:
    max_requests_per_second: int
    contact_env: str


@dataclass(frozen=True, slots=True)
class CredentialEnvConfig:
    alpaca_key_id: str
    alpaca_secret_key: str
    alpha_vantage_key: str
    tiingo_token: str
    fred_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str


@dataclass(frozen=True, slots=True)
class SettingsConfig:
    schema_version: int
    evidence_mode: EvidenceMode
    execution_mode: ExecutionMode
    paths: PathsConfig
    timezones: TimezoneConfig
    edgar: EdgarConfig
    credential_env: CredentialEnvConfig


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    exchanges: tuple[str, ...]
    domestic_only: bool
    common_stock_only: bool
    include_otc: bool
    raw_close_min_exclusive: float
    dollar_volume_window_sessions: int
    median_dollar_volume_min: float
    core_market_cap_min: float
    core_market_cap_max: float
    large_cap_min_exclusive: float
    excluded_groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FactorWeights:
    quality: float
    value: float
    momentum: float


@dataclass(frozen=True, slots=True)
class FactorsConfig:
    weights: FactorWeights
    quality_metrics: tuple[str, ...]
    value_metrics: tuple[str, ...]
    momentum_lookback_months: int
    momentum_skip_months: int
    piotroski_veto_max: int
    sector_relative: bool


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    holdings: int
    weighting: str
    large_cap_max_slots: int
    cash_return: float
    sector_cap_fraction: float
    correlation_enabled: bool
    correlation_threshold: float
    correlation_window_sessions: int
    rotation_weeks: int
    rotation_anchor_weekday: str
    entry_top_fraction: float
    hold_top_fraction: float
    turnover_window_sessions: int
    turnover_one_way_fraction: float
    trailing_stop: str
    execution_cost_bps: int
    cost_sensitivity_bps: tuple[int, ...]
    buy_collar_fraction: float
    routine_sell_collar_fraction: float
    thesis_sell_collar_fraction: float
    exit_retry_sessions: int
    funding_model: str


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    overlay_id: str
    signal: str
    evaluation: str
    risk_off_exposure: float
    hysteresis_fraction: float
    credit_confirm: bool
    hy_oas_threshold_bps: int
    hy_oas_ma_sessions: int


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    default_overlay: str
    overlays: tuple[OverlayConfig, ...]


@dataclass(frozen=True, slots=True)
class ExperimentGridConfig:
    holdings: tuple[int, ...]
    large_cap_max_slots: tuple[int, ...]
    rotation_weeks: tuple[int, ...]
    bands: tuple[str, ...]
    sector_cap_fractions: tuple[float, ...]
    correlation_filters: tuple[str, ...]
    trailing_stops: tuple[str, ...]
    overlays: tuple[str, ...]
    sector_relative_ranks: tuple[bool, ...]
    factor_weights: tuple[str, ...]
    full_cross_cells: int
    stage1_max: int
    stage2_max: int
    stage3_max: int
    controls_max: int
    planned_max: int


@dataclass(frozen=True, slots=True)
class FreshnessConfig:
    """Phase 2.4 freshness / kill-switch thresholds (DATA_SPEC §8)."""

    fundamentals_stale_fraction_max: float
    fundamentals_grace_business_days: int
    form10q_due_days_large_accelerated: int
    form10q_due_days_accelerated: int
    form10q_due_days_other: int
    form10k_due_days_large_accelerated: int
    form10k_due_days_accelerated: int
    form10k_due_days_other: int
    price_max_sessions_stale: int
    macro_max_age_cadence_multiple: int

    def due_window_days(self, form: str, filer_category: str) -> int:
        """Return the periodic-report due window in calendar days for a form/filer."""
        table = {
            ("10-Q", "large_accelerated"): self.form10q_due_days_large_accelerated,
            ("10-Q", "accelerated"): self.form10q_due_days_accelerated,
            ("10-Q", "other"): self.form10q_due_days_other,
            ("10-K", "large_accelerated"): self.form10k_due_days_large_accelerated,
            ("10-K", "accelerated"): self.form10k_due_days_accelerated,
            ("10-K", "other"): self.form10k_due_days_other,
        }
        try:
            return table[(form, filer_category)]
        except KeyError as exc:
            raise ConfigError(f"no due window for form={form!r} filer={filer_category!r}") from exc


@dataclass(frozen=True, slots=True)
class AppConfig:
    settings: SettingsConfig
    universe: UniverseConfig
    factors: FactorsConfig
    portfolio: PortfolioConfig
    regime: RegimeConfig
    experiment_grid: ExperimentGridConfig
    freshness: FreshnessConfig

    @property
    def config_hash(self) -> str:
        """Return a stable hash for the complete declared configuration."""
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{context} must be a mapping")
    return value


def _strict(value: Any, expected: set[str], context: str) -> Mapping[str, Any]:
    mapping = _mapping(value, context)
    actual = set(mapping)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise ConfigError(f"{context} keys invalid; missing={missing}, unknown={unknown}")
    return mapping


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must be a non-empty string")
    return value


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be a boolean")
    return value


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{context} must be an integer")
    return value


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{context} must be finite")
    return result


def _tuple(value: Any, converter: Any, context: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConfigError(f"{context} must be a sequence")
    result = tuple(converter(item, f"{context}[{index}]") for index, item in enumerate(value))
    if not result:
        raise ConfigError(f"{context} cannot be empty")
    return result


def _read_yaml(directory: Path, filename: str) -> Mapping[str, Any]:
    path = directory / filename
    if not path.is_file():
        raise ConfigError(f"missing configuration file: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    return _mapping(payload, filename)


def _parse_settings(raw: Mapping[str, Any]) -> SettingsConfig:
    node = _strict(
        raw,
        {
            "schema_version",
            "evidence_mode",
            "execution_mode",
            "paths",
            "timezones",
            "edgar",
            "credential_env",
        },
        "settings",
    )
    paths = _strict(node["paths"], {"data_dir", "cache_dir", "artifacts_dir"}, "settings.paths")
    timezones = _strict(node["timezones"], {"exchange", "display"}, "settings.timezones")
    edgar = _strict(
        node["edgar"],
        {"max_requests_per_second", "contact_env"},
        "settings.edgar",
    )
    credential_keys = {
        "alpaca_key_id",
        "alpaca_secret_key",
        "alpha_vantage_key",
        "tiingo_token",
        "fred_api_key",
        "telegram_bot_token",
        "telegram_chat_id",
    }
    credentials = _strict(node["credential_env"], credential_keys, "settings.credential_env")
    try:
        evidence_mode = EvidenceMode(_string(node["evidence_mode"], "settings.evidence_mode"))
        execution_mode = ExecutionMode(_string(node["execution_mode"], "settings.execution_mode"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return SettingsConfig(
        schema_version=_integer(node["schema_version"], "settings.schema_version"),
        evidence_mode=evidence_mode,
        execution_mode=execution_mode,
        paths=PathsConfig(**{key: _string(paths[key], f"settings.paths.{key}") for key in paths}),
        timezones=TimezoneConfig(
            exchange=_string(timezones["exchange"], "settings.timezones.exchange"),
            display=_string(timezones["display"], "settings.timezones.display"),
        ),
        edgar=EdgarConfig(
            max_requests_per_second=_integer(
                edgar["max_requests_per_second"], "settings.edgar.max_requests_per_second"
            ),
            contact_env=_string(edgar["contact_env"], "settings.edgar.contact_env"),
        ),
        credential_env=CredentialEnvConfig(
            **{
                key: _string(credentials[key], f"settings.credential_env.{key}")
                for key in credentials
            }
        ),
    )


def _parse_universe(raw: Mapping[str, Any]) -> UniverseConfig:
    keys = {
        "exchanges",
        "domestic_only",
        "common_stock_only",
        "include_otc",
        "raw_close_min_exclusive",
        "dollar_volume_window_sessions",
        "median_dollar_volume_min",
        "core_market_cap_min",
        "core_market_cap_max",
        "large_cap_min_exclusive",
        "excluded_groups",
    }
    node = _strict(raw, keys, "universe")
    return UniverseConfig(
        exchanges=_tuple(node["exchanges"], _string, "universe.exchanges"),
        domestic_only=_boolean(node["domestic_only"], "universe.domestic_only"),
        common_stock_only=_boolean(node["common_stock_only"], "universe.common_stock_only"),
        include_otc=_boolean(node["include_otc"], "universe.include_otc"),
        raw_close_min_exclusive=_number(
            node["raw_close_min_exclusive"], "universe.raw_close_min_exclusive"
        ),
        dollar_volume_window_sessions=_integer(
            node["dollar_volume_window_sessions"], "universe.dollar_volume_window_sessions"
        ),
        median_dollar_volume_min=_number(
            node["median_dollar_volume_min"], "universe.median_dollar_volume_min"
        ),
        core_market_cap_min=_number(node["core_market_cap_min"], "universe.core_market_cap_min"),
        core_market_cap_max=_number(node["core_market_cap_max"], "universe.core_market_cap_max"),
        large_cap_min_exclusive=_number(
            node["large_cap_min_exclusive"], "universe.large_cap_min_exclusive"
        ),
        excluded_groups=_tuple(node["excluded_groups"], _string, "universe.excluded_groups"),
    )


def _parse_factors(raw: Mapping[str, Any]) -> FactorsConfig:
    keys = {
        "weights",
        "quality_metrics",
        "value_metrics",
        "momentum_lookback_months",
        "momentum_skip_months",
        "piotroski_veto_max",
        "sector_relative",
    }
    node = _strict(raw, keys, "factors")
    weights = _strict(node["weights"], {"quality", "value", "momentum"}, "factors.weights")
    return FactorsConfig(
        weights=FactorWeights(
            quality=_number(weights["quality"], "factors.weights.quality"),
            value=_number(weights["value"], "factors.weights.value"),
            momentum=_number(weights["momentum"], "factors.weights.momentum"),
        ),
        quality_metrics=_tuple(node["quality_metrics"], _string, "factors.quality_metrics"),
        value_metrics=_tuple(node["value_metrics"], _string, "factors.value_metrics"),
        momentum_lookback_months=_integer(
            node["momentum_lookback_months"], "factors.momentum_lookback_months"
        ),
        momentum_skip_months=_integer(node["momentum_skip_months"], "factors.momentum_skip_months"),
        piotroski_veto_max=_integer(node["piotroski_veto_max"], "factors.piotroski_veto_max"),
        sector_relative=_boolean(node["sector_relative"], "factors.sector_relative"),
    )


def _parse_portfolio(raw: Mapping[str, Any]) -> PortfolioConfig:
    keys = {
        "holdings",
        "weighting",
        "large_cap_max_slots",
        "cash_return",
        "sector_cap_fraction",
        "correlation",
        "rotation",
        "bands",
        "turnover",
        "trailing_stop",
        "execution",
    }
    node = _strict(raw, keys, "portfolio")
    correlation = _strict(
        node["correlation"], {"enabled", "threshold", "window_sessions"}, "portfolio.correlation"
    )
    rotation = _strict(node["rotation"], {"weeks", "anchor_weekday"}, "portfolio.rotation")
    bands = _strict(node["bands"], {"entry_top_fraction", "hold_top_fraction"}, "portfolio.bands")
    turnover = _strict(
        node["turnover"], {"window_sessions", "one_way_fraction"}, "portfolio.turnover"
    )
    execution_keys = {
        "cost_bps",
        "cost_sensitivity_bps",
        "buy_collar_fraction",
        "routine_sell_collar_fraction",
        "thesis_sell_collar_fraction",
        "exit_retry_sessions",
        "funding_model",
    }
    execution = _strict(node["execution"], execution_keys, "portfolio.execution")
    return PortfolioConfig(
        holdings=_integer(node["holdings"], "portfolio.holdings"),
        weighting=_string(node["weighting"], "portfolio.weighting"),
        large_cap_max_slots=_integer(node["large_cap_max_slots"], "portfolio.large_cap_max_slots"),
        cash_return=_number(node["cash_return"], "portfolio.cash_return"),
        sector_cap_fraction=_number(node["sector_cap_fraction"], "portfolio.sector_cap_fraction"),
        correlation_enabled=_boolean(correlation["enabled"], "portfolio.correlation.enabled"),
        correlation_threshold=_number(correlation["threshold"], "portfolio.correlation.threshold"),
        correlation_window_sessions=_integer(
            correlation["window_sessions"], "portfolio.correlation.window_sessions"
        ),
        rotation_weeks=_integer(rotation["weeks"], "portfolio.rotation.weeks"),
        rotation_anchor_weekday=_string(
            rotation["anchor_weekday"], "portfolio.rotation.anchor_weekday"
        ),
        entry_top_fraction=_number(
            bands["entry_top_fraction"], "portfolio.bands.entry_top_fraction"
        ),
        hold_top_fraction=_number(bands["hold_top_fraction"], "portfolio.bands.hold_top_fraction"),
        turnover_window_sessions=_integer(
            turnover["window_sessions"], "portfolio.turnover.window_sessions"
        ),
        turnover_one_way_fraction=_number(
            turnover["one_way_fraction"], "portfolio.turnover.one_way_fraction"
        ),
        trailing_stop=_string(node["trailing_stop"], "portfolio.trailing_stop"),
        execution_cost_bps=_integer(execution["cost_bps"], "portfolio.execution.cost_bps"),
        cost_sensitivity_bps=_tuple(
            execution["cost_sensitivity_bps"], _integer, "portfolio.execution.cost_sensitivity_bps"
        ),
        buy_collar_fraction=_number(
            execution["buy_collar_fraction"], "portfolio.execution.buy_collar_fraction"
        ),
        routine_sell_collar_fraction=_number(
            execution["routine_sell_collar_fraction"],
            "portfolio.execution.routine_sell_collar_fraction",
        ),
        thesis_sell_collar_fraction=_number(
            execution["thesis_sell_collar_fraction"],
            "portfolio.execution.thesis_sell_collar_fraction",
        ),
        exit_retry_sessions=_integer(
            execution["exit_retry_sessions"], "portfolio.execution.exit_retry_sessions"
        ),
        funding_model=_string(execution["funding_model"], "portfolio.execution.funding_model"),
    )


def _parse_regime(raw: Mapping[str, Any]) -> RegimeConfig:
    node = _strict(raw, {"default_overlay", "overlays"}, "regime")
    raw_overlays = _mapping(node["overlays"], "regime.overlays")
    overlays: list[OverlayConfig] = []
    overlay_keys = {
        "signal",
        "evaluation",
        "risk_off_exposure",
        "hysteresis_fraction",
        "credit_confirm",
        "hy_oas_threshold_bps",
        "hy_oas_ma_sessions",
    }
    for overlay_id, payload in raw_overlays.items():
        context = f"regime.overlays.{overlay_id}"
        overlay = _strict(payload, overlay_keys, context)
        overlays.append(
            OverlayConfig(
                overlay_id=_string(overlay_id, f"{context}.id"),
                signal=_string(overlay["signal"], f"{context}.signal"),
                evaluation=_string(overlay["evaluation"], f"{context}.evaluation"),
                risk_off_exposure=_number(
                    overlay["risk_off_exposure"], f"{context}.risk_off_exposure"
                ),
                hysteresis_fraction=_number(
                    overlay["hysteresis_fraction"], f"{context}.hysteresis_fraction"
                ),
                credit_confirm=_boolean(overlay["credit_confirm"], f"{context}.credit_confirm"),
                hy_oas_threshold_bps=_integer(
                    overlay["hy_oas_threshold_bps"], f"{context}.hy_oas_threshold_bps"
                ),
                hy_oas_ma_sessions=_integer(
                    overlay["hy_oas_ma_sessions"], f"{context}.hy_oas_ma_sessions"
                ),
            )
        )
    return RegimeConfig(
        default_overlay=_string(node["default_overlay"], "regime.default_overlay"),
        overlays=tuple(overlays),
    )


def _parse_experiment_grid(raw: Mapping[str, Any]) -> ExperimentGridConfig:
    node = _strict(raw, {"axes", "limits"}, "experiment_grid")
    axis_keys = {
        "holdings",
        "large_cap_max_slots",
        "rotation_weeks",
        "bands",
        "sector_cap_fractions",
        "correlation_filters",
        "trailing_stops",
        "overlays",
        "sector_relative_ranks",
        "factor_weights",
    }
    axes = _strict(node["axes"], axis_keys, "experiment_grid.axes")
    limit_keys = {
        "full_cross_cells",
        "stage1_max",
        "stage2_max",
        "stage3_max",
        "controls_max",
        "planned_max",
    }
    limits = _strict(node["limits"], limit_keys, "experiment_grid.limits")
    return ExperimentGridConfig(
        holdings=_tuple(axes["holdings"], _integer, "experiment_grid.axes.holdings"),
        large_cap_max_slots=_tuple(
            axes["large_cap_max_slots"], _integer, "experiment_grid.axes.large_cap_max_slots"
        ),
        rotation_weeks=_tuple(
            axes["rotation_weeks"], _integer, "experiment_grid.axes.rotation_weeks"
        ),
        bands=_tuple(axes["bands"], _string, "experiment_grid.axes.bands"),
        sector_cap_fractions=_tuple(
            axes["sector_cap_fractions"], _number, "experiment_grid.axes.sector_cap_fractions"
        ),
        correlation_filters=_tuple(
            axes["correlation_filters"], _string, "experiment_grid.axes.correlation_filters"
        ),
        trailing_stops=_tuple(
            axes["trailing_stops"], _string, "experiment_grid.axes.trailing_stops"
        ),
        overlays=_tuple(axes["overlays"], _string, "experiment_grid.axes.overlays"),
        sector_relative_ranks=_tuple(
            axes["sector_relative_ranks"], _boolean, "experiment_grid.axes.sector_relative_ranks"
        ),
        factor_weights=_tuple(
            axes["factor_weights"], _string, "experiment_grid.axes.factor_weights"
        ),
        **{key: _integer(limits[key], f"experiment_grid.limits.{key}") for key in limit_keys},
    )


def _validate(config: AppConfig) -> None:
    settings = config.settings
    if settings.schema_version != 1:
        raise ConfigError("settings.schema_version must be 1")
    if (
        settings.execution_mode is ExecutionMode.LIVE
        and settings.evidence_mode is EvidenceMode.UNDECIDED
    ):
        raise ConfigError("live execution is forbidden while evidence mode is undecided")
    if settings.execution_mode is ExecutionMode.LIVE:
        raise ConfigError("live execution is unavailable until D024 activation gates exist")
    if not 0 < settings.edgar.max_requests_per_second <= 8:
        raise ConfigError("EDGAR request rate must be within (0, 8]")
    for name, timezone in asdict(settings.timezones).items():
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"unknown {name} timezone: {timezone}") from exc
    for name, value in asdict(settings.paths).items():
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ConfigError(f"{name} must be a repository-relative path")

    universe = config.universe
    if universe.include_otc:
        raise ConfigError("OTC cannot be enabled in v1")
    if not universe.domestic_only or not universe.common_stock_only:
        raise ConfigError("v1 universe must remain domestic common stock only")
    required_exclusions = {"financials", "reits", "pre_revenue_biotech", "fpi_adr"}
    if not required_exclusions <= set(universe.excluded_groups):
        raise ConfigError("v1 universe scope exclusions cannot be disabled")
    if not 0 < universe.core_market_cap_min < universe.core_market_cap_max:
        raise ConfigError("core market-cap bounds are invalid")
    if universe.large_cap_min_exclusive != universe.core_market_cap_max:
        raise ConfigError("large-cap boundary must equal the core maximum")

    weights = config.factors.weights
    if not math.isclose(weights.quality + weights.value + weights.momentum, 1.0, abs_tol=1e-9):
        raise ConfigError("factor weights must sum to 1")
    if config.factors.momentum_skip_months >= config.factors.momentum_lookback_months:
        raise ConfigError("momentum skip must be shorter than lookback")

    portfolio = config.portfolio
    grid = config.experiment_grid
    if portfolio.holdings not in grid.holdings:
        raise ConfigError("default holdings must be registered in experiment grid")
    if portfolio.large_cap_max_slots not in grid.large_cap_max_slots:
        raise ConfigError("default large-cap slots must be registered in experiment grid")
    if portfolio.rotation_weeks not in grid.rotation_weeks:
        raise ConfigError("default rotation must be registered in experiment grid")
    if portfolio.trailing_stop not in grid.trailing_stops:
        raise ConfigError("default stop must be registered in experiment grid")
    if not 0 < portfolio.entry_top_fraction <= portfolio.hold_top_fraction <= 1:
        raise ConfigError("entry/hold bands are invalid")
    if portfolio.funding_model != "settled_cash_only":
        raise ConfigError("v1 default funding must be settled_cash_only")

    overlay_ids = tuple(item.overlay_id for item in config.regime.overlays)
    if config.regime.default_overlay not in overlay_ids or overlay_ids != grid.overlays:
        raise ConfigError("regime overlays must exactly match the registered grid order")

    axes = (
        grid.holdings,
        grid.large_cap_max_slots,
        grid.rotation_weeks,
        grid.bands,
        grid.sector_cap_fractions,
        grid.correlation_filters,
        grid.trailing_stops,
        grid.overlays,
        grid.sector_relative_ranks,
        grid.factor_weights,
    )
    if any(not axis or len(axis) != len(set(axis)) for axis in axes):
        raise ConfigError("experiment axes must be non-empty and contain unique values")
    if math.prod(len(axis) for axis in axes) != grid.full_cross_cells:
        raise ConfigError("full-cross cell count does not match registered axes")
    if grid.stage1_max + grid.stage2_max + grid.stage3_max + grid.controls_max != grid.planned_max:
        raise ConfigError("planned experiment count does not match staged limits")

    freshness = config.freshness
    if not 0.0 < freshness.fundamentals_stale_fraction_max <= 1.0:
        raise ConfigError("fundamentals stale fraction must be in (0, 1]")
    if freshness.fundamentals_grace_business_days < 0:
        raise ConfigError("fundamentals grace business days cannot be negative")
    if freshness.price_max_sessions_stale <= 0:
        raise ConfigError("price max stale sessions must be positive")
    if freshness.macro_max_age_cadence_multiple <= 0:
        raise ConfigError("macro max-age cadence multiple must be positive")
    for form in ("10-Q", "10-K"):
        for filer in ("large_accelerated", "accelerated", "other"):
            if freshness.due_window_days(form, filer) <= 0:
                raise ConfigError("freshness due windows must be positive")


def _parse_freshness(raw: Mapping[str, Any]) -> FreshnessConfig:
    keys = {
        "fundamentals_stale_fraction_max",
        "fundamentals_grace_business_days",
        "form10q_due_days_large_accelerated",
        "form10q_due_days_accelerated",
        "form10q_due_days_other",
        "form10k_due_days_large_accelerated",
        "form10k_due_days_accelerated",
        "form10k_due_days_other",
        "price_max_sessions_stale",
        "macro_max_age_cadence_multiple",
    }
    node = _strict(raw, keys, "freshness")
    return FreshnessConfig(
        fundamentals_stale_fraction_max=_number(
            node["fundamentals_stale_fraction_max"], "freshness.fundamentals_stale_fraction_max"
        ),
        fundamentals_grace_business_days=_integer(
            node["fundamentals_grace_business_days"], "freshness.fundamentals_grace_business_days"
        ),
        form10q_due_days_large_accelerated=_integer(
            node["form10q_due_days_large_accelerated"],
            "freshness.form10q_due_days_large_accelerated",
        ),
        form10q_due_days_accelerated=_integer(
            node["form10q_due_days_accelerated"], "freshness.form10q_due_days_accelerated"
        ),
        form10q_due_days_other=_integer(
            node["form10q_due_days_other"], "freshness.form10q_due_days_other"
        ),
        form10k_due_days_large_accelerated=_integer(
            node["form10k_due_days_large_accelerated"],
            "freshness.form10k_due_days_large_accelerated",
        ),
        form10k_due_days_accelerated=_integer(
            node["form10k_due_days_accelerated"], "freshness.form10k_due_days_accelerated"
        ),
        form10k_due_days_other=_integer(
            node["form10k_due_days_other"], "freshness.form10k_due_days_other"
        ),
        price_max_sessions_stale=_integer(
            node["price_max_sessions_stale"], "freshness.price_max_sessions_stale"
        ),
        macro_max_age_cadence_multiple=_integer(
            node["macro_max_age_cadence_multiple"], "freshness.macro_max_age_cadence_multiple"
        ),
    )


def load_config(directory: str | Path | None = None) -> AppConfig:
    """Load all required YAML files, reject drift, and return immutable config."""
    base = Path(directory) if directory is not None else Path(__file__).resolve().parent
    config = AppConfig(
        settings=_parse_settings(_read_yaml(base, "settings.yaml")),
        universe=_parse_universe(_read_yaml(base, "universe.yaml")),
        factors=_parse_factors(_read_yaml(base, "factors.yaml")),
        portfolio=_parse_portfolio(_read_yaml(base, "portfolio.yaml")),
        regime=_parse_regime(_read_yaml(base, "regime.yaml")),
        experiment_grid=_parse_experiment_grid(_read_yaml(base, "experiment_grid.yaml")),
        freshness=_parse_freshness(_read_yaml(base, "freshness.yaml")),
    )
    _validate(config)
    return config
