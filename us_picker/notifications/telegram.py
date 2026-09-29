"""Telegram notification provider for BIST Stock Picker."""

import logging
import time

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends HTML formatted messages to a Telegram chat or group."""

    MAX_MESSAGE_LENGTH = 4096
    MAX_ATTEMPTS = 3

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.bot_token = bot_token.strip() if bot_token else ""
        self.chat_id = chat_id.strip() if chat_id else ""
        self.enabled = enabled

    def send_message(self, text: str) -> bool:
        """Send a message to the configured Telegram chat/group.

        Uses HTML formatting.
        """
        if not self.enabled:
            logger.info("Telegram notification skipped (disabled in config).")
            return False

        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram bot token or chat ID is missing. Cannot send notification.")
            return False

        text = text.strip()
        if not text:
            logger.warning("Empty Telegram notification skipped.")
            return False
        if len(text) > self.MAX_MESSAGE_LENGTH:
            logger.error(
                "Telegram notification exceeds the %d character limit (%d).",
                self.MAX_MESSAGE_LENGTH,
                len(text),
            )
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                response = requests.post(url, json=payload, timeout=15)
            except requests.RequestException as exc:
                logger.warning(
                    "Telegram request failed on attempt %d/%d: %s",
                    attempt,
                    self.MAX_ATTEMPTS,
                    exc,
                )
                if attempt < self.MAX_ATTEMPTS:
                    time.sleep(attempt)
                    continue
                return False

            try:
                response_data = response.json()
            except ValueError:
                response_data = {}

            if response.status_code == 200 and response_data.get("ok", True) is not False:
                logger.info("Telegram notification sent successfully.")
                return True

            if response.status_code == 429 and attempt < self.MAX_ATTEMPTS:
                retry_after = (
                    response_data.get("parameters", {}).get("retry_after", attempt)
                    if isinstance(response_data, dict)
                    else attempt
                )
                try:
                    wait_seconds = max(1, min(int(retry_after), 30))
                except (TypeError, ValueError):
                    wait_seconds = attempt
                logger.warning("Telegram rate limit reached; retrying in %d seconds.", wait_seconds)
                time.sleep(wait_seconds)
                continue

            if response.status_code >= 500 and attempt < self.MAX_ATTEMPTS:
                logger.warning(
                    "Telegram server error %d on attempt %d/%d; retrying.",
                    response.status_code,
                    attempt,
                    self.MAX_ATTEMPTS,
                )
                time.sleep(attempt)
                continue

            logger.error(
                "Failed to send Telegram notification. Status code: %d, Response: %s",
                response.status_code,
                response.text[:1000],
            )
            return False

        return False
