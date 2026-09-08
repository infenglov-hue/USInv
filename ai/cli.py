"""CLI entrypoint for USInv AI High-Risk / High-Reward ETF Intelligence Engine."""

import argparse
import sys

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ai.agent.orchestrator import AutonomousOrchestrator
from ai.core.models import InvestmentRecommendation, MacroRegime


def print_banner() -> None:
    print("=" * 76)
    print("  [*] USInv AI — Yuksek Riskli & Yuksek Getirili Fon/ETF Karar Motoru")
    print("=" * 76)


def format_recommendation_card(rec: InvestmentRecommendation) -> None:
    tp = rec.trade_plan
    qm = rec.metrics
    rp = rec.risk_profile

    action_colors = {
        "STRONG_BUY": "\033[92m[GÜÇLÜ AL / STRONG BUY]\033[0m",
        "SPECULATIVE_BUY": "\033[96m[SPEKÜLATİF AL / SPECULATIVE BUY]\033[0m",
        "HEDGE_SHORT": "\033[93m[KORUMA / HEDGE SHORT]\033[0m",
        "HOLD_NEUTRAL": "\033[90m[TUT / NÖTR]\033[0m",
        "AVOID_CASH": "\033[91m[UZAK DUR / NAKİT]\033[0m",
    }
    action_badge = action_colors.get(rec.action.value, f"[{rec.action.value}]")

    print(f"\n{'-' * 76}")
    print(f"🎯 VARLIK: {rec.symbol} — {rec.name} ({rec.leverage}x Kaldıraç)")
    print(f"💡 KARAR: {action_badge}  |  GÜVEN PUANI: {rec.conviction_score:.1f} / 100")
    print(f"{'-' * 76}")
    print(f"📖 YATIRIM TEZİ:")
    print(f"   {rec.thesis_summary}")
    print(f"\n📋 SOMUT İŞLEM PLANI:")
    print(f"   • Güncel Fiyat       : ${qm.current_price:.2f}")
    print(f"   • Giriş Aralığı      : ${tp.entry_zone_low:.2f} - ${tp.entry_zone_high:.2f}")
    print(f"   • Hedef 1 (%50 Kar)  : ${tp.target_1:.2f} (+{((tp.target_1/qm.current_price)-1)*100:.1f}%)")
    print(f"   • Hedef 2 (Ana Hedef): ${tp.target_2:.2f} (+{((tp.target_2/qm.current_price)-1)*100:.1f}%)")
    print(f"   • Zarar Kes (Stop)   : ${tp.stop_loss:.2f} ({((tp.stop_loss/qm.current_price)-1)*100:.1f}%)")
    print(f"   • Risk/Ödül Oranı    : 1 : {tp.risk_reward_ratio:.2f}")
    print(f"   • Maks. Portföy Payı : %{tp.max_portfolio_allocation_pct:.1f}")
    print(f"   • Vade Limiti        : {tp.expected_holding_days} İş Günü (Kaldıraç erime koruması)")
    print(f"\n📊 KANTİTATİF GÖRÜNÜM:")
    print(f"   • Getiri: 5G: %{qm.return_5d*100:+.1f} | 20G: %{qm.return_20d*100:+.1f} | 60G: %{qm.return_60d*100:+.1f}")
    print(f"   • Yıllık Volatilite  : %{qm.annualized_volatility*100:.1f} ({rp.volatility_grade})")
    print(f"   • Sortino: {qm.sortino_ratio:.2f} | RSI(14): {qm.rsi_14:.1f} | Trend: {qm.trend_alignment}")
    print(f"   • Beta (QQQ/SPY)     : QQQ: {qm.beta_qqq:.2f} | SPY: {qm.beta_spy:.2f}")
    print(f"\n⚡ KATALİZÖRLER & RİSKLER:")
    for b in rec.bull_catalysts[:2]:
        print(f"   [+] {b}")
    for r in rec.bear_risks[:2]:
        print(f"   [-] {r}")


def cmd_scan(args: argparse.Namespace) -> None:
    print_banner()
    print("🔍 Otonom Makro ve Yüksek Riskli ETF taraması başlatılıyor...\n")

    orchestrator = AutonomousOrchestrator()
    macro, recs = orchestrator.run_discovery_pipeline(top_n=args.top)

    print("=" * 76)
    print(f"🌍 MEVCUT MAKROEKONOMİK REJİM: {macro.regime_type.value} (Güven: %{macro.confidence_score:.0f})")
    print(f"ℹ️  {macro.summary}")
    print(f"📈 SPY: {macro.spy_trend} | QQQ: {macro.qqq_trend} | UVXY: {macro.volatility_uvxy_trend} | Faiz (TLT): {macro.rates_tlt_trend}")
    print("=" * 76)

    if not recs:
        print("⚠️ Uygun filtreden geçen aday bulunamadı.")
        return

    print(f"\n🏆 EN İYİ {len(recs)} YÜKSEK GETİRİ / YÜKSEK RİSK ADAYI:")
    for rec in recs:
        format_recommendation_card(rec)

    print(f"\n📁 Detaylı Markdown & JSON raporları 'ai/reports/output/' dizinine kaydedildi.")
    print("=" * 76)


def cmd_deepdive(args: argparse.Namespace) -> None:
    print_banner()
    sym = args.symbol.upper()
    print(f"🔬 {sym} için derinlemesine otonom araştırma ve işlem planı çıkarılıyor...\n")

    orchestrator = AutonomousOrchestrator()
    macro, rec = orchestrator.deep_dive(sym)

    print("=" * 76)
    print(f"🌍 MAKRO REJİM: {macro.regime_type.value} (Güven: %{macro.confidence_score:.0f})")
    print("=" * 76)

    format_recommendation_card(rec)
    print(f"\n📁 Rapor kaydedildi: ai/reports/output/recommendation_{sym}_*.md\n")


def cmd_macro(args: argparse.Namespace) -> None:
    print_banner()
    print("🌐 Canlı çoklu-varlık makroekonomik rejim analizi yapılıyor...\n")

    orchestrator = AutonomousOrchestrator()
    macro = orchestrator.macro_tool.get_macro_regime()

    print("=" * 76)
    print(f"🌍 MAKRO REJİM: {macro.regime_type.value}")
    print(f"📊 GÜVEN SEVİYESİ: %{macro.confidence_score:.1f}")
    print(f"📝 DEĞERLENDİRME: {macro.summary}")
    print("=" * 76)
    print("📈 GÖSTERGE TRENDLERİ:")
    print(f"   • S&P 500 (SPY)       : {macro.spy_trend}")
    print(f"   • Nasdaq-100 (QQQ)    : {macro.qqq_trend}")
    print(f"   • 20+ Yıl Tahvil (TLT): {macro.rates_tlt_trend} (Faiz baskısı göstergesi)")
    print(f"   • Volatilite (UVXY)   : {macro.volatility_uvxy_trend} (Korku / şok göstergesi)")
    print(f"   • Dolar Endeksi (UUP) : {macro.dollar_uup_trend} (Likidite göstergesi)")
    print(f"   • Yüksek Getiri (HYG) : {macro.credit_hyg_trend} (Kredi riski iştahı)")
    print(f"\n✅ Bu rejimde öne çıkan fon tipleri : {', '.join(macro.favored_etf_types)}")
    print(f"❌ Bu rejimde riskli/kaçınılması gerekenler: {', '.join(macro.unfavored_etf_types)}")
    print("=" * 76)


def cmd_history(args: argparse.Namespace) -> None:
    print_banner()
    print("📜 DuckDB üzerinde saklanan geçmiş öneriler:\n")

    orchestrator = AutonomousOrchestrator()
    recs = orchestrator.db.get_recent_recommendations(limit=args.limit)

    if not recs:
        print("Kayıtlı geçmiş öneri bulunamadı. Önce bir tarama çalıştırın: python -m ai.cli scan")
        return

    print(f"{'Zaman':<18} | {'Sembol':<6} | {'Karar':<15} | {'Güven':<6} | {'Fiyat':<8} | {'Hedef 2':<8} | {'Zarar Kes':<8}")
    print("-" * 82)
    for r in recs:
        ts = str(r["generated_at"])[:16]
        sym = r["symbol"]
        act = r["action"]
        cv = f"{r['conviction_score']:.1f}"
        pr = f"${r['current_price']:.2f}"
        t2 = f"${r['target_2']:.2f}"
        sl = f"${r['stop_loss']:.2f}"
        print(f"{ts:<18} | {sym:<6} | {act:<15} | {cv:<6} | {pr:<8} | {t2:<8} | {sl:<8}")
    print("-" * 82)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="USInv AI: Autonomous High-Risk / High-Reward ETF Investment Engine",
    )
    subparsers = parser.add_subparsers(dest="command", help="Komutlar")

    # scan
    scan_parser = subparsers.add_parser("scan", help="Otonom makro + ETF taraması çalıştır")
    scan_parser.add_argument("--top", type=int, default=4, help="Seçilecek en iyi fon sayısı (varsayılan: 4)")

    # deepdive
    dive_parser = subparsers.add_parser("deepdive", help="Belirli bir fon/ETF için derinlemesine analiz yap")
    dive_parser.add_argument("symbol", type=str, help="Analiz edilecek borsa sembolü (örn. TQQQ, SOXL, IBIT)")

    # macro
    subparsers.add_parser("macro", help="Canlı makro rejim durumunu görüntüle")

    # history
    hist_parser = subparsers.add_parser("history", help="Geçmiş önerileri ve kararları listele")
    hist_parser.add_argument("--limit", type=int, default=10, help="Listelenecek kayıt sayısı")

    args = parser.parse_args()

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "deepdive":
        cmd_deepdive(args)
    elif args.command == "macro":
        cmd_macro(args)
    elif args.command == "history":
        cmd_history(args)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
