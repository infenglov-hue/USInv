"""Phase 3.4 vintage macro, regime signals, overlays and weight tests."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from usinv.calendar import EXCHANGE_TIMEZONE, XNYSCalendar
from usinv.config import load_config
from usinv.config.loader import FactorWeights
from usinv.data.macro import (
    HY_OAS_SERIES,
    MacroDataError,
    MacroRawArchive,
    VintagedObservation,
    hyg_lqd_credit_stress,
    observations_as_of,
    parse_alfred_vintages,
    parse_cboe_vix_csv,
    parse_fred_observations,
)
from usinv.regime import (
    MarketRegime,
    OverlayError,
    TrendBand,
    classify_market_regime,
    evaluate_overlay,
    hy_oas_credit_stress,
    moving_average_trend,
    regime_conditional_weights,
    slow_gate_directives,
    slow_regime_signals,
)

FIXTURE = Path(__file__).parent / "fixtures" / "regime" / "historical_states.json"
UTC = ZoneInfo("UTC")


def _observation(
    series_id: str,
    day: date,
    available_from: datetime,
    value: float,
    suffix: str,
    *,
    source: str = "fixture",
    vintage_date: date | None = None,
) -> VintagedObservation:
    return VintagedObservation(
        series_id=series_id,
        observation_date=day,
        available_from=available_from,
        value=value,
        source=source,
        evidence_pointer=f"fixture:{series_id}:{suffix}",
        vintage_date=vintage_date,
    )


def _session_labels(end: date, count: int) -> list[date]:
    calendar = XNYSCalendar(start=date(2018, 1, 1), end=date(2025, 12, 31))
    labels = [calendar.session(end).label]
    while len(labels) < count:
        labels.append(calendar.previous_session(labels[-1]).label)
    return list(reversed(labels))


def _market_series(
    *,
    series_id: str,
    as_of: datetime,
    count: int,
    baseline: float,
    latest: float,
    lagged: bool = False,
) -> tuple[VintagedObservation, ...]:
    calendar = XNYSCalendar(start=date(2018, 1, 1), end=date(2025, 12, 31))
    end = calendar.previous_session(as_of.date()).label if lagged else as_of.date()
    labels = _session_labels(end, count)
    rows: list[VintagedObservation] = []
    for index, label in enumerate(labels):
        if lagged:
            available = datetime.combine(label + timedelta(days=1), time(9), EXCHANGE_TIMEZONE)
        else:
            available = calendar.session(label).close_at
        rows.append(
            _observation(
                series_id,
                label,
                available,
                latest if index == len(labels) - 1 else baseline,
                str(index),
            )
        )
    return tuple(rows)


def _overlay(overlay_id: str):
    return next(item for item in load_config().regime.overlays if item.overlay_id == overlay_id)


# --------------------------------------------------------------------------- #
# Canonical vintages and provider parsing.
# --------------------------------------------------------------------------- #
def test_vintage_resolution_hides_future_revision():
    initial = _observation(
        "NFCI",
        date(2020, 3, 20),
        datetime(2020, 3, 25, 8, 30, tzinfo=EXCHANGE_TIMEZONE),
        0.4,
        "initial",
        vintage_date=date(2020, 3, 25),
    )
    future_revision = _observation(
        "NFCI",
        date(2020, 3, 20),
        datetime(2023, 1, 4, 8, 30, tzinfo=EXCHANGE_TIMEZONE),
        -0.2,
        "revision",
        vintage_date=date(2023, 1, 4),
    )
    old = observations_as_of(
        (initial, future_revision),
        series_id="NFCI",
        as_of=datetime(2020, 3, 27, 16, tzinfo=EXCHANGE_TIMEZONE),
    )
    revised = observations_as_of(
        (initial, future_revision),
        series_id="NFCI",
        as_of=datetime(2023, 1, 5, 16, tzinfo=EXCHANGE_TIMEZONE),
    )
    assert old[0].value == pytest.approx(0.4)
    assert revised[0].value == pytest.approx(-0.2)


def test_equal_time_conflicting_macro_vintages_fail_closed():
    available = datetime(2020, 3, 25, 8, 30, tzinfo=EXCHANGE_TIMEZONE)
    rows = (
        _observation("NFCI", date(2020, 3, 20), available, 0.4, "a"),
        _observation("NFCI", date(2020, 3, 20), available, -0.2, "b"),
    )
    with pytest.raises(MacroDataError, match="conflicting"):
        observations_as_of(rows, series_id="NFCI", as_of=available)


def test_alfred_parser_requires_exact_publication_instant_and_retains_vintage():
    payload = json.dumps(
        {
            "observations": [
                {
                    "date": "2020-03-20",
                    "realtime_start": "2020-03-25",
                    "realtime_end": "2023-01-03",
                    "value": "0.40",
                },
                {
                    "date": "2020-03-20",
                    "realtime_start": "2023-01-04",
                    "realtime_end": "9999-12-31",
                    "value": "-0.20",
                },
            ]
        }
    ).encode()
    initial_at = datetime(2020, 3, 25, 8, 30, tzinfo=EXCHANGE_TIMEZONE)
    revised_at = datetime(2023, 1, 4, 8, 30, tzinfo=EXCHANGE_TIMEZONE)
    rows = parse_alfred_vintages(
        payload,
        series_id="NFCI",
        publication_instants={
            date(2020, 3, 25): initial_at,
            date(2023, 1, 4): revised_at,
        },
    )
    assert [item.vintage_date for item in rows] == [date(2020, 3, 25), date(2023, 1, 4)]
    with pytest.raises(MacroDataError, match="missing official publication"):
        parse_alfred_vintages(
            payload,
            series_id="NFCI",
            publication_instants={date(2020, 3, 25): initial_at},
        )


def test_sahmcurrent_is_structurally_forbidden():
    payload = b'{"observations":[]}'
    with pytest.raises(MacroDataError, match="SAHMCURRENT"):
        parse_fred_observations(
            payload,
            series_id="SAHMCURRENT",
            publication_instants={},
        )


def test_cboe_vix_parser_requires_official_availability():
    payload = b"DATE,OPEN,HIGH,LOW,CLOSE\n03/23/2020,60,82,55,82.69\n"
    available = datetime(2020, 3, 23, 16, 15, tzinfo=EXCHANGE_TIMEZONE)
    rows = parse_cboe_vix_csv(
        payload,
        publication_instants={date(2020, 3, 23): available},
    )
    assert rows[0].value == pytest.approx(82.69)
    assert rows[0].available_from == available
    with pytest.raises(MacroDataError, match="missing timezone-aware"):
        parse_cboe_vix_csv(payload, publication_instants={})


def test_macro_archive_is_content_addressed_and_redacts_api_key(tmp_path):
    archive = MacroRawArchive(tmp_path / "macro")
    record = archive.store(
        source="fred",
        series_id="NFCI",
        retrieved_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
        request_url="https://api.stlouisfed.org/fred/series?series_id=NFCI&api_key=secret",
        payload=b'{"observations":[]}',
    )
    assert "secret" not in record.request_url
    assert record.payload_sha256 in record.object_path
    archive.verify(record)


# --------------------------------------------------------------------------- #
# Registered historical-state acceptance gate.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scenario_name", ["2020-03-risk-off", "2022-bear", "2023-chop"])
def test_registered_historical_regime_states_from_visible_vintages_only(scenario_name):
    scenario = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"][scenario_name]
    as_of = datetime.fromisoformat(scenario["as_of"])
    spy = _market_series(
        series_id="SPY_TR",
        as_of=as_of,
        count=200,
        baseline=scenario["spy_baseline"],
        latest=scenario["spy_latest"],
    )
    oas = _market_series(
        series_id=HY_OAS_SERIES,
        as_of=as_of,
        count=63,
        baseline=scenario["oas_baseline_bps"],
        latest=scenario["oas_latest_bps"],
        lagged=True,
    )
    trend = moving_average_trend(
        spy,
        series_id="SPY_TR",
        as_of=as_of,
        window_observations=200,
        hysteresis_fraction=0.02,
    )
    credit = hy_oas_credit_stress(
        oas,
        as_of=as_of,
        absolute_threshold_bps=500,
        moving_average_sessions=63,
    )
    assert classify_market_regime(trend, credit) is MarketRegime(scenario["expected"])


def test_shift_all_availability_plus_seven_days_changes_regime_access():
    as_of = datetime(2022, 6, 17, 16, tzinfo=EXCHANGE_TIMEZONE)
    spy = _market_series(
        series_id="SPY_TR",
        as_of=as_of,
        count=200,
        baseline=100,
        latest=80,
    )
    shifted = tuple(
        VintagedObservation(
            series_id=item.series_id,
            observation_date=item.observation_date,
            available_from=item.available_from + timedelta(days=7),
            value=item.value,
            source=item.source,
            evidence_pointer=item.evidence_pointer,
            vintage_date=item.vintage_date,
        )
        for item in spy
    )
    with pytest.raises(MacroDataError, match="insufficient"):
        moving_average_trend(
            shifted,
            series_id="SPY_TR",
            as_of=as_of,
            window_observations=200,
            hysteresis_fraction=0.02,
        )


# --------------------------------------------------------------------------- #
# O0-O3 overlay contracts.
# --------------------------------------------------------------------------- #
def test_o0_never_reads_market_data():
    decision = evaluate_overlay(
        _overlay("O0"),
        as_of=datetime(2023, 3, 17, 16, tzinfo=EXCHANGE_TIMEZONE),
    )
    assert decision.gross_exposure == pytest.approx(1.0)
    assert decision.risk_off is False


def test_o1_uses_two_percent_hysteresis_and_retains_previous_state_in_band():
    as_of = datetime(2023, 3, 17, 16, tzinfo=EXCHANGE_TIMEZONE)
    spy = _market_series(
        series_id="SPY_TR",
        as_of=as_of,
        count=200,
        baseline=100,
        latest=100.5,
    )
    retained = evaluate_overlay(
        _overlay("O1"),
        as_of=as_of,
        spy_total_return=spy,
        previous_risk_off=True,
    )
    assert retained.trend is not None and retained.trend.band is TrendBand.HYSTERESIS
    assert retained.risk_off is True
    assert retained.gross_exposure == pytest.approx(0.0)


def test_o2_changes_only_on_declared_month_end_evaluation():
    as_of = datetime(2023, 10, 31, 16, tzinfo=EXCHANGE_TIMEZONE)
    days = (
        date(2023, 1, 31),
        date(2023, 2, 28),
        date(2023, 3, 31),
        date(2023, 4, 28),
        date(2023, 5, 31),
        date(2023, 6, 30),
        date(2023, 7, 31),
        date(2023, 8, 31),
        date(2023, 9, 29),
        date(2023, 10, 31),
    )
    calendar = XNYSCalendar(start=date(2023, 1, 1), end=date(2024, 1, 31))
    spy = tuple(
        _observation(
            "SPY_TR",
            day,
            calendar.session(day).close_at,
            80 if index == len(days) - 1 else 100,
            str(index),
        )
        for index, day in enumerate(days)
    )
    held = evaluate_overlay(
        _overlay("O2"),
        as_of=as_of,
        spy_total_return=spy,
        previous_risk_off=False,
        is_month_end_evaluation=False,
    )
    evaluated = evaluate_overlay(
        _overlay("O2"),
        as_of=as_of,
        spy_total_return=spy,
        previous_risk_off=False,
        is_month_end_evaluation=True,
    )
    assert held.evaluated is False and held.risk_off is False
    assert evaluated.risk_off is True
    assert evaluated.gross_exposure == pytest.approx(0.5)


def test_o3_requires_dual_credit_confirmation_and_supports_explicit_fallback():
    as_of = datetime(2020, 3, 23, 16, tzinfo=EXCHANGE_TIMEZONE)
    spy = _market_series(
        series_id="SPY_TR",
        as_of=as_of,
        count=200,
        baseline=100,
        latest=70,
    )
    no_stress_oas = _market_series(
        series_id=HY_OAS_SERIES,
        as_of=as_of,
        count=63,
        baseline=350,
        latest=450,
        lagged=True,
    )
    unconfirmed = evaluate_overlay(
        _overlay("O3"),
        as_of=as_of,
        spy_total_return=spy,
        hy_oas_bps=no_stress_oas,
    )
    assert unconfirmed.risk_off is False

    hyg = _market_series(
        series_id="HYG_TR",
        as_of=as_of,
        count=63,
        baseline=100,
        latest=75,
    )
    lqd = _market_series(
        series_id="LQD_TR",
        as_of=as_of,
        count=63,
        baseline=100,
        latest=100,
    )
    fallback = hyg_lqd_credit_stress(
        hyg,
        lqd,
        as_of=as_of,
        window_sessions=63,
        threshold_z=-1.0,
    )
    confirmed = evaluate_overlay(
        _overlay("O3"),
        as_of=as_of,
        spy_total_return=spy,
        fallback_credit=fallback,
    )
    assert fallback.stressed is True
    assert confirmed.risk_off is True
    assert confirmed.credit_source == "hyg_lqd_tr_zscore"


def test_o3_missing_credit_evidence_fails_closed():
    as_of = datetime(2020, 3, 23, 16, tzinfo=EXCHANGE_TIMEZONE)
    spy = _market_series(
        series_id="SPY_TR",
        as_of=as_of,
        count=200,
        baseline=100,
        latest=70,
    )
    with pytest.raises(OverlayError, match="requires visible"):
        evaluate_overlay(_overlay("O3"), as_of=as_of, spy_total_return=spy)


# --------------------------------------------------------------------------- #
# Slow signals and explicit regime weight policy.
# --------------------------------------------------------------------------- #
def test_slow_signals_use_visible_nfci_vintage_and_sahm_realtime():
    as_of = datetime(2020, 5, 8, 16, tzinfo=EXCHANGE_TIMEZONE)
    nfci = (
        _observation(
            "NFCI",
            date(2020, 5, 1),
            datetime(2020, 5, 6, 8, 30, tzinfo=EXCHANGE_TIMEZONE),
            0.7,
            "initial",
        ),
        _observation(
            "NFCI",
            date(2020, 5, 1),
            datetime(2022, 1, 5, 8, 30, tzinfo=EXCHANGE_TIMEZONE),
            -0.1,
            "future-revision",
        ),
    )
    sahm = (
        _observation(
            "SAHMREALTIME",
            date(2020, 4, 1),
            datetime(2020, 5, 8, 8, 30, tzinfo=EXCHANGE_TIMEZONE),
            4.0,
            "jobs-release",
        ),
    )
    signals = slow_regime_signals(nfci, sahm, as_of=as_of)
    assert signals.nfci_value == pytest.approx(0.7)
    assert signals.tighter_financial_conditions is True
    assert signals.sahm_triggered is True
    directives = slow_gate_directives(signals)
    assert directives.shade_gross_exposure is True
    assert directives.tighten_quality_gate is True
    assert directives.magnitude_registered is False


def test_regime_weights_require_explicit_momentum_deweight():
    base = FactorWeights(quality=0.4, value=0.3, momentum=0.3)
    defensive = FactorWeights(quality=0.5, value=0.35, momentum=0.15)
    normal = regime_conditional_weights(
        base,
        defensive,
        below_trend=False,
        high_volatility=False,
    )
    risk = regime_conditional_weights(
        base,
        defensive,
        below_trend=True,
        high_volatility=False,
    )
    assert normal.weights is base
    assert risk.weights is defensive
    assert risk.defensive is True
