"""Investment dossier and report generator for human-readable recommendations."""

import json
from pathlib import Path
from ai.core.models import InvestmentRecommendation, MacroRegime


class ReportGenerator:
    """Generates detailed Markdown and JSON reports for recommended funds and ETFs."""

    def __init__(self, output_dir: str | Path | None = None):
        base_dir = Path(__file__).resolve().parent.parent
        self.output_dir = Path(output_dir or (base_dir / "reports" / "output"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_dossier_markdown(
        self,
        recommendation: InvestmentRecommendation,
        macro: MacroRegime,
    ) -> str:
        """Create a full, actionable markdown investment dossier."""
        rec = recommendation
        tp = rec.trade_plan
        qm = rec.metrics
        rp = rec.risk_profile

        md = []
        md.append(f"# 🎯 Otonom Fon / ETF Yatırım Raporu: {rec.symbol} ({rec.name})")
        md.append(f"**Tarih:** {rec.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}  |  **Kaldıraç:** {rec.leverage}x  |  **Kategori:** {rec.category}")
        md.append("")
        md.append("---")
        md.append("## 📌 Karar Özeti (Executive Action)")
        action_emoji = "🚀" if "BUY" in rec.action.value else ("⚠️" if "HEDGE" in rec.action.value else "🛑")
        md.append(f"### {action_emoji} **ÖNERİ:** `{rec.action.value}`  |  **Güven Puanı (Conviction):** `{rec.conviction_score:.1f} / 100`")
        md.append("")
        md.append(f"> **Yatırım Tezi:** {rec.thesis_summary}")
        md.append("")
        md.append("---")
        md.append("## 📋 Somut İşlem Planı (Tactical Trade Plan)")
        md.append(f"| Parametre | Değer | Açıklama |")
        md.append(f"| :--- | :--- | :--- |")
        md.append(f"| **Güncel Fiyat** | `${qm.current_price:.2f}` | Son seans kapanış/anlık fiyat |")
        md.append(f"| **Giriş Aralığı (Entry Zone)** | `${tp.entry_zone_low:.2f} - ${tp.entry_zone_high:.2f}` | İdeal pozisyon açılış bandı |")
        md.append(f"| **Hedef 1 (Kar Al - %50)** | `${tp.target_1:.2f}` (`+{((tp.target_1/qm.current_price)-1)*100:.1f}%`) | İlk kar realizasyonu / stop girişe çekme |")
        md.append(f"| **Hedef 2 (Ana Hedef)** | `${tp.target_2:.2f}` (`+{((tp.target_2/qm.current_price)-1)*100:.1f}%`) | Trend uzama hedefi |")
        md.append(f"| **Zarar Kes (Stop-Loss)** | `${tp.stop_loss:.2f}` (`{((tp.stop_loss/qm.current_price)-1)*100:.1f}%`) | Kesin disiplinli stop sınırı |")
        md.append(f"| **Risk / Ödül Oranı (R:R)** | `1 : {tp.risk_reward_ratio:.2f}` | Minimum 1:2 asimetrik getiri |")
        md.append(f"| **Maksimum Portföy Payı** | `%{tp.max_portfolio_allocation_pct:.1f}` | Yüksek risk/kaldıraç sermaye koruma tavanı |")
        md.append(f"| **Beklenen Vade (Horizon)** | `{tp.expected_holding_days} İş Günü` | Kaldıraç erimesine karşı taktik swing limiti |")
        md.append("")
        md.append("---")
        md.append("## 📊 Kantitatif & Teknik Metrikler")
        md.append(f"- **Momentum Getirileri:** 5 Gün: `%{qm.return_5d*100:+.2f}` | 20 Gün: `%{qm.return_20d*100:+.2f}` | 60 Gün: `%{qm.return_60d*100:+.2f}`")
        md.append(f"- **Yıllıklandırılmış Volatilite:** `%{qm.annualized_volatility*100:.1f}` ({rp.volatility_grade})")
        md.append(f"- **Sortino Oranı:** `{qm.sortino_ratio:.2f}`  |  **Sharpe Oranı:** `{qm.sharpe_ratio:.2f}`")
        md.append(f"- **Maksimum Düşüş (60G):** `%{qm.max_drawdown_60d*100:.1f}`")
        md.append(f"- **RSI (14):** `{qm.rsi_14:.1f}`  |  **Trend Hizalanması:** `{qm.trend_alignment}`")
        md.append(f"- **Beta (QQQ / SPY):** `QQQ: {qm.beta_qqq:.2f}` | `SPY: {qm.beta_spy:.2f}`")
        md.append(f"- **20 Günlük Ort. Hacim:** `{qm.avg_volume_20d:,.0f}` adet / gün (`${qm.avg_dollar_volume_20d/1e6:.1f}M` / gün)")
        md.append("")
        md.append("---")
        md.append("## 🌍 Makroekonomik Rejim Uyumu")
        md.append(f"- **Genel Rejim:** `{macro.regime_type.value}` (Güven: `%{macro.confidence_score:.0f}`)")
        md.append(f"- **Makro Özeti:** {macro.summary}")
        md.append(f"- **Endeks Trendleri:** SPY: `{macro.spy_trend}` | QQQ: `{macro.qqq_trend}` | Volatilite (UVXY): `{macro.volatility_uvxy_trend}` | Faizler (TLT): `{macro.rates_tlt_trend}`")
        md.append(f"- **Bu Varlığa Etkisi:** {rec.macro_context}")
        md.append("")
        md.append("---")
        md.append("## ⚡ Katalizörler ve Haber Akışı")
        md.append("### 🟢 Boğa Katalizörleri:")
        for c in rec.bull_catalysts:
            md.append(f"- {c}")
        md.append("")
        md.append("### 🔴 Risk Faktörleri & Ayı Senaryosu:")
        for r in rec.bear_risks:
            md.append(f"- {r}")
        md.append("")
        md.append("---")
        md.append("> ⚠️ **Yüksek Risk & Kaldıraç Uyarısı:** Kaldıraçlı ve yüksek betalı fonlar (2x/3x) günlük sıfırlama nedeniyle volatil piyasalarda bileşik getiri erimesine (leverage decay) maruz kalır. Belirtilen zarar kes (stop-loss) seviyesine ve maksimum taşıma vadesine titizlikle uyulmalıdır.")

        return "\n".join(md)

    def save_report(
        self,
        recommendation: InvestmentRecommendation,
        macro: MacroRegime,
    ) -> Path:
        """Save report to Markdown and JSON files."""
        md_text = self.generate_dossier_markdown(recommendation, macro)
        timestamp_str = recommendation.generated_at.strftime("%Y%m%d_%H%M%S")
        filename_base = f"recommendation_{recommendation.symbol}_{timestamp_str}"

        md_path = self.output_dir / f"{filename_base}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        json_path = self.output_dir / f"{filename_base}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(recommendation.model_dump(mode="json"), f, indent=2)

        return md_path
