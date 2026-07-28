"""Phase 4.2 point-in-time EOD exit tests."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from usinv.hygiene.screen import HygieneScreenResult
from usinv.hygiene.verdict import HygieneAction, HygieneVerdict
from usinv.portfolio import (
    ExitError,
    ExitKind,
    PositionState,
    SplitContinuousBar,
    apply_effective_split,
    evaluate_atr_trailing_stop,
    evaluate_percent_trailing_stop,
    evaluate_thesis_break,
    wilder_atr,
)

D = Decimal
ET = ZoneInfo("America/New_York")


def _bar(
    session: date,
    close: str,
    *,
    security_id: str = "SEC",
    high: str | None = None,
    low: str | None = None,
    quality: str = "reconstructed",
) -> SplitContinuousBar:
    close_value = D(close)
    return SplitContinuousBar(
        security_id,
        session,
        datetime(session.year, session.month, session.day, 16, 1, tzinfo=ET),
        close_value,
        D(high) if high else close_value + 1,
        D(low) if low else close_value - 1,
        close_value,
        quality,
        f"prices://{security_id}/{session.isoformat()}",
    )


def _position(*, high_water: str = "100", entry: date = date(2026, 6, 1)) -> PositionState:
    return PositionState("P1", "SEC", "ABC", D("10"), D("50"), D(high_water), entry)


def test_percent_stop_is_strict_eod_and_schedules_next_xnys_open():
    bar = _bar(date(2026, 6, 5), "79", high="82", low="78")
    decision = evaluate_percent_trailing_stop(
        _position(),
        bar,
        as_of=datetime(2026, 6, 5, 16, 2, tzinfo=ET),
        stop_fraction=D("0.20"),
    )
    assert decision.signal is not None
    assert decision.signal.kind is ExitKind.PERCENT_STOP
    assert decision.signal.signal_session == date(2026, 6, 5)
    assert decision.signal.execute_session == date(2026, 6, 8)
    assert decision.signal.threshold == D("80.00")
    assert decision.signal.collar_fraction == D("0.05")

    at_threshold = _bar(date(2026, 6, 8), "80", high="81", low="79")
    assert (
        evaluate_percent_trailing_stop(
            _position(),
            at_threshold,
            as_of=datetime(2026, 6, 8, 16, 2, tzinfo=ET),
            stop_fraction=D("0.20"),
        ).signal
        is None
    )


def test_unavailable_intraday_or_future_bar_is_rejected():
    bar = _bar(date(2026, 6, 5), "79")
    with pytest.raises(ExitError, match="future/unavailable"):
        evaluate_percent_trailing_stop(
            _position(),
            bar,
            as_of=datetime(2026, 6, 5, 15, 59, tzinfo=ET),
            stop_fraction=D("0.20"),
        )


def test_split_transforms_position_stop_basis_without_changing_decision():
    original = _position(high_water="100")
    before = evaluate_percent_trailing_stop(
        original,
        _bar(date(2026, 6, 5), "79"),
        as_of=datetime(2026, 6, 5, 16, 2, tzinfo=ET),
        stop_fraction=D("0.20"),
    )
    split = apply_effective_split(original, new_for_old=D("10"))
    after = evaluate_percent_trailing_stop(
        split,
        _bar(date(2026, 6, 8), "7.9", high="8.1", low="7.8"),
        as_of=datetime(2026, 6, 8, 16, 2, tzinfo=ET),
        stop_fraction=D("0.20"),
    )
    assert before.signal is not None
    assert after.signal is not None
    assert before.signal.threshold == D("80.0")
    assert after.signal.threshold == D("8.00")


def test_wilder_atr_uses_exact_initial_mean_and_recursive_update():
    sessions = (
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
        date(2026, 6, 4),
        date(2026, 6, 5),
    )
    bars = tuple(_bar(day, "10", high="12", low="8") for day in sessions)
    assert (
        wilder_atr(
            bars,
            period=3,
            as_of=datetime(2026, 6, 5, 16, 2, tzinfo=ET),
        )
        == D("4")
    )


def test_atr_stop_uses_close_high_water_and_only_visible_bars():
    sessions = (
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
        date(2026, 6, 4),
        date(2026, 6, 5),
        date(2026, 6, 8),
        date(2026, 6, 9),
        date(2026, 6, 10),
        date(2026, 6, 11),
        date(2026, 6, 12),
        date(2026, 6, 15),
        date(2026, 6, 16),
        date(2026, 6, 17),
        date(2026, 6, 18),
        date(2026, 6, 22),
    )
    closes = (*("100" for _ in range(14)), "90")
    bars = tuple(
        _bar(day, close, high=str(D(close) + 1), low=str(D(close) - 1))
        for day, close in zip(sessions, closes, strict=True)
    )
    decision = evaluate_atr_trailing_stop(
        _position(entry=sessions[0]),
        bars,
        period=14,
        multiple=D("3"),
        as_of=datetime(2026, 6, 22, 16, 2, tzinfo=ET),
    )
    assert decision.updated_high_water_mark == D("100")
    assert decision.signal is not None
    assert decision.signal.kind is ExitKind.ATR_STOP

    future = _bar(date(2026, 6, 23), "200", high="201", low="199")
    with pytest.raises(ExitError, match="future/unavailable"):
        wilder_atr(
            (*bars, future),
            period=14,
            as_of=datetime(2026, 6, 22, 16, 2, tzinfo=ET),
        )


def test_stop_fails_closed_on_unresolved_adjustment():
    with pytest.raises(ExitError, match="unresolved"):
        evaluate_percent_trailing_stop(
            _position(),
            _bar(date(2026, 6, 5), "79", quality="unresolved"),
            as_of=datetime(2026, 6, 5, 16, 2, tzinfo=ET),
            stop_fraction=D("0.20"),
        )


def test_thesis_break_requires_registered_excluding_gate_and_uses_ten_percent_collar():
    screen = HygieneScreenResult(
        "SEC",
        (
            HygieneVerdict(
                "atm_dilution",
                HygieneAction.EXCLUDE,
                "corroborated active issuance",
                "edgar://filing/1",
            ),
            HygieneVerdict(
                "shell",
                HygieneAction.PENALIZE,
                "shell marker",
                "edgar://filing/2",
            ),
        ),
    )
    signal = evaluate_thesis_break(
        screen,
        signal_session=date(2026, 6, 5),
        reference_close=D("7.50"),
        as_of=datetime(2026, 6, 5, 16, 2, tzinfo=ET),
    )
    assert signal is not None
    assert signal.kind is ExitKind.THESIS_BREAK
    assert signal.collar_fraction == D("0.10")
    assert signal.evidence_pointers == ("edgar://filing/1",)

    quarantine_only = HygieneScreenResult(
        "SEC",
        (
            HygieneVerdict(
                "data_integrity",
                HygieneAction.QUARANTINE,
                "missing data",
                "quality://missing",
            ),
        ),
    )
    assert (
        evaluate_thesis_break(
            quarantine_only,
            signal_session=date(2026, 6, 5),
            reference_close=D("7.50"),
            as_of=datetime(2026, 6, 5, 16, 2, tzinfo=ET),
        )
        is None
    )
