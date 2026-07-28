"""Pre-registered staged search, durable deduplication and one-shot TEST gate."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from usinv.backtest.splits import TEST_END, TEST_START, LockedSplits
from usinv.config.loader import ExperimentGridConfig


class ExperimentProtocolError(ValueError):
    """Raised when a run would violate the pre-registered experiment protocol."""


class TestUnlockError(ExperimentProtocolError):
    """Raised when the one-shot static holdout is not authorized."""


AXIS_FIELDS = (
    "holdings",
    "large_cap_max_slots",
    "rotation_weeks",
    "band",
    "sector_cap_fraction",
    "correlation_filter",
    "trailing_stop",
    "overlay",
    "sector_relative_ranks",
    "factor_weights",
)
GRID_ATTRS = {
    "holdings": "holdings",
    "large_cap_max_slots": "large_cap_max_slots",
    "rotation_weeks": "rotation_weeks",
    "band": "bands",
    "sector_cap_fraction": "sector_cap_fractions",
    "correlation_filter": "correlation_filters",
    "trailing_stop": "trailing_stops",
    "overlay": "overlays",
    "sector_relative_ranks": "sector_relative_ranks",
    "factor_weights": "factor_weights",
}
ORDINAL_FIELDS = (
    "holdings",
    "rotation_weeks",
    "band",
    "sector_cap_fraction",
    "trailing_stop",
)
CATEGORICAL_FIELDS = (
    "large_cap_max_slots",
    "correlation_filter",
    "overlay",
    "sector_relative_ranks",
    "factor_weights",
)
HIGH_IMPACT_FIELDS = ("holdings", "rotation_weeks", "band", "trailing_stop", "overlay")


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _is_hash(value: str, *, minimum: int = 7) -> bool:
    return len(value) >= minimum and all(character in "0123456789abcdef" for character in value)


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    holdings: int
    large_cap_max_slots: int
    rotation_weeks: int
    band: str
    sector_cap_fraction: float
    correlation_filter: str
    trailing_stop: str
    overlay: str
    sector_relative_ranks: bool
    factor_weights: str

    @property
    def config_hash(self) -> str:
        return _sha256(asdict(self))


@dataclass(frozen=True, slots=True)
class SearchMetrics:
    net_sharpe: float
    annualized_net_alpha: float
    ulcer_index: float
    max_drawdown: float
    one_way_turnover: float
    steady_returns_pass: bool

    def __post_init__(self) -> None:
        values = (
            self.net_sharpe,
            self.annualized_net_alpha,
            self.ulcer_index,
            self.max_drawdown,
            self.one_way_turnover,
        )
        if any(not math.isfinite(value) for value in values):
            raise ExperimentProtocolError("search metrics must be finite")
        if self.ulcer_index < 0 or self.one_way_turnover < 0:
            raise ExperimentProtocolError("Ulcer and turnover must be non-negative")
        if self.max_drawdown > 0:
            raise ExperimentProtocolError("MaxDD must be represented as zero or a negative return")


@dataclass(frozen=True, slots=True)
class Stage1Selection:
    surviving_values: Mapping[str, tuple[object, ...]]
    winners: ExperimentConfig
    protocol_notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExperimentGrid:
    axes: Mapping[str, tuple[object, ...]]
    full_cross_cells: int
    stage1_max: int
    stage2_max: int
    stage3_max: int
    controls_max: int
    planned_max: int

    @classmethod
    def from_config(cls, config: ExperimentGridConfig) -> ExperimentGrid:
        axes = {field: tuple(getattr(config, GRID_ATTRS[field])) for field in AXIS_FIELDS}
        grid = cls(
            axes,
            config.full_cross_cells,
            config.stage1_max,
            config.stage2_max,
            config.stage3_max,
            config.controls_max,
            config.planned_max,
        )
        grid.validate()
        return grid

    @property
    def default(self) -> ExperimentConfig:
        return ExperimentConfig(
            holdings=15,
            large_cap_max_slots=0,
            rotation_weeks=4,
            band="quartile",
            sector_cap_fraction=0.27,
            correlation_filter="0.7",
            trailing_stop="percent_20",
            overlay="O0",
            sector_relative_ranks=False,
            factor_weights="equal",
        )

    def validate(self) -> None:
        if tuple(self.axes) != AXIS_FIELDS:
            raise ExperimentProtocolError("experiment axes or their order differ from the protocol")
        if any(not values or len(values) != len(set(values)) for values in self.axes.values()):
            raise ExperimentProtocolError("experiment axes must be non-empty and unique")
        actual_cross = math.prod(len(values) for values in self.axes.values())
        if actual_cross != self.full_cross_cells or actual_cross != 331_776:
            raise ExperimentProtocolError("full cross must be exactly 331,776 cells")
        if (
            self.stage1_max,
            self.stage2_max,
            self.stage3_max,
            self.controls_max,
            self.planned_max,
        ) != (29, 96, 60, 5, 190):
            raise ExperimentProtocolError("staged run caps differ from the pre-registration")
        for field in AXIS_FIELDS:
            if getattr(self.default, field) not in self.axes[field]:
                raise ExperimentProtocolError(f"default is absent from axis {field}")

    def stage1_cells(self) -> tuple[ExperimentConfig, ...]:
        """Return default plus every one-axis deviation: exactly 29 cells."""
        cells = [self.default]
        for field in AXIS_FIELDS:
            for value in self.axes[field]:
                if value != getattr(self.default, field):
                    cells.append(replace(self.default, **{field: value}))
        unique = _deduplicate(cells)
        if len(unique) != self.stage1_max:
            raise ExperimentProtocolError("Stage 1 must contain exactly 29 unique cells")
        return unique

    def select_stage1(
        self,
        results: Mapping[str, SearchMetrics],
    ) -> Stage1Selection:
        """Select Stage-1 survivors without inspecting any unregistered cell."""
        stage1 = self.stage1_cells()
        missing = [cell.config_hash for cell in stage1 if cell.config_hash not in results]
        if missing:
            raise ExperimentProtocolError("Stage 1 selection requires all 29 registered results")

        survivors: dict[str, tuple[object, ...]] = {}
        winner_values: dict[str, object] = {}
        notes: list[str] = []
        for field in AXIS_FIELDS:
            default_value = getattr(self.default, field)
            axis_cells = [
                cell
                for cell in stage1
                if all(
                    getattr(cell, other) == getattr(self.default, other)
                    for other in AXIS_FIELDS
                    if other != field
                )
            ]
            eligible = [
                cell for cell in axis_cells if results[cell.config_hash].steady_returns_pass
            ]
            winner = max(
                eligible or [self.default],
                key=lambda cell: (
                    results[cell.config_hash].net_sharpe,
                    cell.config_hash,
                ),
            )
            winner_values[field] = getattr(winner, field)

            if field == "factor_weights":
                fixed_tilts = [
                    cell
                    for cell in axis_cells
                    if cell.factor_weights in {"quality_tilt", "value_tilt"}
                    and results[cell.config_hash].steady_returns_pass
                ]
                values: list[object] = ["equal"]
                if fixed_tilts:
                    best_fixed = max(
                        fixed_tilts,
                        key=lambda cell: (
                            results[cell.config_hash].net_sharpe,
                            cell.config_hash,
                        ),
                    )
                    values.append(best_fixed.factor_weights)
                values.append("attribution_derived")
                survivors[field] = tuple(dict.fromkeys(values))
                continue

            challengers = [cell for cell in eligible if getattr(cell, field) != default_value]
            best_challenger = (
                max(
                    challengers,
                    key=lambda cell: (
                        results[cell.config_hash].net_sharpe,
                        cell.config_hash,
                    ),
                )
                if challengers
                else None
            )
            values = [default_value]
            if best_challenger is not None:
                values.append(getattr(best_challenger, field))

            # EXPERIMENT_PLAN mandates four O0/O1 x none/20 interaction cells
            # inside the <=96 Stage-2 factorial. Keeping those registered cells
            # takes precedence over a different generic "best challenger".
            if field == "trailing_stop":
                values = [default_value, "none"]
                notes.append("required interaction override: trailing_stop challenger=none")
            elif field == "overlay":
                values = [default_value, "O1"]
                notes.append("required interaction override: overlay challenger=O1")
            survivors[field] = tuple(dict.fromkeys(values))

        winners = replace(self.default, **winner_values)
        return Stage1Selection(survivors, winners, tuple(notes))

    def stage2_cells(self, selection: Stage1Selection) -> tuple[ExperimentConfig, ...]:
        """Cross the five high-impact survivors and factor weights, capped at 96."""
        for field in (*HIGH_IMPACT_FIELDS, "factor_weights"):
            if not selection.surviving_values.get(field):
                raise ExperimentProtocolError(f"Stage 2 has no survivors for {field}")
        fixed = selection.winners
        fields = (*HIGH_IMPACT_FIELDS, "factor_weights")
        values = [selection.surviving_values[field] for field in fields]
        cells = [
            replace(fixed, **dict(zip(fields, combination, strict=True)))
            for combination in itertools.product(*values)
        ]
        unique = _deduplicate(cells)
        if len(unique) > self.stage2_max:
            raise ExperimentProtocolError("Stage 2 exceeds 96 unique cells")
        required = {
            ("O1", "none"),
            ("O0", "percent_20"),
            ("O1", "percent_20"),
            ("O0", "none"),
        }
        present = {(cell.overlay, cell.trailing_stop) for cell in unique}
        if not required <= present:
            raise ExperimentProtocolError("Stage 2 is missing a mandated overlay/stop interaction")
        return unique

    def finalists(
        self,
        stage2_cells: tuple[ExperimentConfig, ...],
        results: Mapping[str, SearchMetrics],
    ) -> tuple[ExperimentConfig, ...]:
        eligible = [
            cell
            for cell in stage2_cells
            if cell.config_hash in results and results[cell.config_hash].steady_returns_pass
        ]
        return tuple(
            sorted(
                eligible,
                key=lambda cell: (
                    -results[cell.config_hash].net_sharpe,
                    results[cell.config_hash].one_way_turnover,
                    cell.config_hash,
                ),
            )[:3]
        )

    def stage3_cells(
        self,
        finalists: tuple[ExperimentConfig, ...],
        *,
        completed_config_hashes: frozenset[str] = frozenset(),
    ) -> tuple[ExperimentConfig, ...]:
        """Return every missing original-grid neighbor for at most three finalists."""
        if len(finalists) > 3:
            raise ExperimentProtocolError("Stage 3 accepts at most three finalists")
        cells: list[ExperimentConfig] = []
        for finalist in finalists:
            for field in ORDINAL_FIELDS:
                axis = self.axes[field]
                index = axis.index(getattr(finalist, field))
                for neighbor_index in (index - 1, index + 1):
                    if 0 <= neighbor_index < len(axis):
                        cells.append(replace(finalist, **{field: axis[neighbor_index]}))
            for field in CATEGORICAL_FIELDS:
                for value in self.axes[field]:
                    if value != getattr(finalist, field):
                        cells.append(replace(finalist, **{field: value}))
        unique = tuple(
            cell for cell in _deduplicate(cells) if cell.config_hash not in completed_config_hashes
        )
        if len(unique) > self.stage3_max:
            raise ExperimentProtocolError("Stage 3 exceeds 60 new unique cells")
        return unique

    def control_cells(self, chosen: ExperimentConfig) -> tuple[ExperimentConfig, ...]:
        """Return C0 plus four exact single-layer ablations, deduplicated."""
        cells = (
            self.default,
            replace(chosen, overlay="O0"),
            replace(chosen, trailing_stop="none"),
            replace(chosen, band="none"),
            replace(chosen, factor_weights="equal"),
        )
        unique = _deduplicate(cells)
        if len(unique) > self.controls_max:
            raise ExperimentProtocolError("control set exceeds five cells")
        return unique


def _deduplicate(cells: Iterable[ExperimentConfig]) -> tuple[ExperimentConfig, ...]:
    by_hash: dict[str, ExperimentConfig] = {}
    for cell in cells:
        by_hash.setdefault(cell.config_hash, cell)
    return tuple(by_hash.values())


@dataclass(frozen=True, slots=True)
class FrozenDataManifest:
    source_batch_hashes: Mapping[str, str]
    evidence_mode: str
    unresolved_action_count: int
    unmapped_security_count: int
    first_session: date
    last_session: date

    def __post_init__(self) -> None:
        if self.evidence_mode not in {"research", "audit"}:
            raise ExperimentProtocolError("manifest evidence mode must be research or audit")
        if not self.source_batch_hashes or any(
            not key or not _is_hash(value, minimum=64)
            for key, value in self.source_batch_hashes.items()
        ):
            raise ExperimentProtocolError("manifest source/batch hashes are incomplete")
        if self.unresolved_action_count < 0 or self.unmapped_security_count < 0:
            raise ExperimentProtocolError("manifest quality counts cannot be negative")
        if self.first_session >= self.last_session:
            raise ExperimentProtocolError("manifest session range is invalid")

    @property
    def manifest_hash(self) -> str:
        return _sha256(
            {
                **asdict(self),
                "first_session": self.first_session.isoformat(),
                "last_session": self.last_session.isoformat(),
            }
        )


@dataclass(frozen=True, slots=True)
class ExperimentRunRecord:
    record_hash: str
    previous_record_hash: str
    attempted_at: str
    stage: str
    split: str
    config: Mapping[str, object]
    config_hash: str
    data_manifest_hash: str
    code_sha: str
    status: str
    metrics: Mapping[str, object] | None
    output_hash: str | None
    error: str | None


class ExperimentLedger:
    """Append-only hash-chained JSONL attempt ledger."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def records(self) -> tuple[ExperimentRunRecord, ...]:
        if not self.path.exists():
            return ()
        output: list[ExperimentRunRecord] = []
        previous = ""
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            try:
                payload = json.loads(line)
                record_hash = str(payload.pop("record_hash"))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ExperimentProtocolError(
                    f"experiment ledger line {line_number} is invalid"
                ) from exc
            if payload.get("previous_record_hash") != previous:
                raise ExperimentProtocolError("experiment ledger hash chain is broken")
            if _sha256(payload) != record_hash:
                raise ExperimentProtocolError("experiment ledger record hash is invalid")
            full = {"record_hash": record_hash, **payload}
            try:
                output.append(ExperimentRunRecord(**full))
            except TypeError as exc:
                raise ExperimentProtocolError("experiment ledger schema is invalid") from exc
            previous = record_hash
        return tuple(output)

    def append(self, payload: Mapping[str, object]) -> ExperimentRunRecord:
        records = self.records()
        body = {
            "previous_record_hash": records[-1].record_hash if records else "",
            **payload,
        }
        record_hash = _sha256(body)
        encoded = json.dumps(
            {"record_hash": record_hash, **body},
            sort_keys=True,
            separators=(",", ":"),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded + "\n")
            handle.flush()
        return self.records()[-1]

    def completed_keys(self) -> frozenset[tuple[str, str, str, str]]:
        return frozenset(
            (
                record.split,
                record.config_hash,
                record.data_manifest_hash,
                record.code_sha,
            )
            for record in self.records()
            if record.status == "complete"
        )

    def train_validation_seal(
        self,
        *,
        data_manifest_hash: str | None = None,
        code_sha: str | None = None,
    ) -> str:
        records = tuple(
            record
            for record in self.records()
            if record.status == "complete"
            and record.split in {"train", "validation"}
            and (data_manifest_hash is None or record.data_manifest_hash == data_manifest_hash)
            and (code_sha is None or record.code_sha == code_sha)
        )
        if not {record.split for record in records} >= {"train", "validation"}:
            raise ExperimentProtocolError("TRAIN and VALIDATION outputs are not both complete")
        return _sha256([record.record_hash for record in records])


Evaluator = Callable[[ExperimentConfig], SearchMetrics]


class ExperimentRunner:
    """Runs only missing cells and persists each attempt before moving on."""

    def __init__(
        self,
        *,
        ledger: ExperimentLedger,
        data_manifest_hash: str,
        code_sha: str,
    ) -> None:
        if not _is_hash(data_manifest_hash, minimum=64):
            raise ExperimentProtocolError("data manifest hash must be a SHA-256")
        if not _is_hash(code_sha):
            raise ExperimentProtocolError("code SHA is invalid")
        self.ledger = ledger
        self.data_manifest_hash = data_manifest_hash
        self.code_sha = code_sha

    def run_cells(
        self,
        *,
        stage: str,
        split: str,
        cells: tuple[ExperimentConfig, ...],
        evaluator: Evaluator,
    ) -> tuple[ExperimentRunRecord, ...]:
        if split not in {"train", "validation", "test"}:
            raise ExperimentProtocolError("unknown experiment split")
        unique = _deduplicate(cells)
        completed = self.ledger.completed_keys()
        produced: list[ExperimentRunRecord] = []
        for cell in unique:
            key = (split, cell.config_hash, self.data_manifest_hash, self.code_sha)
            if key in completed:
                continue
            attempted_at = datetime.now(UTC).isoformat()
            common: dict[str, object] = {
                "attempted_at": attempted_at,
                "stage": stage,
                "split": split,
                "config": asdict(cell),
                "config_hash": cell.config_hash,
                "data_manifest_hash": self.data_manifest_hash,
                "code_sha": self.code_sha,
            }
            try:
                metrics = evaluator(cell)
            except Exception as exc:
                self.ledger.append(
                    {
                        **common,
                        "status": "failed",
                        "metrics": None,
                        "output_hash": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                raise
            metric_payload = asdict(metrics)
            produced.append(
                self.ledger.append(
                    {
                        **common,
                        "status": "complete",
                        "metrics": metric_payload,
                        "output_hash": _sha256(metric_payload),
                        "error": None,
                    }
                )
            )
            completed = completed | {key}
        return tuple(produced)


class TestUnlockRegistry:
    """Persistent final-config registration and one-time TEST burn flag."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def register(
        self,
        *,
        final_config_hash: str,
        train_validation_seal: str,
        data_manifest_hash: str,
        code_sha: str,
        unlock_token: str,
    ) -> None:
        if self.path.exists():
            raise TestUnlockError("final TEST configuration is already registered")
        if not _is_hash(final_config_hash, minimum=64) or not _is_hash(
            train_validation_seal,
            minimum=64,
        ):
            raise TestUnlockError("final config or output seal hash is invalid")
        if not _is_hash(data_manifest_hash, minimum=64) or not _is_hash(code_sha):
            raise TestUnlockError("registered data manifest hash or code SHA is invalid")
        if not unlock_token:
            raise TestUnlockError("a user-provided TEST unlock token is required")
        payload = {
            "final_config_hash": final_config_hash,
            "train_validation_seal": train_validation_seal,
            "data_manifest_hash": data_manifest_hash,
            "code_sha": code_sha,
            "unlock_token_sha256": hashlib.sha256(unlock_token.encode()).hexdigest(),
            "registered_at": datetime.now(UTC).isoformat(),
            "test_consumed": False,
            "consumed_at": None,
        }
        self._write(payload)

    def authorize_and_consume(
        self,
        *,
        config_hash: str,
        train_validation_seal: str,
        data_manifest_hash: str,
        code_sha: str,
        unlock_token: str,
    ) -> None:
        if not self.path.exists():
            raise TestUnlockError("final TEST configuration is not registered")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TestUnlockError("TEST registry is invalid") from exc
        if payload.get("test_consumed"):
            raise TestUnlockError("TEST has already been consumed")
        if payload.get("final_config_hash") != config_hash:
            raise TestUnlockError("TEST config does not match the registered final hash")
        if payload.get("train_validation_seal") != train_validation_seal:
            raise TestUnlockError("TRAIN/VALIDATION outputs changed after sealing")
        if payload.get("data_manifest_hash") != data_manifest_hash:
            raise TestUnlockError("TEST data manifest differs from the registered hash")
        if payload.get("code_sha") != code_sha:
            raise TestUnlockError("TEST code SHA differs from the registered SHA")
        token_hash = hashlib.sha256(unlock_token.encode()).hexdigest()
        if payload.get("unlock_token_sha256") != token_hash:
            raise TestUnlockError("TEST unlock token is invalid")
        payload["test_consumed"] = True
        payload["consumed_at"] = datetime.now(UTC).isoformat()
        self._write(payload)

    def _write(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def run_locked_test(
    *,
    splits: LockedSplits,
    config: ExperimentConfig,
    evaluator: Evaluator,
    runner: ExperimentRunner,
    registry: TestUnlockRegistry,
    unlock_token: str,
) -> ExperimentRunRecord:
    """Burn the exact static TEST once, before invoking the evaluator."""
    if not splits.test or splits.test[0] != TEST_START or splits.test[-1] != TEST_END:
        raise TestUnlockError("TEST dates differ from the locked 2023-01-03..2026-06-30 window")
    seal = runner.ledger.train_validation_seal(
        data_manifest_hash=runner.data_manifest_hash,
        code_sha=runner.code_sha,
    )
    registered_validation = any(
        record.status == "complete"
        and record.split == "validation"
        and record.config_hash == config.config_hash
        and record.data_manifest_hash == runner.data_manifest_hash
        and record.code_sha == runner.code_sha
        for record in runner.ledger.records()
    )
    if not registered_validation:
        raise TestUnlockError(
            "final config has no sealed VALIDATION result for the registered data/code"
        )
    registry.authorize_and_consume(
        config_hash=config.config_hash,
        train_validation_seal=seal,
        data_manifest_hash=runner.data_manifest_hash,
        code_sha=runner.code_sha,
        unlock_token=unlock_token,
    )
    records = runner.run_cells(
        stage="final_test",
        split="test",
        cells=(config,),
        evaluator=evaluator,
    )
    if len(records) != 1:
        raise TestUnlockError("TEST did not produce exactly one new result")
    return records[0]
