"""Portfolio Alert Monitoring Service.

Fetches open positions, monitors live prices against stop-loss/take-profit targets,
scrapes KAP disclosures for open portfolio companies, and triggers Telegram alerts.

Rules — when NOT to send a message:
- Stop/TP not triggered → no message
- KAP already processed → no message
- KAP text empty → mark processed, no message (won't retry every hour)
- KAP sentiment NEUTRAL → mark processed, no message
- KAP summary empty/whitespace → mark processed, no message
- Weekly report: all three action lists empty (no AL / SAT / TUT) → no message
- Weekly report: already sent today → no message
"""

import json
import logging
import os
from datetime import datetime, date
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml
import yfinance as yf

from us_picker.read_service import get_open_positions
from us_picker.notifications.telegram import TelegramNotifier
from us_picker.notifications.event_analyzer import EventAnalyzer
from us_picker.data.sources.kap_feed import KAPFeed

logger = logging.getLogger("us_picker.notifications.monitor_alerts")

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "alerts_state.json"
_MAX_PROCESSED_KAP_IDS = 2000
_MAX_NOTIFIED_EXIT_KEYS = 250


def _default_state() -> dict:
    return {
        "processed_kap_ids": [],
        "notified_exits": {},
        "last_weekly_report_date": "",
    }


def _normalize_state(raw_state: object) -> dict:
    """Return a bounded, backwards-compatible alert state."""
    state = raw_state if isinstance(raw_state, dict) else {}

    processed = state.get("processed_kap_ids", [])
    if not isinstance(processed, list):
        processed = []
    processed = list(dict.fromkeys(str(item) for item in processed if item))[
        -_MAX_PROCESSED_KAP_IDS:
    ]

    notified = state.get("notified_exits", {})
    if not isinstance(notified, dict):
        notified = {}
    normalized_notified: dict[str, list[str]] = {}
    for key, values in list(notified.items())[-_MAX_NOTIFIED_EXIT_KEYS:]:
        if not isinstance(values, list):
            continue
        normalized_values = [str(value) for value in values]
        normalized_notified[str(key)] = list(
            dict.fromkeys(
                value
                for value in normalized_values
                if value in {"STOP_LOSS", "TAKE_PROFIT", "EXIT_APPLIED"}
            )
        )

    weekly_date = state.get("last_weekly_report_date", "")
    return {
        "processed_kap_ids": processed,
        "notified_exits": normalized_notified,
        "last_weekly_report_date": weekly_date if isinstance(weekly_date, str) else "",
    }


def _html(value: object, max_chars: int = 2000) -> str:
    text = str(value or "").strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return escape(text, quote=True)


def _safe_http_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return escape(url, quote=True)


def _mark_kap_processed(state: dict, disclosure_id: object) -> None:
    disclosure_id = str(disclosure_id or "").strip()
    if not disclosure_id:
        return
    processed = state["processed_kap_ids"]
    if disclosure_id not in processed:
        processed.append(disclosure_id)
    if len(processed) > _MAX_PROCESSED_KAP_IDS:
        del processed[:-_MAX_PROCESSED_KAP_IDS]


def load_settings() -> dict:
    """Load settings.yaml configuration."""
    if _SETTINGS_PATH.exists():
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def load_state() -> dict:
    """Load the alert state from file."""
    if _STATE_PATH.exists():
        try:
            return _normalize_state(json.loads(_STATE_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("Failed to load alerts state: %s. Reinitializing.", e)

    return _default_state()


def save_state(state: dict) -> None:
    """Atomically save a bounded alert state to file."""
    temp_path = _STATE_PATH.with_suffix(_STATE_PATH.suffix + ".tmp")
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        normalized = _normalize_state(state)
        temp_path.write_text(
            json.dumps(normalized, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_path.replace(_STATE_PATH)
    except Exception as e:
        logger.error("Failed to save alerts state: %s", e)
        temp_path.unlink(missing_ok=True)
        raise


def send_weekly_report_if_monday(
    notifier: TelegramNotifier, state: dict, positions: list
) -> None:
    """Send the rotation-day summary — only on rotation Mondays.

    Cadence comes from ``selection.rotation_weeks`` (bi-weekly since
    2026-07-04); on non-rotation Mondays there are no fresh AL/SAT decisions
    to report, so the full report would just repeat stale actions.
    """
    from us_picker.portfolio.rotation import (
        load_rotation_config,
        rotation_cycle_start,
    )

    today = date.today()
    rotation_weeks, rotation_anchor = load_rotation_config()
    if today != rotation_cycle_start(today, rotation_weeks, rotation_anchor):
        return

    today_str = today.isoformat()
    if state.get("last_weekly_report_date") == today_str:
        logger.info("Monday weekly report already sent today. Skipping.")
        return

    logger.info("Preparing Monday morning portfolio report...")

    # 1. Active Portfolio (Ana Portföy)
    open_lines = []
    for pos in positions:
        ticker = _html(pos["ticker"], 30)
        entry = pos.get("entry_price") or 0.0
        current = pos.get("current_price") or entry  # fallback to entry if no live price
        pnl = pos.get("pnl_pct")

        if pnl is not None:
            pnl_str = f"+{pnl:.1f}%" if pnl >= 0 else f"{pnl:.1f}%"
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_part = f"({pnl_emoji} {pnl_str})"
        else:
            pnl_part = ""

        open_lines.append(
            f"• <b>{ticker}</b>: Giriş: {entry:.2f} TL | Güncel: {current:.2f} TL {pnl_part}"
        )

    # 2. Rebalance Actions (Zeki İşlemler)
    buy_tickers: list[str] = []
    sell_tickers: list[str] = []
    hold_tickers: list[str] = []
    rebalance_error: Optional[str] = None

    try:
        from us_picker.db.connection import get_session
        from us_picker.db.schema import PortfolioSelection, Company

        session = get_session()
        try:
            latest_date_row = (
                session.query(PortfolioSelection.selection_date)
                .filter(PortfolioSelection.portfolio == "ALPHA")
                .order_by(PortfolioSelection.selection_date.desc())
                .first()
            )
            if latest_date_row:
                latest_date = latest_date_row[0]

                new_rows = (
                    session.query(Company.ticker)
                    .join(PortfolioSelection, Company.id == PortfolioSelection.company_id)
                    .filter(
                        PortfolioSelection.portfolio == "ALPHA",
                        PortfolioSelection.selection_date == latest_date,
                        PortfolioSelection.exit_date.is_(None),
                    )
                    .all()
                )
                new_tickers = {r[0] for r in new_rows}

                exited_rows = (
                    session.query(Company.ticker)
                    .join(PortfolioSelection, Company.id == PortfolioSelection.company_id)
                    .filter(
                        PortfolioSelection.portfolio == "ALPHA",
                        PortfolioSelection.exit_date == latest_date,
                    )
                    .all()
                )
                exited_tickers = {r[0] for r in exited_rows}

                hold_tickers = sorted(new_tickers.intersection(exited_tickers))
                sell_tickers = sorted(exited_tickers - new_tickers)
                buy_tickers = sorted(new_tickers - exited_tickers)
        finally:
            session.close()
    except Exception as e:
        logger.error("Failed to load rebalance actions for weekly report: %s", e)
        rebalance_error = str(e)

    # Guard: if there are no open positions AND no rebalance actions, nothing to say
    has_positions = bool(open_lines)
    has_rebalance = bool(buy_tickers or sell_tickers or hold_tickers)
    if not has_positions and not has_rebalance:
        logger.info("Monday report skipped — no open positions and no rebalance actions.")
        state["last_weekly_report_date"] = today_str  # Don't retry today
        return

    # Build rebalance section
    rec_lines: list[str] = []
    if rebalance_error:
        rec_lines.append("<i>Zeki işlemler hesaplanırken hata oluştu.</i>")
    elif not has_rebalance:
        rec_lines.append("<i>Bu rotasyonda portföyde değişiklik yok.</i>")
    else:
        if buy_tickers:
            rec_lines.append(f"🟢 <b>AL:</b> {_html(', '.join(buy_tickers), 500)}")
        if sell_tickers:
            rec_lines.append(f"🔴 <b>SAT:</b> {_html(', '.join(sell_tickers), 500)}")
        if hold_tickers:
            rec_lines.append(f"🔵 <b>TUT:</b> {_html(', '.join(hold_tickers), 500)}")

    portfolio_section = (
        "\n".join(open_lines)
        if open_lines
        else "<i>Aktif açık pozisyon bulunmamaktadır.</i>"
    )

    cadence_str = (
        f"{rotation_weeks} haftada bir" if rotation_weeks > 1 else "her hafta"
    )
    message = (
        f"📅 <b>Rotasyon Günü — Portföy ve Rebalans Raporu</b>\n"
        f"<i>Tarih: {today.strftime('%d.%m.%Y')} · Rotasyon: {cadence_str}</i>\n\n"
        f"💼 <b>ANA PORTFÖY:</b>\n"
        f"{portfolio_section}\n\n"
        f"🧠 <b>ZEKİ İŞLEMLER (Rotasyon Kararları):</b>\n"
        + "\n".join(rec_lines)
        + "\n\n"
        f"⏳ <i>Bugün aldığın hisseleri bir sonraki rotasyona kadar "
        f"({rotation_weeks} hafta) tut. Ara dönemde yalnızca stop-loss/hedef "
        f"uyarısı gelirse sat.</i>\n\n"
        "📈 <i>Hayırlı ve bol kazançlı haftalar dileriz!</i>"
    )

    success = notifier.send_message(message)
    if success:
        state["last_weekly_report_date"] = today_str
        logger.info("Monday morning weekly portfolio report sent successfully.")


def _notify_applied_exits(notifier: TelegramNotifier, state: dict) -> None:
    """Alert on positions the pipeline auto-closed (stop/target/thesis).

    Since 2026-07-02 the cron pipeline applies exit rules to the database
    (check-exits --apply). A closed position disappears from the open list,
    so the live-price monitor above never fires for it — without this pass
    the user would get NO Telegram message for an executed exit and their
    broker portfolio would silently diverge from the model.
    """
    from datetime import timedelta

    from us_picker.db.connection import get_session
    from us_picker.db.schema import Company, PortfolioSelection

    reason_labels = {
        "STOP_LOSS": ("🚨", "Zarar Kes (Stop-Loss)"),
        "TARGET": ("🎯", "Hedef Fiyat"),
        "THESIS_BREAKER": ("⚠️", "İçeriden Satış Uyarısı"),
    }

    session = get_session()
    try:
        cutoff = date.today() - timedelta(days=5)
        exited = (
            session.query(PortfolioSelection, Company.ticker)
            .join(Company, Company.id == PortfolioSelection.company_id)
            .filter(
                PortfolioSelection.portfolio == "ALPHA",
                PortfolioSelection.exit_reason.in_(list(reason_labels)),
                PortfolioSelection.exit_date >= cutoff,
            )
            .all()
        )
    finally:
        session.close()

    for selection, ticker in exited:
        sel_date = selection.selection_date
        sel_date_str = (
            sel_date.isoformat()
            if isinstance(sel_date, (date, datetime))
            else str(sel_date)
        )
        pos_key = f"{ticker}_{sel_date_str}"
        markers = state["notified_exits"].setdefault(pos_key, [])
        if "EXIT_APPLIED" in markers:
            continue

        emoji, label = reason_labels[selection.exit_reason]
        exit_price = selection.exit_price
        entry_price = selection.entry_price
        ret_pct = selection.return_pct
        safe_ticker = _html(str(ticker), 30)
        exit_str = f"{exit_price:.2f} TL" if exit_price is not None else "—"
        entry_str = f"{entry_price:.2f} TL" if entry_price is not None else "—"
        ret_str = f"{ret_pct:+.1f}%" if ret_pct is not None else "—"

        message = (
            f"{emoji} <b>Model Pozisyonu Kapattı: {safe_ticker}</b>\n\n"
            f"📋 <b>Sebep:</b> {label}\n"
            f"💰 <b>Giriş:</b> {entry_str} → <b>Çıkış:</b> {exit_str} ({ret_str})\n\n"
            f"👉 <i>Elinde varsa aynı gün sat. Boşalan yer bir sonraki "
            f"rotasyon pazartesisine kadar nakitte kalır.</i>"
        )
        if notifier.send_message(message):
            markers.append("EXIT_APPLIED")
            logger.info("Applied-exit alert sent for %s (%s).", ticker, selection.exit_reason)


def monitor_portfolio() -> None:
    """Check positions and disclosures, and send Telegram alerts only when necessary."""
    logger.info("Starting portfolio monitoring cycle...")

    # 1. Load configuration and initialize notifier
    settings = load_settings()
    tg_config = settings.get("telegram", {})

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", tg_config.get("bot_token", "")).strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", tg_config.get("chat_id", "")).strip()
    enabled = tg_config.get("enabled", True)

    if not bot_token or not chat_id:
        logger.warning("Telegram credentials missing — running in dry-run mode.")
        notifier = TelegramNotifier(bot_token="", chat_id="", enabled=False)
    else:
        notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id, enabled=enabled)

    analyzer = EventAnalyzer()
    kap_feed = KAPFeed()
    state = load_state()

    # 2. Load open positions
    try:
        open_pos_df = get_open_positions()
    except Exception as e:
        logger.error("Failed to load open positions from database: %s", e)
        raise

    if open_pos_df.empty:
        logger.info("No open positions; skipping price checks and KAP disclosure monitoring.")
        positions = []
    else:
        positions = open_pos_df.to_dict(orient="records")
    logger.info("Loaded %d open positions to monitor.", len(positions))

    # 2b. Alert on exits the pipeline already applied to the DB — these are
    # no longer "open", so the live-price loop below cannot catch them.
    try:
        _notify_applied_exits(notifier, state)
    except Exception as exc:
        logger.error("Applied-exit notification pass failed: %s", exc)

    # 3. Check live prices — Stop-loss / Take-profit
    for pos in positions:
        ticker = pos["ticker"]
        selection_date = pos["selection_date"]
        sel_date_str = (
            selection_date.isoformat()
            if isinstance(selection_date, (date, datetime))
            else str(selection_date)
        )
        pos_key = f"{ticker}_{sel_date_str}"

        stop_loss = pos.get("stop_loss_price")
        target_price = pos.get("target_price")
        entry_price = pos.get("entry_price")
        reason_factors = pos.get("reason_top_factors_json")

        if pos_key not in state["notified_exits"]:
            state["notified_exits"][pos_key] = []

        # Skip if both targets are missing — nothing to check
        if stop_loss is None and target_price is None:
            logger.info("%s has no stop-loss or take-profit targets set. Skipping price check.", ticker)
            continue

        # Fetch live price from Yahoo Finance
        try:
            t = yf.Ticker(f"{ticker}.IS")
            hist = t.history(period="5d")
            if hist.empty:
                logger.warning("No price history returned for %s.IS", ticker)
                continue
            current_price = float(hist["Close"].iloc[-1])
            logger.info("Current price for %s: %.2f TL", ticker, current_price)
        except Exception as e:
            logger.error("Failed to fetch live price for %s: %s", ticker, e)
            continue

        # --- Stop-Loss check ---
        if (
            stop_loss is not None
            and current_price <= stop_loss
            and "STOP_LOSS" not in state["notified_exits"][pos_key]
        ):
            logger.warning(
                "Stop-loss triggered for %s! Live: %.2f, Limit: %.2f",
                ticker, current_price, stop_loss,
            )
            alert_text = analyzer.generate_exit_alert(
                ticker=ticker,
                trigger_type="STOP_LOSS",
                price=current_price,
                limit_price=stop_loss,
                reason_top_factors=reason_factors,
            )
            if not alert_text or not alert_text.strip():
                alert_text = f"{ticker} zarar kes limitine ulaştı."

            safe_ticker = _html(ticker, 30)
            safe_alert_text = _html(alert_text, 1500)
            entry_str = f"{entry_price:.2f} TL" if entry_price is not None else "—"
            message = (
                f"🚨 <b>Zarar Kes (Stop-Loss) Tetiklendi: {safe_ticker}</b>\n\n"
                f"💰 <b>Giriş Fiyatı:</b> {entry_str}\n"
                f"📉 <b>Güncel Fiyat:</b> {current_price:.2f} TL\n"
                f"🛑 <b>Stop-Loss Limiti:</b> {stop_loss:.2f} TL\n\n"
                f"🤖 <b>Yapay Zeka Analizi:</b>\n"
                f"<i>{safe_alert_text}</i>"
            )
            if notifier.send_message(message):
                state["notified_exits"][pos_key].append("STOP_LOSS")

        # --- Take-Profit check ---
        if (
            target_price is not None
            and current_price >= target_price
            and "TAKE_PROFIT" not in state["notified_exits"][pos_key]
        ):
            logger.info(
                "Take-profit triggered for %s! Live: %.2f, Target: %.2f",
                ticker, current_price, target_price,
            )
            alert_text = analyzer.generate_exit_alert(
                ticker=ticker,
                trigger_type="TAKE_PROFIT",
                price=current_price,
                limit_price=target_price,
                reason_top_factors=reason_factors,
            )
            if not alert_text or not alert_text.strip():
                alert_text = f"{ticker} kar al hedefine ulaştı."

            safe_ticker = _html(ticker, 30)
            safe_alert_text = _html(alert_text, 1500)
            entry_str = f"{entry_price:.2f} TL" if entry_price is not None else "—"
            message = (
                f"🟢 <b>Kar Al (Take-Profit) Tetiklendi: {safe_ticker}</b>\n\n"
                f"💰 <b>Giriş Fiyatı:</b> {entry_str}\n"
                f"📈 <b>Güncel Fiyat:</b> {current_price:.2f} TL\n"
                f"🎯 <b>Kar Al Limiti:</b> {target_price:.2f} TL\n\n"
                f"🤖 <b>Yapay Zeka Analizi:</b>\n"
                f"<i>{safe_alert_text}</i>"
            )
            if notifier.send_message(message):
                state["notified_exits"][pos_key].append("TAKE_PROFIT")

    # 4. KAP Disclosures — only open portfolio positions.
    open_tickers = [pos["ticker"] for pos in positions]
    tickers_to_monitor = sorted(set(open_tickers))
    logger.info(
        "Checking KAP disclosures for %d tickers: %s",
        len(tickers_to_monitor), tickers_to_monitor,
    )

    for ticker in tickers_to_monitor:
        try:
            disclosures = kap_feed.get_latest_disclosures(ticker, limit=2)
        except Exception as exc:
            logger.error("KAP disclosure fetch crashed for %s: %s", ticker, exc)
            continue
        logger.info("Fetched %d recent disclosures for %s.", len(disclosures), ticker)

        for disc in disclosures:
            if not isinstance(disc, dict):
                logger.warning("Ignoring malformed KAP disclosure for %s: %r", ticker, disc)
                continue
            disc_id = str(disc.get("id") or "").strip()
            if not disc_id:
                logger.warning("Ignoring KAP disclosure without an ID for %s.", ticker)
                continue

            # Already processed in a previous run — skip entirely
            if disc_id in state["processed_kap_ids"]:
                continue

            title = str(disc.get("title") or "Başlıksız KAP bildirimi").strip()
            logger.info("Analyzing KAP disclosure for %s: %s (ID: %s)", ticker, title, disc_id)

            # Fetch full text
            text_content = kap_feed.get_disclosure_text(disc_id)
            if not text_content or not text_content.strip():
                # Mark processed so we don't retry every hour — won't ever have content
                logger.warning(
                    "Empty content for disclosure %s (%s). Marking processed, skipping.",
                    disc_id, ticker,
                )
                _mark_kap_processed(state, disc_id)
                continue

            # Analyze with Gemini
            analysis = analyzer.analyze_kap_event(ticker, title, text_content)
            if not isinstance(analysis, dict):
                logger.warning("Malformed KAP analysis for %s/%s; treating as NEUTRAL.", ticker, disc_id)
                analysis = {}
            sentiment = str(analysis.get("sentiment", "NEUTRAL")).upper()
            if sentiment not in {"POSITIVE", "NEGATIVE", "NEUTRAL"}:
                sentiment = "NEUTRAL"
            summary = str(analysis.get("summary") or "").strip()

            # NEUTRAL → mark processed, no alert
            if sentiment == "NEUTRAL":
                logger.info(
                    "NEUTRAL KAP for %s ('%s') — marking processed, no alert.", ticker, title
                )
                _mark_kap_processed(state, disc_id)
                continue

            # Empty summary → mark processed, no alert (avoid sending blank message)
            if not summary:
                logger.warning(
                    "Empty summary for %s KAP '%s' — marking processed, skipping.", ticker, title
                )
                _mark_kap_processed(state, disc_id)
                continue

            sentiment_emoji = "🟢" if sentiment == "POSITIVE" else "🔴"
            safe_ticker = _html(ticker, 30)
            safe_title = _html(title, 500)
            safe_summary = _html(summary, 2200)
            safe_url = _safe_http_url(disc.get("url"))
            detail_line = (
                f"\n\n🔗 <a href=\"{safe_url}\">KAP Bildirim Detayı</a>"
                if safe_url
                else ""
            )
            message = (
                f"📢 <b>Yeni KAP Açıklaması: {safe_ticker}</b>\n"
                f"📝 <b>Konu:</b> {safe_title}\n"
                f"⚖️ <b>Etki Derecesi:</b> {sentiment_emoji} {sentiment}\n\n"
                f"🤖 <b>Yapay Zeka Özeti:</b>\n"
                f"<i>{safe_summary}</i>"
                f"{detail_line}"
            )
            if notifier.send_message(message):
                _mark_kap_processed(state, disc_id)

    # 5. Monday morning weekly report
    try:
        send_weekly_report_if_monday(notifier, state, positions)
    except Exception as e:
        logger.error("Failed to execute Monday weekly report: %s", e)

    # 6. Persist state
    save_state(state)
    logger.info("Portfolio monitoring cycle completed successfully.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    monitor_portfolio()
