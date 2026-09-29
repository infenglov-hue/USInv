"""AI Analyst module for BIST Stock Picker.

Generates natural language insights (Turkish) explaining changes in 
stock scores and portfolio decisions using Gemini or a rule-based fallback.
"""

import os
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session
import yaml

from us_picker.db.schema import ScoringResult, Company, DailyPrice

logger = logging.getLogger(__name__)

class AiAnalyst:
    """Generates AI insights for investment decisions."""

    def __init__(self):
        self.api_key = self._load_api_key()

    def _load_api_key(self) -> Optional[str]:
        """Load Gemini API key from environment, settings.yaml, or key file."""
        # 1. Environment variable
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if key:
            return key

        # 2. settings.yaml
        settings_path = Path(__file__).resolve().parent.parent.parent / "config" / "settings.yaml"
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
        key_file = Path(__file__).resolve().parent.parent.parent.parent / "APIKEY_FOLDER" / "gemini_api_key.txt"
        if key_file.exists():
            try:
                key = key_file.read_text(encoding="utf-8").strip()
                if key:
                    return key
            except Exception as e:
                logger.debug("Failed to read key file: %s", e)

        return None

    def generate_all_insights(self, session: Session, scoring_date: date) -> int:
        """Analyze score changes and generate insights for all scored stocks."""
        # 1. Get current results
        current_results = session.query(ScoringResult).filter(
            ScoringResult.scoring_date == scoring_date
        ).all()
        
        if not current_results:
            return 0
            
        # 2. Get previous scoring date
        prev_date_row = session.query(ScoringResult.scoring_date).filter(
            ScoringResult.scoring_date < scoring_date
        ).order_by(desc(ScoringResult.scoring_date)).first()
        
        prev_date = prev_date_row[0] if prev_date_row else None
        
        # 3. Load previous results into a lookup
        prev_results = {}
        if prev_date:
            prev_results = {
                r.company_id: r for r in session.query(ScoringResult).filter(
                    ScoringResult.scoring_date == prev_date
                ).all()
            }
        
        count = 0
        for curr in current_results:
            prev = prev_results.get(curr.company_id)
            insight = self.generate_insight(session, curr, prev)
            if insight:
                curr.ai_insight = insight
                count += 1
                
        session.commit()
        logger.info("Generated %d AI insights for %s", count, scoring_date)
        return count

    def generate_insight(self, session: Session, curr: ScoringResult, prev: Optional[ScoringResult]) -> Optional[str]:
        """Generate a Turkish insight summary of the score changes using Gemini or fallback."""
        if not prev:
            return "Sisteme yeni dahil edildi, ilk analiz yapılıyor."

        # Compute differences for context
        diffs = []
        if curr.graham_score is not None and prev.graham_score is not None:
            diffs.append(f"Graham Puanı: {prev.graham_score:.1f} -> {curr.graham_score:.1f}")
        if curr.buffett_score is not None and prev.buffett_score is not None:
            diffs.append(f"Buffett Kalite Puanı: {prev.buffett_score:.1f} -> {curr.buffett_score:.1f}")
        if curr.momentum_score is not None and prev.momentum_score is not None:
            diffs.append(f"Momentum Puanı: {prev.momentum_score:.1f} -> {curr.momentum_score:.1f}")
        if curr.composite_alpha is not None and prev.composite_alpha is not None:
            diffs.append(f"Toplam Model Puanı (Alpha): {prev.composite_alpha:.1f} -> {curr.composite_alpha:.1f}")

        # Fetch Company details
        company = session.query(Company).filter(Company.id == curr.company_id).first()
        company_name = company.name if company else "Bilinmeyen Şirket"
        ticker = company.ticker if company else "UNK"
        sector = company.sector_custom or company.sector_bist or "Diğer"

        # If no API key, use rule-based fallback
        if not self.api_key:
            return self._rule_based_diff(curr, prev)

        prompt = (
            "Aşağıdaki BIST şirketi için Türkçe dilinde kısa (en fazla 2-3 cümle), "
            "profesyonel ve sade bir yatırım tezi/analiz özeti oluştur. "
            "Kullanıcıya doğrudan yatırım önerisi vermekten kaçın.\n"
            "Önemli kural: 'tavsiye', 'öneri', 'alın', 'satın', 'hedef', 'stop' kelimelerini KESİNLİKLE kullanma.\n\n"
            f"Şirket: {company_name} ({ticker})\n"
            f"Sektör: {sector}\n"
            f"Güncel Skorlar:\n"
            f"- Buffett Kalite Skoru: {curr.buffett_score if curr.buffett_score is not None else '--'}\n"
            f"- Graham Değer Skoru: {curr.graham_score if curr.graham_score is not None else '--'}\n"
            f"- DCF Emniyet Marjı: {curr.dcf_margin_of_safety_pct if curr.dcf_margin_of_safety_pct is not None else '--'}%\n"
            f"- Piotroski Mali Güç Skoru: {curr.piotroski_fscore_raw if curr.piotroski_fscore_raw is not None else '--'}/9\n"
            f"- İvme (Momentum): {curr.momentum_score if curr.momentum_score is not None else '--'}\n"
            f"- Teknik Durum: {curr.technical_score if curr.technical_score is not None else '--'}\n"
            f"Değişimler: {', '.join(diffs) if diffs else 'Önemli bir skor değişimi yok'}\n\n"
            "Özet:"
        )

        try:
            from google import genai
            client = genai.Client(api_key=self.api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            insight = response.text.strip()
            
            # Compliance check: strip or reject if forbidden words leaked
            forbidden = ["tavsiye", "öneri", "alın", "satın", "hedef", "stop"]
            for word in forbidden:
                if word in insight.lower():
                    logger.debug("Gemini response contained forbidden word '%s'. Falling back.", word)
                    return self._rule_based_diff(curr, prev)
            return insight
        except Exception as e:
            logger.warning("Gemini insight generation failed: %s. Falling back to rule-based.", e)
            return self._rule_based_diff(curr, prev)

    def _rule_based_diff(self, curr: ScoringResult, prev: Optional[ScoringResult]) -> Optional[str]:
        """Compare two snapshots and generate a Turkish summary of the change (fallback)."""
        if not prev:
            return "Sisteme yeni dahil edildi, ilk analiz yapılıyor."

        diffs = []
        
        # Graham change (Value)
        if curr.graham_score is not None and prev.graham_score is not None:
            g_diff = curr.graham_score - prev.graham_score
            if abs(g_diff) > 15:
                if g_diff < 0:
                    diffs.append("Fiyat artışı veya faiz yükselişi nedeniyle Graham iskontosu azaldı.")
                else:
                    diffs.append("Fiyat düşüşü emniyet marjını (MOS) artırdı.")

        # Buffett change (Quality)
        if curr.buffett_score is not None and prev.buffett_score is not None:
            b_diff = curr.buffett_score - prev.buffett_score
            if b_diff < -15:
                diffs.append("Finansal verilerde (ROE/Nakit Akışı) bozulma sinyali var.")
            elif b_diff > 15:
                diffs.append("Operasyonel karlılık ve kalite puanı güçlendi.")

        # Momentum change
        if curr.momentum_score is not None and prev.momentum_score is not None:
            m_diff = curr.momentum_score - prev.momentum_score
            if m_diff > 20:
                diffs.append("Piyasada hisseye olan ilgi ve yükseliş ivmesi arttı.")
            elif m_diff < -20:
                diffs.append("Hisse kısa vadeli trend desteğini kaybetti.")

        if not diffs:
            if abs((curr.composite_alpha or 0) - (prev.composite_alpha or 0)) < 5:
                return "Hisse puanı stabil, yatırım tezi korunuyor."
            return None

        return " ".join(diffs)
