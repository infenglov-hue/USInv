from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from usinv.calendar import default_calendar
from usinv.data.prices.actions import (
    ActionDetectionPoint,
    CorporateActionObservation,
    detect_split_candidates,
    reconcile_corporate_actions,
)
from usinv.data.prices.adjust import (
    RawClose,
    UnresolvedAdjustmentError,
    build_price_factors,
    raw_price_for_level_rule,
)


def _close(day: date) -> datetime:
    return default_calendar().session(day).close_at.astimezone(UTC)


def _observation(
    source: str,
    day: date,
    action_type: str,
    value: str,
    *,
    known_at: datetime | None = None,
    security_id: str = "security",
) -> CorporateActionObservation:
    return CorporateActionObservation(
        security_id,
        day,
        action_type,  # type: ignore[arg-type]
        Decimal(value),
        "USD" if action_type == "cash_dividend" else None,
        source,
        known_at or _close(day),
        f"{source}://{security_id}/{day}/{action_type}",
        f"batch-{source}",
    )


def _raw(day: date, close: str) -> RawClose:
    return RawClose("security", day, Decimal(close), f"raw://security/{day}")


def test_split_and_dividend_reconcile_and_build_distinct_as_of_factors() -> None:
    split_day = date(2024, 1, 4)
    dividend_day = date(2024, 1, 5)
    observations = (
        _observation("alpaca", split_day, "split", "2"),
        _observation("tiingo", split_day, "split", "2"),
        _observation("detector", split_day, "split", "2"),
        _observation("alpaca", dividend_day, "cash_dividend", "1"),
        _observation("tiingo", dividend_day, "cash_dividend", "1"),
    )
    report = reconcile_corporate_actions(observations, as_of=_close(dividend_day))

    assert [action.quality for action in report.actions] == [
        "reconstructed",
        "reconstructed",
    ]
    assert report.quarantined_security_ids == ()
    raw = (
        _raw(date(2024, 1, 2), "100"),
        _raw(date(2024, 1, 3), "100"),
        _raw(split_day, "50"),
        _raw(dividend_day, "49"),
    )
    factors = build_price_factors(
        raw,
        report.actions,
        anchor_session=dividend_day,
        as_of=_close(dividend_day),
        evidence_mode="research",
        consumer="total_return",
    )

    assert raw_price_for_level_rule(raw[0]) == Decimal("100")
    assert factors[0].split_factor == Decimal("0.5")
    assert factors[0].tr_factor == Decimal("0.490")
    assert factors[2].split_factor == Decimal("1")
    assert factors[2].tr_factor == Decimal("0.98")
    assert factors[-1].tr_factor == Decimal("1")


def test_future_reverse_split_cannot_rewrite_old_price_floor_or_stop_basis() -> None:
    old_session = date(2024, 12, 31)
    future_effective = date(2025, 1, 2)
    future = (
        _observation(
            "alpaca",
            future_effective,
            "split",
            "0.1",
            known_at=datetime(2024, 12, 20, 17, tzinfo=UTC),
        ),
        _observation(
            "tiingo",
            future_effective,
            "split",
            "0.1",
            known_at=datetime(2024, 12, 20, 17, tzinfo=UTC),
        ),
    )
    report = reconcile_corporate_actions(future, as_of=_close(old_session))
    old_raw = (_raw(old_session, "1.20"),)
    old_factor = build_price_factors(
        old_raw,
        report.actions,
        anchor_session=old_session,
        as_of=_close(old_session),
        evidence_mode="research",
        consumer="stop",
    )[0]

    assert raw_price_for_level_rule(old_raw[0]) == Decimal("1.20")
    assert old_factor.split_factor == Decimal("1")
    assert old_factor.split_continuous(Decimal("1.20")) == Decimal("1.20")

    later_raw = (
        *old_raw,
        _raw(future_effective, "12"),
        _raw(date(2025, 1, 3), "12.50"),
    )
    later_factor = build_price_factors(
        later_raw,
        report.actions,
        anchor_session=date(2025, 1, 3),
        as_of=_close(date(2025, 1, 3)),
        evidence_mode="research",
        consumer="stop",
    )[0]
    assert later_factor.split_factor == Decimal("1E+1")
    assert later_factor.split_continuous(Decimal("1.20")) == Decimal("12.0")


def test_reconciliation_never_reads_observation_newer_than_as_of() -> None:
    effective = date(2024, 1, 4)
    observations = (
        _observation("alpaca", effective, "split", "2"),
        _observation(
            "tiingo",
            effective,
            "split",
            "2",
            known_at=_close(date(2024, 1, 5)),
        ),
    )

    before = reconcile_corporate_actions(observations, as_of=_close(effective))
    after = reconcile_corporate_actions(observations, as_of=_close(date(2024, 1, 5)))

    assert before.actions[0].quality == "unresolved"
    assert before.actions[0].sources == ("alpaca",)
    assert after.actions[0].quality == "reconstructed"


def test_unresolved_adjustment_is_rejected_by_stops_and_audit_mode() -> None:
    effective = date(2024, 1, 4)
    report = reconcile_corporate_actions(
        (_observation("alpaca", effective, "split", "2"),),
        as_of=_close(effective),
    )
    raw = (_raw(date(2024, 1, 3), "100"), _raw(effective, "50"))

    with pytest.raises(UnresolvedAdjustmentError, match="stop calculation"):
        build_price_factors(
            raw,
            report.actions,
            anchor_session=effective,
            as_of=_close(effective),
            evidence_mode="research",
            consumer="stop",
        )
    with pytest.raises(UnresolvedAdjustmentError, match="audit mode"):
        build_price_factors(
            raw,
            report.actions,
            anchor_session=effective,
            as_of=_close(effective),
            evidence_mode="audit",
            consumer="total_return",
        )


def test_real_nikola_reverse_split_fixture_reconciles_all_three_signals() -> None:
    # SEC 8-K: https://www.sec.gov/Archives/edgar/data/1731289/000173128924000195/
    # NKLA began split-adjusted trading 2024-06-25 after a 1-for-30 reverse split.
    before = ActionDetectionPoint(
        "nikola-common",
        date(2024, 6, 24),
        Decimal("0.32"),
        Decimal("9.60"),
        1_000_000,
        "fixture://nkla/raw/2024-06-24",
        "fixture://nkla/adjusted/2024-06-24",
        "fixture-nkla",
    )
    after = ActionDetectionPoint(
        "nikola-common",
        date(2024, 6, 25),
        Decimal("9.60"),
        Decimal("9.70"),
        2_000_000,
        "fixture://nkla/raw/2024-06-25",
        "fixture://nkla/adjusted/2024-06-25",
        "fixture-nkla",
    )
    detected = detect_split_candidates((before, after))
    assert len(detected) == 1
    assert detected[0].ratio_or_cash == Decimal("0.0333333333333333333333333333")

    event_day = date(2024, 6, 25)
    declared = (
        _observation(
            "alpaca",
            event_day,
            "split",
            "0.0333333333333333333333333333",
            security_id="nikola-common",
        ),
        _observation(
            "tiingo",
            event_day,
            "split",
            "0.0333333333333333333333333333",
            security_id="nikola-common",
        ),
    )
    report = reconcile_corporate_actions((*declared, *detected), as_of=_close(event_day))

    assert report.actions[0].quality == "reconstructed"
    assert report.actions[0].sources == ("alpaca", "detector", "tiingo")
    assert report.quarantined_security_ids == ()


def test_earnings_8k_session_blocks_price_jump_detector() -> None:
    before = ActionDetectionPoint(
        "security",
        date(2024, 1, 3),
        Decimal("10"),
        Decimal("10"),
        100,
        "raw://before",
        "adjusted://before",
        "batch",
    )
    after = ActionDetectionPoint(
        "security",
        date(2024, 1, 4),
        Decimal("5"),
        Decimal("10.1"),
        200,
        "raw://after",
        "adjusted://after",
        "batch",
    )
    other_before = ActionDetectionPoint(
        "other-security",
        before.session,
        before.raw_close,
        before.adjusted_close,
        before.volume,
        "raw://other/before",
        "adjusted://other/before",
        "batch",
    )
    other_after = ActionDetectionPoint(
        "other-security",
        after.session,
        after.raw_close,
        after.adjusted_close,
        after.volume,
        "raw://other/after",
        "adjusted://other/after",
        "batch",
    )

    detected = detect_split_candidates(
        (before, after, other_before, other_after),
        earnings_8k_events=frozenset({("security", after.session)}),
    )

    assert len(detected) == 1
    assert detected[0].security_id == "other-security"
