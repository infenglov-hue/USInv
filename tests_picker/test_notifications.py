"""Unit tests for the BIST Stock Picker notification system."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, date
import json

import pandas as pd
import requests
from click.testing import CliRunner

from us_picker.cli import cli
from us_picker.notifications.telegram import TelegramNotifier
from us_picker.notifications.event_analyzer import EventAnalyzer
from us_picker.data.sources.sec_feed import SECFilingFeed, filing_title, document_text
from us_picker.notifications.monitor_alerts import monitor_portfolio


@pytest.fixture
def mock_telegram():
    with patch("us_picker.notifications.telegram.requests.post") as mock_post:
        yield mock_post


def test_telegram_notifier_success(mock_telegram):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"ok": True}
    mock_telegram.return_value = mock_response

    notifier = TelegramNotifier("fake_token", "fake_chat")
    res = notifier.send_message("test message")

    assert res is True
    mock_telegram.assert_called_once()
    args, kwargs = mock_telegram.call_args
    assert kwargs["json"]["chat_id"] == "fake_chat"
    assert kwargs["json"]["text"] == "test message"


def test_telegram_notifier_disabled(mock_telegram):
    notifier = TelegramNotifier("fake_token", "fake_chat", enabled=False)
    res = notifier.send_message("test message")

    assert res is False
    mock_telegram.assert_not_called()


def test_telegram_notifier_missing_args(mock_telegram):
    notifier = TelegramNotifier("", "")
    res = notifier.send_message("test")
    assert res is False
    mock_telegram.assert_not_called()


@patch("us_picker.notifications.telegram.time.sleep")
def test_telegram_notifier_retries_transient_network_failure(mock_sleep, mock_telegram):
    success = MagicMock()
    success.status_code = 200
    success.json.return_value = {"ok": True}
    mock_telegram.side_effect = [
        requests.ConnectionError("temporary"),
        success,
    ]

    notifier = TelegramNotifier("fake_token", "fake_chat")

    assert notifier.send_message("retry me") is True
    assert mock_telegram.call_count == 2
    mock_sleep.assert_called_once_with(1)


@patch("us_picker.notifications.telegram.time.sleep")
def test_telegram_notifier_honors_rate_limit_retry(mock_sleep, mock_telegram):
    limited = MagicMock()
    limited.status_code = 429
    limited.json.return_value = {
        "ok": False,
        "parameters": {"retry_after": 2},
    }
    success = MagicMock()
    success.status_code = 200
    success.json.return_value = {"ok": True}
    mock_telegram.side_effect = [limited, success]

    notifier = TelegramNotifier("fake_token", "fake_chat")

    assert notifier.send_message("retry me") is True
    mock_sleep.assert_called_once_with(2)


def test_telegram_notifier_rejects_overlong_message(mock_telegram):
    notifier = TelegramNotifier("fake_token", "fake_chat")

    assert notifier.send_message("x" * 4097) is False
    mock_telegram.assert_not_called()


def test_alert_state_is_normalized_and_saved_atomically(tmp_path):
    state_path = tmp_path / "alerts_state.json"
    state_path.write_text(
        json.dumps(
            {
                "processed_kap_ids": ["1", "1", None],
                "notified_exits": {
                    "ASELS_2026-06-01": ["STOP_LOSS", "STOP_LOSS", "INVALID"],
                    "bad": "not-a-list",
                },
                "last_weekly_report_date": 123,
            }
        ),
        encoding="utf-8",
    )

    with patch("us_picker.notifications.monitor_alerts._STATE_PATH", state_path):
        from us_picker.notifications.monitor_alerts import load_state, save_state

        loaded = load_state()
        assert loaded == {
            "processed_kap_ids": ["1"],
            "notified_exits": {"ASELS_2026-06-01": ["STOP_LOSS"]},
            "last_weekly_report_date": "",
        }

        save_state(loaded)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted == loaded
    assert not state_path.with_suffix(".json.tmp").exists()


def test_alert_state_save_failure_is_not_silenced(tmp_path):
    state_path = tmp_path / "alerts_state.json"

    with (
        patch("us_picker.notifications.monitor_alerts._STATE_PATH", state_path),
        patch("pathlib.Path.replace", side_effect=OSError("disk unavailable")),
    ):
        from us_picker.notifications.monitor_alerts import save_state

        with pytest.raises(OSError, match="disk unavailable"):
            save_state(
                {
                    "processed_kap_ids": [],
                    "notified_exits": {},
                    "last_weekly_report_date": "",
                }
            )

    assert not state_path.with_suffix(".json.tmp").exists()


def test_monitor_alerts_cli_returns_failure_when_monitor_crashes():
    runner = CliRunner()

    with patch(
        "us_picker.notifications.monitor_alerts.monitor_portfolio",
        side_effect=RuntimeError("boom"),
    ):
        result = runner.invoke(cli, ["monitor-alerts"])

    assert result.exit_code != 0
    assert "Monitoring failed: boom" in result.output


@patch("us_picker.notifications.event_analyzer.genai.Client")
def test_event_analyzer_kap(mock_genai_client):
    mock_client = MagicMock()
    mock_genai_client.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = "SENTIMENT: POSITIVE\nSUMMARY: Bu şirket çok kar elde etmiş."
    mock_client.models.generate_content.return_value = mock_response

    analyzer = EventAnalyzer()
    analyzer.api_key = "fake_key"
    res = analyzer.analyze_kap_event("ASELS", "Mali Rapor", "Nakitimiz arttı.")

    assert res["sentiment"] == "POSITIVE"
    assert res["summary"] == "Bu şirket çok kar elde etmiş."


@patch("us_picker.notifications.event_analyzer.genai.Client")
def test_event_analyzer_exit_alert(mock_genai_client):
    mock_client = MagicMock()
    mock_genai_client.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = "ASELS stop seviyesini kırdı, portföy disiplini açısından çıkış yapılıyor."
    mock_client.models.generate_content.return_value = mock_response

    analyzer = EventAnalyzer()
    analyzer.api_key = "fake_key"
    res = analyzer.generate_exit_alert("ASELS", "STOP_LOSS", 10.0, 12.0)

    assert "cıkış yapılıyor" in res.lower() or "çıkış yapılıyor" in res.lower()


class _StubSECClient:
    def fetch_submissions(self, cik):
        return {"filings": {"recent": {
            "form": ["4", "8-K", "10-Q"],
            "accessionNumber": ["0000-26-1", "0000320193-26-000031", "0000320193-26-000020"],
            "primaryDocument": ["x.xml", "aapl-8k.htm", "aapl-10q.htm"],
            "items": ["", "2.02,9.01", ""],
            "acceptanceDateTime": ["2026-09-01T16:05:00.000Z", "2026-07-31T16:30:12.000Z",
                                   "2026-07-31T18:01:02.000Z"],
        }}}

    def _get(self, url):
        class _Response:
            text = "<html><body><p>Apple reports record quarter</p><script>x()</script></body></html>"
        return _Response()


def test_sec_filing_feed_lists_material_filings_and_extracts_text():
    feed = SECFilingFeed(client=_StubSECClient(), cik_lookup=lambda ticker: 320193)
    discs = feed.get_latest_disclosures("AAPL", limit=2)

    assert [d["id"] for d in discs] == ["0000320193-26-000031", "0000320193-26-000020"]
    assert discs[0]["title"] == "8-K: Results of operations, Item 9.01"
    assert discs[0]["url"].endswith("/320193/000032019326000031/aapl-8k.htm")
    text = feed.get_disclosure_text(discs[0]["id"])
    assert "Apple reports record quarter" in text
    assert "x()" not in text


def test_sec_feed_helpers():
    assert filing_title("10-K", "") == "10-K"
    assert document_text("<p>a</p><p>b</p>") == "a" + chr(10) + "b"


@patch("us_picker.notifications.monitor_alerts.send_weekly_report_if_monday")
@patch("us_picker.notifications.monitor_alerts.get_open_positions")
@patch("us_picker.notifications.monitor_alerts.fetch_live_price")
@patch("us_picker.notifications.monitor_alerts.SECFilingFeed")
@patch("us_picker.notifications.monitor_alerts.EventAnalyzer")
@patch("us_picker.notifications.monitor_alerts.TelegramNotifier")
@patch("us_picker.notifications.monitor_alerts.load_state")
@patch("us_picker.notifications.monitor_alerts.save_state")
def test_monitor_portfolio_flow(
    mock_save_state, mock_load_state,
    mock_tg_notifier, mock_analyzer, mock_kap_feed,
    mock_yf_ticker, mock_get_open_positions,
    mock_send_weekly_report
):
    open_pos = pd.DataFrame([
        {
            "ticker": "THYAO",
            "name": "THY",
            "entry_price": 100.0,
            "current_price": 85.0,
            "target_price": 150.0,
            "stop_loss_price": 90.0,  # Trigger stop loss
            "selection_date": date(2026, 5, 21),
            "reason_top_factors_json": "{}"
        },
        {
            "ticker": "ASELS",
            "name": "ASELSAN",
            "entry_price": 50.0,
            "current_price": 65.0,
            "target_price": 60.0,  # Trigger take profit
            "stop_loss_price": 45.0,
            "selection_date": date(2026, 5, 21),
            "reason_top_factors_json": "{}"
        }
    ])
    mock_get_open_positions.return_value = open_pos

    mock_yf_ticker.side_effect = lambda ticker: 85.0 if ticker == "THYAO" else 65.0

    mock_load_state.return_value = {
        "processed_kap_ids": [],
        "notified_exits": {}
    }

    feed_instance = MagicMock()
    def get_latest_disclosures_side_effect(ticker, limit=5):
        if ticker == "THYAO":
            return [{"id": "999", "title": "Finansal Sonuc", "url": "url", "ticker": "THYAO", "date": datetime.now()}]
        return [{"id": "888", "title": "Finansal Sonuc", "url": "url", "ticker": "ASELS", "date": datetime.now()}]
    feed_instance.get_latest_disclosures.side_effect = get_latest_disclosures_side_effect
    feed_instance.get_disclosure_text.return_value = "Sirket karini artirdi."
    mock_kap_feed.return_value = feed_instance

    analyzer_instance = MagicMock()
    analyzer_instance.generate_exit_alert.return_value = "Yapay zeka cikis aciklamasi."
    analyzer_instance.analyze_kap_event.return_value = {"sentiment": "POSITIVE", "summary": "Kar artisi."}
    mock_analyzer.return_value = analyzer_instance

    notifier_instance = MagicMock()
    notifier_instance.send_message.return_value = True
    mock_tg_notifier.return_value = notifier_instance

    monitor_portfolio()

    assert analyzer_instance.generate_exit_alert.call_count == 2
    feed_instance.get_latest_disclosures.assert_called()
    analyzer_instance.analyze_kap_event.assert_any_call("THYAO", "Finansal Sonuc", "Sirket karini artirdi.")
    analyzer_instance.analyze_kap_event.assert_any_call("ASELS", "Finansal Sonuc", "Sirket karini artirdi.")

    mock_save_state.assert_called_once()
    saved_state = mock_save_state.call_args[0][0]
    assert "999" in saved_state["processed_kap_ids"]
    assert "888" in saved_state["processed_kap_ids"]
    assert "STOP_LOSS" in saved_state["notified_exits"]["THYAO_2026-05-21"]
    assert "TAKE_PROFIT" in saved_state["notified_exits"]["ASELS_2026-05-21"]


@patch("us_picker.notifications.monitor_alerts.send_weekly_report_if_monday")
@patch("us_picker.notifications.monitor_alerts.get_open_positions")
@patch("us_picker.notifications.monitor_alerts.SECFilingFeed")
@patch("us_picker.notifications.monitor_alerts.EventAnalyzer")
@patch("us_picker.notifications.monitor_alerts.TelegramNotifier")
@patch("us_picker.notifications.monitor_alerts.load_state")
@patch("us_picker.notifications.monitor_alerts.save_state")
def test_monitor_skips_kap_when_no_open_positions(
    mock_save_state,
    mock_load_state,
    mock_tg_notifier,
    mock_analyzer,
    mock_kap_feed,
    mock_get_open_positions,
    mock_send_weekly_report,
):
    mock_get_open_positions.return_value = pd.DataFrame()
    mock_load_state.return_value = {
        "processed_kap_ids": [],
        "notified_exits": {},
        "last_weekly_report_date": "",
    }

    feed = MagicMock()
    feed.get_latest_disclosures.return_value = [
        {
            "id": "123",
            "title": "Kâr & <script>alert(1)</script>",
            "url": "https://kap.org.tr/tr/Bildirim/123?a=1&b=2",
        }
    ]
    feed.get_disclosure_text.return_value = "Açıklama"
    mock_kap_feed.return_value = feed

    analyzer = MagicMock()
    analyzer.analyze_kap_event.return_value = {
        "sentiment": "POSITIVE",
        "summary": "Özet <b>güçlü</b> & olumlu",
    }
    mock_analyzer.return_value = analyzer

    notifier = MagicMock()
    notifier.send_message.return_value = True
    mock_tg_notifier.return_value = notifier

    monitor_portfolio()

    feed.get_latest_disclosures.assert_not_called()
    analyzer.analyze_kap_event.assert_not_called()
    notifier.send_message.assert_not_called()
    assert mock_save_state.call_args.args[0]["processed_kap_ids"] == []
    mock_send_weekly_report.assert_called_once()


@patch("us_picker.notifications.monitor_alerts.send_weekly_report_if_monday")
@patch("us_picker.notifications.monitor_alerts.get_open_positions")
@patch("us_picker.notifications.monitor_alerts.fetch_live_price")
@patch("us_picker.notifications.monitor_alerts.SECFilingFeed")
@patch("us_picker.notifications.monitor_alerts.EventAnalyzer")
@patch("us_picker.notifications.monitor_alerts.TelegramNotifier")
@patch("us_picker.notifications.monitor_alerts.load_state")
@patch("us_picker.notifications.monitor_alerts.save_state")
def test_monitor_escapes_kap_html_for_open_position_only(
    mock_save_state,
    mock_load_state,
    mock_tg_notifier,
    mock_analyzer,
    mock_kap_feed,
    mock_yf_ticker,
    mock_get_open_positions,
    mock_send_weekly_report,
):
    mock_get_open_positions.return_value = pd.DataFrame(
        [
            {
                "ticker": "ASELS",
                "name": "ASELSAN",
                "entry_price": 50.0,
                "current_price": 55.0,
                "target_price": 80.0,
                "stop_loss_price": 40.0,
                "selection_date": date(2026, 5, 21),
                "reason_top_factors_json": "{}",
            }
        ]
    )
    mock_yf_ticker.return_value = 55.0
    mock_load_state.return_value = {
        "processed_kap_ids": [],
        "notified_exits": {},
        "last_weekly_report_date": "",
    }

    feed = MagicMock()
    feed.get_latest_disclosures.return_value = [
        {
            "id": "123",
            "title": "Kâr & <script>alert(1)</script>",
            "url": "https://kap.org.tr/tr/Bildirim/123?a=1&b=2",
        }
    ]
    feed.get_disclosure_text.return_value = "Açıklama"
    mock_kap_feed.return_value = feed

    analyzer = MagicMock()
    analyzer.analyze_kap_event.return_value = {
        "sentiment": "POSITIVE",
        "summary": "Özet <b>güçlü</b> & olumlu",
    }
    mock_analyzer.return_value = analyzer

    notifier = MagicMock()
    notifier.send_message.return_value = True
    mock_tg_notifier.return_value = notifier

    monitor_portfolio()

    feed.get_latest_disclosures.assert_called_once_with("ASELS", limit=2)
    notifier.send_message.assert_called_once()
    message = notifier.send_message.call_args.args[0]
    assert "<script>" not in message
    assert "&lt;script&gt;" in message
    assert "<b>güçlü</b>" not in message
    assert "&lt;b&gt;güçlü&lt;/b&gt;" in message
    assert 'href="https://kap.org.tr/tr/Bildirim/123?a=1&amp;b=2"' in message
    assert "123" in mock_save_state.call_args.args[0]["processed_kap_ids"]
    mock_send_weekly_report.assert_called_once()


@patch("us_picker.portfolio.rotation.load_rotation_config")
@patch("us_picker.db.connection.get_session")
def test_send_weekly_report_if_monday(mock_get_session, mock_rotation_cfg):
    mock_session = MagicMock()
    mock_get_session.return_value = mock_session
    # Bi-weekly cadence anchored at 2026-06-29 -> 2026-06-01 IS a rotation Monday
    mock_rotation_cfg.return_value = (2, date(2026, 6, 29))

    with patch("us_picker.notifications.monitor_alerts.date") as mock_date:
        mock_date.today.return_value = date(2026, 6, 1)  # 2026-06-01 is a Monday
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)

        mock_query = MagicMock()
        mock_session.query.return_value = mock_query

        # Side effect to handle multiple queries in send_weekly_report_if_monday
        call_count = [0]
        def query_side_effect(model_field):
            q = MagicMock()
            if "selection_date" in str(model_field):
                q.filter.return_value.order_by.return_value.first.return_value = (date(2026, 6, 1),)
            elif "ticker" in str(model_field):
                call_count[0] += 1
                if call_count[0] == 1:
                    # first call: new_rows
                    q.join.return_value.filter.return_value.all.return_value = [("THYAO",), ("ASELS",)]
                else:
                    # second call: exited_rows
                    q.join.return_value.filter.return_value.all.return_value = [("ASELS",), ("BIMAS",)]
            return q

        mock_session.query.side_effect = query_side_effect

        notifier = MagicMock()
        state = {"last_weekly_report_date": ""}
        positions = [
            {"ticker": "THYAO", "entry_price": 100.0, "current_price": 110.0, "pnl_pct": 10.0},
            {"ticker": "ASELS", "entry_price": 50.0, "current_price": 45.0, "pnl_pct": -10.0}
        ]

        from us_picker.notifications.monitor_alerts import send_weekly_report_if_monday as send_report
        send_report(notifier, state, positions)

        notifier.send_message.assert_called_once()
        msg = notifier.send_message.call_args[0][0]

        assert "Rotasyon Günü" in msg
        assert "2 haftada bir" in msg
        assert "bir sonraki rotasyona kadar" in msg  # hold-for-2-weeks rule
        assert "THYAO" in msg
        assert "ASELS" in msg
        assert "BIMAS" in msg
        assert "AL:</b> THYAO" in msg
        assert "SAT:</b> BIMAS" in msg
        assert "TUT:</b> ASELS" in msg
        assert state["last_weekly_report_date"] == "2026-06-01"


@patch("us_picker.portfolio.rotation.load_rotation_config")
@patch("us_picker.db.connection.get_session")
def test_weekly_report_silent_on_off_cycle_monday(mock_get_session, mock_rotation_cfg):
    """A Monday inside the hold period must NOT send the rotation report."""
    mock_get_session.return_value = MagicMock()
    # Bi-weekly anchored at 2026-06-29 -> 2026-06-08 is an off-cycle Monday
    mock_rotation_cfg.return_value = (2, date(2026, 6, 29))

    with patch("us_picker.notifications.monitor_alerts.date") as mock_date:
        mock_date.today.return_value = date(2026, 6, 8)
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)

        notifier = MagicMock()
        state = {"last_weekly_report_date": ""}

        from us_picker.notifications.monitor_alerts import send_weekly_report_if_monday as send_report
        send_report(notifier, state, [{"ticker": "THYAO", "entry_price": 1.0}])

        notifier.send_message.assert_not_called()
        assert state["last_weekly_report_date"] == ""



# ---- Applied-exit alerts (pipeline auto-closed positions) ----


def test_applied_exit_alert_sent_once_and_marker_survives_normalization():
    """Positions the pipeline auto-exited must produce exactly one alert.

    Since the cron applies check-exits to the DB, a stopped position is no
    longer open and the live-price loop cannot catch it. _notify_applied_exits
    covers that gap; the EXIT_APPLIED marker must survive state normalization
    so the alert is not re-sent every run.
    """
    from datetime import timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from us_picker.db.schema import Base, Company, PortfolioSelection
    from us_picker.notifications.monitor_alerts import (
        _normalize_state,
        _notify_applied_exits,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    company = Company(
        ticker="XEXT", name="Exit Test A.S.",
        company_type="OPERATING", is_active=True,
    )
    session.add(company)
    session.flush()
    session.add(PortfolioSelection(
        company_id=company.id,
        portfolio="ALPHA",
        selection_date=date.today() - timedelta(days=3),
        entry_price=100.0,
        exit_date=date.today() - timedelta(days=1),
        exit_price=81.0,
        exit_reason="STOP_LOSS",
        return_pct=-19.0,
    ))
    session.commit()

    notifier = MagicMock()
    notifier.send_message.return_value = True
    state = {"notified_exits": {}}

    with patch("us_picker.db.connection.get_session", return_value=session):
        _notify_applied_exits(notifier, state)
        # Second pass must be a no-op thanks to the EXIT_APPLIED marker.
        session2 = sessionmaker(bind=engine)()
        with patch("us_picker.db.connection.get_session", return_value=session2):
            _notify_applied_exits(notifier, state)

    assert notifier.send_message.call_count == 1
    message = notifier.send_message.call_args[0][0]
    assert "XEXT" in message
    assert "Stop-Loss" in message

    key = next(iter(state["notified_exits"]))
    assert "EXIT_APPLIED" in state["notified_exits"][key]

    # Marker must survive the normalization used by load_state.
    normalized = _normalize_state({"notified_exits": state["notified_exits"]})
    assert "EXIT_APPLIED" in normalized["notified_exits"][key]
