"""Phase 4.1 stateful selector, continuity, sizing and turnover tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from usinv.calendar import XNYSCalendar
from usinv.portfolio import (
    BandPolicy,
    Candidate,
    Holding,
    PositionState,
    ProposedTrade,
    TradeSide,
    TurnoverEvent,
    apply_effective_split,
    enforce_turnover_budget,
    retain_position,
    rotation_schedule,
    select_portfolio,
    size_equal_weight_entries,
)

D = Decimal


def _candidate(
    security_id: str,
    percentile: float,
    *,
    ticker: str | None = None,
    bucket: str = "core",
    sector: str = "business_equipment",
    composite: float | None = None,
    adv: str = "1000000",
    eligible: bool = True,
) -> Candidate:
    return Candidate(
        security_id=security_id,
        ticker=ticker or security_id,
        size_bucket=bucket,
        ff12_group=sector,
        bucket_percentile=percentile,
        composite=percentile if composite is None else composite,
        dollar_volume_21d=D(adv),
        eligible=eligible,
    )


def test_buy_hold_band_has_hysteresis():
    policy = BandPolicy(0.10, 0.25)
    assert policy.permits_entry(0.90)
    assert not policy.permits_entry(0.89)
    assert policy.permits_hold(0.75)
    assert not policy.permits_hold(0.74)
    assert not policy.permits_hold(None)


def test_selector_retains_hold_band_before_walking_new_entries():
    proposal = select_portfolio(
        (
            _candidate("HELD_KEEP", 0.76),
            _candidate("HELD_EXIT", 0.74),
            _candidate("NEW", 0.99),
        ),
        (Holding("P1", "HELD_KEEP"), Holding("P2", "HELD_EXIT")),
        holdings_target=2,
        large_cap_max_slots=0,
        sector_cap_fraction=1.0,
        band_policy=BandPolicy(0.10, 0.25),
    )
    assert proposal.retained_security_ids == ("HELD_KEEP",)
    assert proposal.band_exit_security_ids == ("HELD_EXIT",)
    assert proposal.selected_security_ids == ("HELD_KEEP", "NEW")


def test_forced_exit_is_processed_before_retention():
    proposal = select_portfolio(
        (_candidate("HELD", 0.99), _candidate("NEW", 0.98)),
        (Holding("P1", "HELD"),),
        holdings_target=1,
        large_cap_max_slots=0,
        sector_cap_fraction=1.0,
        band_policy=BandPolicy(0.10, 0.25),
        forced_exit_security_ids=frozenset({"HELD"}),
    )
    assert proposal.forced_exit_security_ids == ("HELD",)
    assert proposal.retained_security_ids == ()
    assert proposal.entry_security_ids == ("NEW",)


def test_selector_tie_break_is_composite_then_adv_then_ticker():
    candidates = (
        _candidate("A", 0.95, composite=0.8, adv="100"),
        _candidate("B", 0.95, composite=0.9, adv="50"),
        _candidate("C", 0.95, composite=0.9, adv="200", ticker="ZZZ"),
        _candidate("D", 0.95, composite=0.9, adv="200", ticker="AAA"),
    )
    proposal = select_portfolio(
        candidates,
        (),
        holdings_target=4,
        large_cap_max_slots=0,
        sector_cap_fraction=1.0,
        band_policy=BandPolicy(0.10, 0.25),
    )
    assert proposal.selected_security_ids == ("D", "C", "B", "A")


def test_sector_cap_uses_round_half_up_and_large_cap_is_ceiling_not_quota():
    candidates = (
        _candidate("TECH1", 0.99, sector="tech"),
        _candidate("TECH2", 0.98, sector="tech"),
        _candidate("TECH3", 0.97, sector="tech"),
        _candidate("OTHER", 0.96, sector="energy"),
        _candidate("LARGE", 0.95, bucket="large", sector="health"),
    )
    proposal = select_portfolio(
        candidates,
        (),
        holdings_target=5,
        large_cap_max_slots=0,
        sector_cap_fraction=0.50,
        band_policy=BandPolicy(0.10, 0.25),
    )
    assert proposal.sector_cap_count == 3
    assert "LARGE" not in proposal.selected_security_ids
    assert any(
        item.security_id == "LARGE" and item.reason == "large_cap_limit"
        for item in proposal.rejections
    )


def test_correlation_filter_rejects_high_and_missing_pairs_fail_closed():
    candidates = (
        _candidate("A", 0.99),
        _candidate("B", 0.98),
        _candidate("C", 0.97),
    )
    proposal = select_portfolio(
        candidates,
        (),
        holdings_target=3,
        large_cap_max_slots=0,
        sector_cap_fraction=1.0,
        band_policy=BandPolicy(0.10, 0.25),
        correlation_enabled=True,
        correlation_threshold=0.70,
        correlations={("A", "B"): 0.80},
    )
    assert proposal.selected_security_ids == ("A",)
    reasons = {item.security_id: item.reason for item in proposal.rejections}
    assert reasons == {"B": "correlation_cap", "C": "missing_correlation"}


def test_position_retention_does_not_reanchor_economics():
    position = PositionState("P1", "SEC1", "OLD", D("10"), D("20"), D("30"), date(2024, 1, 2))
    retained = retain_position(position, current_ticker="NEW")
    assert retained.position_id == position.position_id
    assert retained.entry_session == position.entry_session
    assert retained.cost_basis_per_share == D("20")
    assert retained.high_water_mark == D("30")
    assert retained.ticker == "NEW"


def test_effective_split_preserves_position_value_and_stop_basis():
    position = PositionState("P1", "SEC1", "ABC", D("10"), D("20"), D("30"), date(2024, 1, 2))
    split = apply_effective_split(position, new_for_old=D("5"))
    assert split.quantity == D("50")
    assert split.cost_basis_per_share == D("4")
    assert split.high_water_mark == D("6")
    assert (
        split.quantity * split.cost_basis_per_share
        == position.quantity * position.cost_basis_per_share
    )


def test_equal_weight_sizing_uses_settled_cash_and_leaves_partial_slot_cash():
    plan = size_equal_weight_entries(
        ("A", "B", "C"),
        pre_trade_nav=D("1500"),
        settled_cash=D("250"),
        holdings_target=15,
    )
    assert plan.target_per_position == D("100")
    assert [item.funded for item in plan.allocations] == [True, True, False]
    assert plan.settled_cash_remaining == D("50")


def test_turnover_formula_and_rolling_window():
    history = (
        TurnoverEvent(date(2026, 1, 2), TradeSide.BUY, D("100"), "OLD"),
        TurnoverEvent(date(2026, 2, 2), TradeSide.BUY, D("100"), "RECENT"),
    )
    decision = enforce_turnover_budget(
        history,
        (
            ProposedTrade("SELL", TradeSide.SELL, D("100")),
            ProposedTrade("BUY", TradeSide.BUY, D("100")),
        ),
        eligible_window_sessions=frozenset({date(2026, 2, 2)}),
        pre_trade_nav=D("1000"),
        maximum_fraction=D("0.20"),
    )
    # Existing 100 buy + proposed 100 sell + 100 buy => .5*300/1000=.15.
    assert len(decision.allowed) == 2
    assert decision.rolling_fraction == D("0.15")


def test_forced_exit_can_breach_turnover_but_blocks_replacement_buy():
    decision = enforce_turnover_budget(
        (),
        (
            ProposedTrade("FORCED", TradeSide.SELL, D("1200"), forced=True),
            ProposedTrade("REPLACE", TradeSide.BUY, D("100")),
        ),
        eligible_window_sessions=frozenset(),
        pre_trade_nav=D("1000"),
        maximum_fraction=D("0.50"),
    )
    assert [item.security_id for item in decision.allowed] == ["FORCED"]
    assert [item.security_id for item in decision.blocked] == ["REPLACE"]
    assert decision.forced_breach is True


def test_initial_formation_is_turnover_exempt():
    decision = enforce_turnover_budget(
        (),
        (ProposedTrade("A", TradeSide.BUY, D("1000")),),
        eligible_window_sessions=frozenset(),
        pre_trade_nav=D("1000"),
        maximum_fraction=D("0.10"),
        initial_formation=True,
    )
    assert len(decision.allowed) == 1
    assert decision.blocked == ()


def test_rotation_wrapper_uses_fixed_xnys_anchors():
    calendar = XNYSCalendar(start=date(2025, 1, 1), end=date(2025, 4, 30))
    rotations = rotation_schedule(
        date(2025, 1, 20),
        date(2025, 2, 17),
        weeks=4,
        calendar=calendar,
    )
    assert [item.anchor for item in rotations] == [date(2025, 1, 20), date(2025, 2, 17)]
    assert rotations[0].fill.label == date(2025, 1, 21)  # MLK holiday shift.
    assert rotations[1].fill.label == date(2025, 2, 18)  # Presidents' Day shift.
