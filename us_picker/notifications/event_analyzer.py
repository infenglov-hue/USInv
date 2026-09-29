"""Event Analyzer module for BIST Stock Picker.

Analyzes portfolio events (KAP news, price triggers) using the Gemini API.
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional
import yaml
from google import genai

logger = logging.getLogger(__name__)

class EventAnalyzer:
    """Analyzes portfolio events (KAP news, price triggers) using Gemini API."""

    def __init__(self):
        self.api_key = self._load_api_key()

    def _load_api_key(self) -> Optional[str]:
        """Load Gemini API key from environment, settings.yaml, or key file."""
        # 1. Environment variable
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if key:
            return key

        # 2. settings.yaml
        settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        if settings_path.exists():
            try:
                with open(settings_path, encoding="utf-8") as f:
                    settings = yaml.safe_load(f)
                key = (settings.get("gemini", {}).get("api_key", "") or "").strip()
                if key:
                    return key
            except Exception as e:
                logger.debug("Failed to read settings.yaml for gemini key: %s", e)

        # 3. APIKEY_FOLDER/gemini_api_key.txt
        key_file = Path(__file__).resolve().parent.parent.parent / "APIKEY_FOLDER" / "gemini_api_key.txt"
        if key_file.exists():
            try:
                key = key_file.read_text(encoding="utf-8").strip()
                if key:
                    return key
            except Exception as e:
                logger.debug("Failed to read key file: %s", e)

        return None

    def analyze_kap_event(self, ticker: str, title: str, content: str) -> dict:
        """Analyze a KAP disclosure and return sentiment and Turkish summary.

        Returns:
            Dict: {'sentiment': 'POSITIVE'/'NEGATIVE'/'NEUTRAL', 'summary': str}
        """
        if not self.api_key:
            logger.warning("No Gemini API key found. Using fallback analysis.")
            return {
                'sentiment': 'NEUTRAL',
                'summary': f"{ticker} için KAP bildirimi yayınlandı: {title}"
            }

        prompt = (
            "Aşağıda bir BIST şirketi için yayınlanan KAP (Kamuyu Aydınlatma Platformu) bildirimi yer almaktadır.\n"
            "Bu bildirimi analiz et ve iki şey üret:\n"
            "1. Sentiment (Etki): POSITIVE, NEGATIVE veya NEUTRAL (Sadece bu üçünden biri).\n"
            "2. Özet: Bildirimin şirket finansalları ve geleceğine etkisini açıklayan, "
            "maksimum 2-3 cümlelik, Türkçe, profesyonel ve sade bir özet.\n\n"
            f"Şirket: {ticker}\n"
            f"Başlık: {title}\n"
            f"İçerik:\n{content[:4000]}\n\n"
            "Format:\n"
            "SENTIMENT: [Buraya sadece POSITIVE, NEGATIVE veya NEUTRAL yaz]\n"
            "SUMMARY: [Buraya Türkçe özeti yaz]"
        )

        try:
            client = genai.Client(api_key=self.api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            text = response.text.strip()

            sentiment = "NEUTRAL"
            summary = f"{ticker} için KAP bildirimi: {title}"

            # Parse response
            sentiment_match = re.search(r'SENTIMENT:\s*(POSITIVE|NEGATIVE|NEUTRAL)', text, re.IGNORECASE)
            if sentiment_match:
                sentiment = sentiment_match.group(1).upper()

            summary_match = re.search(r'SUMMARY:\s*(.*)', text, re.DOTALL | re.IGNORECASE)
            if summary_match:
                summary = summary_match.group(1).strip()
            else:
                # Fallback if structure is slightly different
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                for line in lines:
                    if not line.startswith("SENTIMENT:") and not line.startswith("SUMMARY:"):
                        summary = line
                        break

            return {'sentiment': sentiment, 'summary': summary}
        except Exception as e:
            logger.error("Gemini KAP analysis failed: %s", e)
            return {
                'sentiment': 'NEUTRAL',
                'summary': f"{ticker} için KAP bildirimi: {title}"
            }

    def generate_exit_alert(self, ticker: str, trigger_type: str, price: float, limit_price: float, reason_top_factors: Optional[str] = None) -> str:
        """Generate a Turkish alert message when a stock hits stop-loss or target price.

        Args:
            ticker: Stock ticker.
            trigger_type: 'STOP_LOSS' or 'TAKE_PROFIT'.
            price: Current live price.
            limit_price: Trigger price limit.
            reason_top_factors: JSON string of top selection factors.

        Returns:
            Turkish alert message text.
        """
        if not self.api_key:
            action = "Stop-Loss (Zarar Durdur)" if trigger_type == 'STOP_LOSS' else "Take-Profit (Kar Al)"
            return f"🚨 {ticker} için {action} tetiklendi! Güncel Fiyat: {price:.2f} TL (Limit: {limit_price:.2f} TL)."

        action_desc = "zarar kes (stop-loss) seviyesine" if trigger_type == 'STOP_LOSS' else "kar al (take-profit) seviyesine"
        action_name = "Stop-Loss" if trigger_type == 'STOP_LOSS' else "Take-Profit"

        prompt = (
            f"Aşağıdaki BIST hissesi {action_desc} ulaştı ve portföyden çıkış sinyali verdi.\n"
            "Bu durumla ilgili kısa (en fazla 2-3 cümle), profesyonel, sade ve Türkçe bir bildirim metni yaz.\n"
            "Metinde yatırımcı psikolojisini rahatlatacak rasyonel ve disiplinli bir üslup kullan.\n\n"
            f"Hisse: {ticker}\n"
            f"Tetikleyici: {action_name}\n"
            f"Güncel Fiyat: {price:.2f} TL\n"
            f"Tetikleme Limiti: {limit_price:.2f} TL\n"
            f"Hisse Seçim Nedenleri: {reason_top_factors or 'Bilinmiyor'}\n\n"
            "Açıklama:"
        )

        try:
            client = genai.Client(api_key=self.api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            return response.text.strip()
        except Exception as e:
            logger.error("Gemini exit alert generation failed: %s", e)
            action = "Stop-Loss (Zarar Durdur)" if trigger_type == 'STOP_LOSS' else "Take-Profit (Kar Al)"
            return f"🚨 {ticker} için {action} tetiklendi! Güncel Fiyat: {price:.2f} TL (Limit: {limit_price:.2f} TL)."
