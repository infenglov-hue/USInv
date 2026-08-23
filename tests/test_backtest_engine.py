"""Phase 4.3 execution, accounting, termination and metrics gates."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from usinv.backtest import (
    BacktestEngine,
    BacktestError,
    BacktestMetrics,
    DividendEvent,
    FixedBpsCostModel,
    ManualExecutionRequired,
    MarketBar,
    SplitEvent,
    TerminationEvent,
    TerminationEvidenceError,
    TerminationKind,
    assess_steady_returns,
    compute_metrics,
    stationary_block_bootstrap,
)
from usinv.calendar import default_calendar
from usinv.ledger import (
    LedgerPosition,
    NavRecord,
    OrderReason,
    OrderRequest,
    OrderSide,
    OrderStatus,
    RunIdentity,
)

D = Decimal
CAL = default_calendar()


def _identity() -> RunIdentity:
    return RunIdentity(
        code_sha="a" * 40,
        config_hash="b" * 64,
        data_manifest_hash="c" * 64,
        evidence_mode="research",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _bar(
    security_id: str,
    session: date,
    close: str | None,
    open_price: str | None,
    *,
    available_from: datetime | None = None,
) -> MarketBar:
    return MarketBar(
        security_id,
        session,
        None if close is None else D(close),
        None if open_price is None else D(open_price),
        available_from or CAL.session(session).close_at,
        "daily_open_proxy",
        f"prices://{security_id}/{session.isoformat()}",
    )


def _position(
    security_id: str = "A",
    *,
    quantity: str = "10",
    cost: str = "10",
    high_water: str = "10",
) -> LedgerPosition:
    return LedgerPosition(
        f"POS:{security_id}",
        security_id,
        security_id,
        D(quantity),
        D(cost),
        D(high_water),
        date(2025, 12, 1),
    )


def test_three_stock_toy_ledger_matches_hand_calculation_to_cent():
    sessions = (
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
        date(2026, 1, 8),
    )
    bars = (
        _bar("A", sessions[0], "10", "10"),
        _bar("B", sessions[0], "20", "20"),
        _bar("C", sessions[0], "30", "30"),
        _bar("A", sessions[1], "11", "10"),
        _bar("B", sessions[1], "21", "22"),  # Above the 5% entry collar.
        _bar("C", sessions[1], "31", "30"),
        _bar("A", sessions[2], "5.5", "5.5"),
        _bar("C", sessions[2], "30", "30"),
        _bar("A", sessions[3], "6", "6"),
    )
    orders = (
        OrderRequest("BUY-A", "A", "A", OrderSide.BUY, D("10"), sessions[0], OrderReason.ENTRY),
        OrderRequest("BUY-B", "B", "B", OrderSide.BUY, D("5"), sessions[0], OrderReason.ENTRY),
        OrderRequest("BUY-C", "C", "C", OrderSide.BUY, D("3"), sessions[0], OrderReason.ENTRY),
    )
    result = BacktestEngine(
        identity=_identity(),
        initial_settled_cash=D("1000"),
    ).run(
        sessions,
        bars,
        order_requests=orders,
        splits=(SplitEvent("A", sessions[2], D("2"), "actions://A/split"),),
        dividends=(DividendEvent("C", sessions[2], D("1"), "actions://C/dividend"),),
        terminations=(
            TerminationEvent(
                "C",
                sessions[3],
                TerminationKind.UNKNOWN,
                "form25://C",
                final_close=D("30"),
            ),
            TerminationEvent(
                "A",
                sessions[4],
                TerminationKind.ACQUISITION,
                "8k://A/acquisition",
                cash_consideration_per_share=D("6.50"),
            ),
        ),
    )
    ledger = result.ledger

    # A: $100 + $0.40 cost; C: $90 + $0.36 cost. B remains cash.
    assert result.total_costs == D("0.76")
    assert ledger.nav[1].settled_cash == D("809.24")
    assert ledger.nav[2].settled_cash == D("812.24")  # $3 dividend.
    assert ledger.nav[-1].nav == D("942.24")  # Unknown C = $0; A acquisition = $130.
    assert ledger.settled_cash == D("942.24")
    assert ledger.positions == ()

    buy_b = next(item for item in ledger.orders if item.root_order_id == "BUY-B")
    assert buy_b.status is OrderStatus.CANCELLED
    assert len(ledger.fills) == 2
    assert [item.action_type for item in ledger.actions] == ["split", "cash_dividend"]

    unknown, acquisition = ledger.terminations
    assert unknown.kind == TerminationKind.UNKNOWN
    assert unknown.primary_value == D("0.00")
    assert unknown.final_close_sensitivity_value == D("90.00")
    assert unknown.haircut_30_sensitivity_value == D("63.00")
    assert acquisition.kind == TerminationKind.ACQUISITION
    assert acquisition.primary_value == D("130.00")


def test_exit_retries_with_latest_close_then_sale_cash_settles_t_plus_one():
    sessions = (
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
    )
    bars = (
        _bar("A", sessions[0], "10", "10"),
        _bar("A", sessions[1], "9", "9"),
        _bar("A", sessions[2], "8.7", "8.6"),
    )
    request = OrderRequest(
        "SELL-A",
        "A",
        "A",
        OrderSide.SELL,
        D("10"),
        sessions[0],
        OrderReason.ROUTINE_EXIT,
    )
    result = BacktestEngine(
        identity=_identity(),
        initial_settled_cash=D("0"),
        initial_positions=(_position(),),
    ).run(sessions, bars, order_requests=(request,))

    assert [item.status for item in result.ledger.orders] == [
        OrderStatus.RETRY,
        OrderStatus.FILLED,
    ]
    retry = result.ledger.orders[1]
    assert retry.reference_close == D("9")
    assert retry.limit_price == D("8.55")
    assert result.ledger.nav[2].settled_cash == D("0.00")
    assert result.ledger.nav[2].unsettled_cash == D("85.66")
    assert result.ledger.nav[3].settled_cash == D("85.66")


def test_thesis_break_uses_ten_percent_default_collar():
    sessions = (date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6))
    bars = (
        _bar("A", sessions[0], "10", "10"),
        _bar("A", sessions[1], "9", "9.2"),
    )
    request = OrderRequest(
        "THESIS-A",
        "A",
        "A",
        OrderSide.SELL,
        D("10"),
        sessions[0],
        OrderReason.THESIS_BREAK,
    )
    result = BacktestEngine(
        identity=_identity(),
        initial_settled_cash=D("0"),
        initial_positions=(_position(),),
    ).run(sessions, bars, order_requests=(request,))
    assert result.ledger.orders[0].collar_fraction == D("0.10")
    assert result.ledger.orders[0].status is OrderStatus.FILLED


def test_exit_raises_manual_exception_after_initial_open_and_three_retries():
    sessions = (
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
        date(2026, 1, 8),
    )
    bars = tuple(_bar("A", session, "10", "9") for session in sessions)
    request = OrderRequest(
        "SELL-A",
        "A",
        "A",
        OrderSide.SELL,
        D("10"),
        sessions[0],
        OrderReason.ROUTINE_EXIT,
    )
    with pytest.raises(ManualExecutionRequired, match="three retries"):
        BacktestEngine(
            identity=_identity(),
            initial_settled_cash=D("0"),
            initial_positions=(_position(),),
        ).run(sessions, bars, order_requests=(request,))


def test_same_auction_sale_does_not_fund_a_buy_in_cash_account():
    sessions = (date(2026, 1, 2), date(2026, 1, 5))
    bars = (
        _bar("A", sessions[0], "10", "10"),
        _bar("B", sessions[0], "10", "10"),
        _bar("A", sessions[1], "10", "10"),
        _bar("B", sessions[1], "10", "10"),
    )
    requests = (
        OrderRequest(
            "SELL-A",
            "A",
            "A",
            OrderSide.SELL,
            D("10"),
            sessions[0],
            OrderReason.ROUTINE_EXIT,
        ),
        OrderRequest(
            "BUY-B",
            "B",
            "B",
            OrderSide.BUY,
            D("10"),
            sessions[0],
            OrderReason.ENTRY,
        ),
    )
    result = BacktestEngine(
        identity=_identity(),
        initial_settled_cash=D("0"),
        initial_positions=(_position(),),
    ).run(sessions, bars, order_requests=requests)
    statuses = {item.root_order_id: item.status for item in result.ledger.orders}
    assert statuses == {"BUY-B": OrderStatus.CANCELLED, "SELL-A": OrderStatus.FILLED}
    assert result.ledger.settled_cash == D("0.00")
    assert result.ledger.unsettled_settlements[0].amount == D("99.60")


def test_signal_rejects_close_published_after_official_cutoff():
    session = date(2026, 1, 2)
    late = CAL.session(session).close_at.replace(hour=16, minute=1)
    with pytest.raises(BacktestError, match="availability"):
        BacktestEngine(
            identity=_identity(),
            initial_settled_cash=D("100"),
        ).run((session,), (_bar("A", session, "10", "10", available_from=late),))


def test_unvalued_stock_acquisition_is_quarantined_not_guessed():
    session = date(2026, 1, 2)
    with pytest.raises(TerminationEvidenceError, match="cannot be valued"):
        BacktestEngine(
            identity=_identity(),
            initial_settled_cash=D("0"),
            initial_positions=(_position(),),
        ).run(
            (session,),
            (_bar("A", session, "10", "10"),),
            terminations=(
                TerminationEvent(
                    "A",
                    session,
                    TerminationKind.ACQUISITION,
                    "merger://A",
                    stock_consideration_requires_valuation=True,
                ),
            ),
        )


def test_otc_removal_uses_first_permitted_next_open_and_records_evidence():
    sessions = (date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6))
    bars = (
        _bar("A", sessions[0], "10", "10"),
        _bar("A", sessions[1], "9.8", "9.7"),
    )
    result = BacktestEngine(
        identity=_identity(),
        initial_settled_cash=D("0"),
        initial_positions=(_position(),),
    ).run(
        sessions,
        bars,
        terminations=(
            TerminationEvent(
                "A",
                sessions[0],
                TerminationKind.OTC_REMOVAL,
                "form25://A/otc",
            ),
        ),
    )
    assert result.ledger.fills[0].session == sessions[1]
    assert result.ledger.orders[0].reason is OrderReason.OTC_EXIT
    assert result.ledger.terminations[0].kind == TerminationKind.OTC_REMOVAL
    assert result.ledger.terminations[0].evidence_pointer == "form25://A/otc"
    assert result.ledger.settled_cash == D("96.61")


@pytest.mark.parametrize(
    ("recovery", "expected"),
    [(None, D("0.00")), (D("1.25"), D("12.50"))],
)
def test_bankruptcy_uses_documented_recovery_or_conservative_total_loss(
    recovery: Decimal | None,
    expected: Decimal,
):
    session = date(2026, 1, 2)
    result = BacktestEngine(
        identity=_identity(),
        initial_settled_cash=D("0"),
        initial_positions=(_position(),),
    ).run(
        (session,),
        (_bar("A", session, "10", "10"),),
        terminations=(
            TerminationEvent(
                "A",
                session,
                TerminationKind.BANKRUPTCY,
                "court://A/recovery",
                documented_recovery_per_share=recovery,
            ),
        ),
    )
    assert result.ledger.terminations[0].primary_value == expected
    assert result.ledger.nav[0].nav == expected


def test_cost_model_is_charged_per_fill_and_sensitivity_changes_cash_inside_loop():
    sessions = (date(2026, 1, 2), date(2026, 1, 5))
    bars = (_bar("A", sessions[0], "10", "10"), _bar("A", sessions[1], "10", "10"))
    request = OrderRequest(
        "BUY-A",
        "A",
        "A",
        OrderSide.BUY,
        D("10"),
        sessions[0],
        OrderReason.ENTRY,
    )
    baseline = BacktestEngine(
        identity=_identity(),
        initial_settled_cash=D("200"),
        cost_model=FixedBpsCostModel(40),
    ).run(sessions, bars, order_requests=(request,))
    zero = BacktestEngine(
        identity=_identity(),
        initial_settled_cash=D("200"),
        cost_model=FixedBpsCostModel(0),
    ).run(sessions, bars, order_requests=(request,))
    assert baseline.ledger.nav[-1].settled_cash == D("99.60")
    assert zero.ledger.nav[-1].settled_cash == D("100.00")
    assert baseline.ledger.nav[-1].nav == D("199.60")
    assert zero.ledger.nav[-1].nav == D("200.00")


def test_metrics_use_canonical_net_nav_and_geometric_excess_definition():
    sessions = (date(2024, 1, 2), date(2024, 7, 2), date(2025, 1, 2))
    nav = tuple(
        NavRecord(session, D("0"), D("0"), value, value, D("0"))
        for session, value in zip(sessions, (D("100"), D("90"), D("110")), strict=True)
    )
    metrics = compute_metrics(
        nav,
        (D("100"), D("100"), D("105")),
        gross_traded_notional=D("110"),
    )
    assert metrics.max_drawdown == pytest.approx(-0.10)
    assert metrics.ulcer_index > 0
    assert metrics.one_way_turnover == pytest.approx(0.55)
    assert metrics.benchmark_relative_alpha > 0


def test_stationary_block_bootstrap_is_seeded_and_reports_both_intervals():
    strategy = tuple(0.02 if index % 2 == 0 else -0.005 for index in range(36))
    benchmark = tuple(0.005 for _ in range(36))
    first = stationary_block_bootstrap(
        strategy,
        benchmark,
        samples=200,
        expected_block_length=4,
        seed=42,
    )
    second = stationary_block_bootstrap(
        strategy,
        benchmark,
        samples=200,
        expected_block_length=4,
        seed=42,
    )
    assert first == second
    assert (
        first.annualized_active_return_95.lower
        <= first.annualized_active_return_80.lower
        <= first.annualized_active_return_80.upper
        <= first.annualized_active_return_95.upper
    )
    assert first.sharpe_95.lower <= first.sharpe_80.lower
    assert first.sharpe_80.upper <= first.sharpe_95.upper


def test_steady_returns_assessment_checks_calendar_year_relative_floor():
    nav = (
        NavRecord(date(2024, 1, 2), D("0"), D("0"), D("100"), D("100"), D("0")),
        NavRecord(date(2024, 12, 31), D("0"), D("0"), D("95"), D("95"), D("0")),
        NavRecord(date(2025, 12, 31), D("0"), D("0"), D("90"), D("90"), D("0")),
    )
    metrics = BacktestMetrics(
        cagr=-0.05,
        sharpe=-0.1,
        sortino=-0.1,
        max_drawdown=-0.10,
        ulcer_index=0.08,
        rolling_12m_win_rate=0.60,
        one_way_turnover=0.2,
        benchmark_relative_alpha=0.0,
        regression_alpha=0.0,
    )
    passing = assess_steady_returns(
        nav,
        (D("100"), D("100"), D("100")),
        strategy_metrics=metrics,
        benchmark_max_drawdown=-0.15,
        benchmark_ulcer_index=0.10,
    )
    assert passing.passes

    failing = assess_steady_returns(
        nav,
        (D("100"), D("110"), D("130")),
        strategy_metrics=metrics,
        benchmark_max_drawdown=-0.15,
        benchmark_ulcer_index=0.10,
    )
    assert not failing.calendar_year_pass
