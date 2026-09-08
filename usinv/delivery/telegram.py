"""Telegram notifications for unattended daily decision, execution and health alerts.

Conforms to OPS_SPEC §5: rotation summaries, exit alerts, and red health lines.
Safe without credentials: if token/chat_id is absent, logs warning and returns cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date
from typing import Any

from usinv.delivery.snapshot import DeliverySnapshot

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Telegram notification client for pipeline events."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.timeout_seconds = timeout_seconds

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send_message(self, text: str, *, parse_mode: str = "Markdown") -> bool:
        """Send a formatted text message to the configured Telegram chat."""
        if not self.is_configured:
            logger.info(
                "Telegram not configured (missing token/chat_id); message suppressed:\n%s", text
            )
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return bool(result.get("ok"))
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.error("Failed to deliver Telegram notification: %s", exc)
            return False

    def notify_decision(self, snapshot: DeliverySnapshot) -> bool:
        """Send daily decision summary including regime, cash, and pending orders."""
        order_lines = []
        for o in snapshot.orders:
            order_lines.append(
                f"  • *{o.side.upper()}* {o.quantity:.0f} {o.ticker} "
                f"@ limit ${o.limit_price:.2f} ({o.reason})"
            )
        orders_block = (
            "\n".join(order_lines) if order_lines else "  _No orders scheduled for this session._"
        )

        equity_target = snapshot.macro_regime.equity_exposure_target
        msg = (
            f"🏛 *USInv Decision Pipeline — {snapshot.as_of_session}*\n\n"
            f"*Portfolio Summary:*\n"
            f"• NAV: `${snapshot.nav:,.2f}`\n"
            f"• Cash: `${snapshot.cash:,.2f}`\n"
            f"• Holdings: `{len(snapshot.positions)} positions`\n"
            f"• Regime: `{snapshot.macro_regime.regime}` (Target Equity: {equity_target:.0%})\n\n"
            f"*Orders Scheduled (LOO Cutoff 09:28 ET):*\n"
            f"{orders_block}\n\n"
            f"*Data Health:* `{snapshot.data_health.status}` "
            f"(Core Cov: {snapshot.data_health.core_coverage_pct:.1f}%)\n"
            f"Config: `{snapshot.config_hash[:10]}...` | SHA: `{snapshot.code_sha[:8]}`"
        )
        return self.send_message(msg)

    def notify_fill_reconciliation(
        self,
        session: date,
        filled_orders: list[dict[str, Any]],
        unfilled_orders: list[dict[str, Any]],
        new_nav: float,
    ) -> bool:
        """Send fill reconciliation summary following market open."""
        filled_lines = [
            f"  • {f['side'].upper()} {f['quantity']:.0f} {f['ticker']} filled @ ${f['price']:.2f}"
            for f in filled_orders
        ]
        filled_block = "\n".join(filled_lines) if filled_lines else "  _No orders filled._"

        unfilled_lines = [
            f"  • {u['side'].upper()} {u['quantity']:.0f} {u['ticker']}: "
            f"{u.get('reason', 'unfilled')}"
            for u in unfilled_orders
        ]
        unfilled_block = "\n".join(unfilled_lines) if unfilled_lines else "  _None._"

        msg = (
            f"⚖️ *USInv Fill Reconciliation — {session.isoformat()}*\n\n"
            f"*Executed Fills:*\n"
            f"{filled_block}\n\n"
            f"*Unfilled / Rejected:*\n"
            f"{unfilled_block}\n\n"
            f"*Updated Portfolio NAV:* `${new_nav:,.2f}`\n"
            f"Ledger committed and delivery snapshot updated."
        )
        return self.send_message(msg)

    def notify_alarm(self, title: str, details: str) -> bool:
        """Send immediate red alert for pipeline failures or freshness/coverage violations."""
        msg = (
            f"🚨 *USInv ALARM — {title}* 🚨\n\n"
            f"*{details}*\n\n"
            f"_Automated pipeline stopped; fails closed per blueprint protocol._"
        )
        return self.send_message(msg)
