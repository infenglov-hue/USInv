"""Phase 3.2 point-in-time scoring context tests (MODEL_SPEC §7, AGENTS.md rule 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from usinv.scoring import (
    LookAheadError,
    ScoringContextError,
    ScoringInputs,
    Vintage,
)

AS_OF = datetime(2026, 2, 17, 21, 0, tzinfo=UTC)


def _instant(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 21, 0, tzinfo=UTC)


def _inputs() -> ScoringInputs:
    return ScoringInputs(
        fundamentals={
            "SEC-1": (
                Vintage(_instant(2025, 11, 14), {"ttm_revenue": 100}),
                Vintage(_instant(2026, 2, 12), {"ttm_revenue": 120}),
                Vintage(_instant(2026, 5, 1), {"ttm_revenue": 999}),  # future, unseen
            )
        },
        prices={
            "SEC-1": (
                Vintage(_instant(2026, 2, 13), Decimal("10.00")),
                Vintage(_instant(2026, 2, 17), Decimal("10.50")),
            )
        },
        macro={"NFCI": (Vintage(_instant(2026, 2, 11), -0.3),)},
        universe=(
            Vintage(_instant(2026, 1, 1), "SEC-1"),
            Vintage(_instant(2026, 3, 1), "SEC-2"),  # becomes a member later
        ),
    )


def _shift(inputs: ScoringInputs, days: int) -> ScoringInputs:
    def shift_seq(seq):
        return tuple(Vintage(v.available_from + timedelta(days=days), v.value) for v in seq)

    return ScoringInputs(
        fundamentals={k: shift_seq(v) for k, v in inputs.fundamentals.items()},
        prices={k: shift_seq(v) for k, v in inputs.prices.items()},
        macro={k: shift_seq(v) for k, v in inputs.macro.items()},
        universe=shift_seq(inputs.universe),
    )


# --------------------------------------------------------------------------- #
# Availability filtering.
# --------------------------------------------------------------------------- #
def test_latest_visible_fundamentals():
    ctx = _inputs().as_of(AS_OF)
    assert ctx.fundamentals_ttm("SEC-1") == {"ttm_revenue": 120}


def test_future_fact_is_invisible():
    # The 2026-05-01 vintage (999) is after the signal and must never be read.
    ctx = _inputs().as_of(AS_OF)
    assert ctx.fundamentals_ttm("SEC-1") != {"ttm_revenue": 999}


def test_price_returns_latest_close_at_or_before_signal():
    ctx = _inputs().as_of(AS_OF)
    assert ctx.price("SEC-1") == Decimal("10.50")


def test_missing_key_returns_none():
    ctx = _inputs().as_of(AS_OF)
    assert ctx.fundamentals_ttm("SEC-UNKNOWN") is None
    assert ctx.macro("MISSING") is None


# --------------------------------------------------------------------------- #
# Leak test (the Phase 3.2 acceptance gate).
# --------------------------------------------------------------------------- #
def test_shifting_availability_changes_the_visible_value():
    base = _inputs().as_of(AS_OF)
    shifted = _shift(_inputs(), 7).as_of(AS_OF)
    # +7d pushes the 2026-02-12 filing past the signal, so the older one shows.
    assert base.fundamentals_ttm("SEC-1") == {"ttm_revenue": 120}
    assert shifted.fundamentals_ttm("SEC-1") == {"ttm_revenue": 100}


def test_require_available_raises_on_future():
    ctx = _inputs().as_of(AS_OF)
    ctx.require_available(_instant(2026, 2, 10), "fundamentals")  # past: ok
    with pytest.raises(LookAheadError):
        ctx.require_available(_instant(2026, 5, 1), "fundamentals")


# --------------------------------------------------------------------------- #
# Universe membership is point-in-time.
# --------------------------------------------------------------------------- #
def test_universe_membership_is_point_in_time():
    ctx = _inputs().as_of(AS_OF)
    assert ctx.universe_members() == ("SEC-1",)
    assert ctx.is_member("SEC-1") is True
    assert ctx.is_member("SEC-2") is False  # joins 2026-03-01, after the signal


# --------------------------------------------------------------------------- #
# Timezone discipline.
# --------------------------------------------------------------------------- #
def test_naive_as_of_is_rejected():
    with pytest.raises(ScoringContextError):
        _inputs().as_of(datetime(2026, 2, 17, 16, 0))


def test_naive_vintage_is_rejected():
    with pytest.raises(ScoringContextError):
        Vintage(datetime(2026, 2, 17, 16, 0), {"x": 1})
