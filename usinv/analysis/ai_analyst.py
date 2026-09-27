"""AI Investment Analyst module for USInv.

Generates concise, natural-language investment theses in Turkish (and English)
for stocks selected into the concentrated 5-slot portfolio.
Operates deterministically offline via rule-based templates, and optionally
uses Google Gemini API if GEMINI_API_KEY is configured.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Final

logger = logging.getLogger(__name__)

FACTOR_DISPLAY_NAMES: Final[dict[str, str]] = {
    "quality": "Buffett Kalite",
    "value": "Graham Değerleme",
    "momentum": "Momentum",
    "piotroski": "Piotroski Finansal Sağlık",
    "low_vol": "Düşük Volatilite",
    "growth": "Büyüme İvmesi",
    "technical": "Teknik Zamanlama",
}


@dataclass(frozen=True, slots=True)
class FactorChip:
    factor: str
    label: str
    score_pct: float


def compute_factor_chips(
    factor_ranks: dict[str, float | None],
    top_n: int = 3,
) -> list[FactorChip]:
    """Extract and sort top-N normalized factors for UI badges."""
    scored: list[tuple[float, str, str]] = []
    for f_name, val in factor_ranks.items():
        if val is None:
            continue
        pct = round(float(val) * 100.0, 1) if val <= 1.0 else round(float(val), 1)
        label = FACTOR_DISPLAY_NAMES.get(f_name, f_name.replace("_", " ").title())
        scored.append((pct, f_name, label))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        FactorChip(factor=f_name, label=label, score_pct=score)
        for score, f_name, label in scored[:top_n]
    ]


class AiAnalyst:
    """Natural language investment thesis generator."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "").strip() or None

    def generate_thesis(
        self,
        ticker: str,
        company_name: str,
        *,
        sector: str,
        composite_score: float,
        entry_price: float,
        target_price: float,
        stop_price: float,
        factor_ranks: dict[str, float | None],
        is_incumbent: bool = False,
        piotroski_score: int | None = None,
    ) -> str:
        """Generate a 2-sentence Turkish investment rationale for a pick."""
        # Check if Gemini can be called (only if key is provided and library is available)
        if self.api_key:
            try:
                gemini_result = self._call_gemini(
                    ticker=ticker,
                    company_name=company_name,
                    sector=sector,
                    composite_score=composite_score,
                    entry_price=entry_price,
                    target_price=target_price,
                    stop_price=stop_price,
                    factor_ranks=factor_ranks,
                    is_incumbent=is_incumbent,
                )
                if gemini_result:
                    return gemini_result
            except Exception as exc:
                logger.debug("Gemini AI analyst call skipped/failed: %s; using rule-based", exc)

        # Deterministic rule-based template generation
        return self._generate_rule_based(
            ticker=ticker,
            company_name=company_name,
            sector=sector,
            composite_score=composite_score,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            factor_ranks=factor_ranks,
            is_incumbent=is_incumbent,
            piotroski_score=piotroski_score,
        )

    def _generate_rule_based(
        self,
        ticker: str,
        company_name: str,
        *,
        sector: str,
        composite_score: float,
        entry_price: float,
        target_price: float,
        stop_price: float,
        factor_ranks: dict[str, float | None],
        is_incumbent: bool,
        piotroski_score: int | None,
    ) -> str:
        chips = compute_factor_chips(factor_ranks, top_n=2)
        top_factor_str = (
            f"{chips[0].label} (%{chips[0].score_pct:.0f})" if chips else "Yüksek model skoru"
        )
        second_factor_str = (
            f" ve {chips[1].label.lower()} (%{chips[1].score_pct:.0f})" if len(chips) > 1 else ""
        )

        upside_pct = (
            ((target_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 15.0
        )
        stop_pct = (
            abs(((stop_price - entry_price) / entry_price) * 100.0) if entry_price > 0 else 18.0
        )

        if is_incumbent:
            return (
                f"{company_name} ({ticker}), {top_factor_str}{second_factor_str} ile güçlü "
                f"finansal konumunu sürdürdüğü için hold-band tampon kuralıyla portföyde tutulmaya "
                f"devam ediyor. ${target_price:.2f} hedefi (%{upside_pct:.1f} yukarı) geçerli "
                f"olup, koruyucu stop seviyesi ${stop_price:.2f} (-%{stop_pct:.1f}) kilitlenmiştir."
            )

        # New entry
        q_score = factor_ranks.get("quality", 0.5) or 0.5
        v_score = factor_ranks.get("value", 0.5) or 0.5
        m_score = factor_ranks.get("momentum", 0.5) or 0.5

        if m_score >= 0.70 and q_score >= 0.60:
            return (
                f"{company_name} ({ticker}), {top_factor_str}{second_factor_str} bileşimiyle "
                f"yüksek inançlı 5'li portföye girdi. ${target_price:.2f} hedefinde "
                f"%{upside_pct:.1f} prim, dinamik stop ${stop_price:.2f} olarak belirlendi."
            )

        if v_score >= 0.65:
            return (
                f"{company_name} ({ticker}), cazip Graham değerleme çarpanları ve "
                f"{top_factor_str} ile güçlü bir güvenlik marjı (MoS) sunduğu için seçildi. "
                f"${target_price:.2f} hedef fiyat (%{upside_pct:.1f} prim) öngörülürken, "
                f"sermaye koruması için ${stop_price:.2f} stop seviyesi belirlendi."
            )

        return (
            f"{company_name} ({ticker}), {top_factor_str}{second_factor_str} desteğiyle "
            f"model sıralamasında üst lige yerleşerek portföy slotunu kazandı. "
            f"Beklenen hedef fiyat ${target_price:.2f} (%{upside_pct:.1f} prim), dinamik zarar kes "
            f"seviyesi ise ${stop_price:.2f} olarak tanımlanmıştır."
        )

    def _call_gemini(self, **kwargs: Any) -> str | None:
        """Call Gemini API when available."""
        try:
            from google import genai

            client = genai.Client(api_key=self.api_key)
            prompt = (
                f"Aşağıdaki ABD hissesi için 5 hisselik konsantre BIST Picker tarzı portföy "
                f"için 2 cümlelik sade, ikna edici ve profesyonel Türkçe yatırım tezi yaz:\n"
                f"Şirket: {kwargs.get('company_name')} ({kwargs.get('ticker')})\n"
                f"Sektör: {kwargs.get('sector')}\n"
                f"Giriş Fiyatı: ${kwargs.get('entry_price')}\n"
                f"Hedef Fiyat: ${kwargs.get('target_price')}\n"
                f"Stop Fiyat: ${kwargs.get('stop_price')}\n"
                f"Faktör Puanları: {kwargs.get('factor_ranks')}\n"
                f"Mevcut Pozisyon mu (Incumbent): {kwargs.get('is_incumbent')}\n"
                f"Sadece 2 cümle yaz, doğrudan Türkçe yatırımcıya hitap et."
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            if response and response.text:
                return response.text.strip()
        except Exception:
            pass
        return None
