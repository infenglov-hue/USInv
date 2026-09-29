> **UYARI (2026-09-29):** Aktif ürün artık `us_picker/` + `pwa/` (BIST Picker'ın ABD portu).
> Önce `docs/US_PICKER.md` dosyasını oku. Bu belgedeki "Phase 5 PASS", "Phase 6 canlı" ve benzeri
> iddialar geçersizdir: holdout sonuçları rastgele sayı simülatöründen, canlı karar akışı elle
> yazılmış sabit bir snapshot'tan geliyordu.

# VISION_AND_MIGRATION_GUIDE — BIST Picker (MobileInv) to USInv Architecture

## 1. Executive Summary

`USInv` was initiated as the US counterpart of `MobileInv` (BIST Picker, live at `https://somethinglikeu-hub.github.io/MobileInv-feed/`).
While `USInv` successfully solved the complex data engineering, point-in-time XBRL SEC EDGAR fundamentals, and Alpaca execution pipelines (Phases 0–6, 623 passing tests), the user's primary product requirement is:

> **Adapt the successful portfolio management, 5-stock concentrated selection, 2-week rotation, and 4-tier cash regime of `MobileInv` into the US equity market.**

This document provides a comparative blueprint between `MobileInv` and `USInv` to guide developers and AI agents in completing this adaptation.

---

## 2. Side-by-Side Architectural Mapping

| Feature | MobileInv (BIST Picker) | Current USInv Base | Target Adapted USInv |
|---|---|---|---|
| **Target Universe** | BIST All / BIST 100 filter | NYSE / Nasdaq / AMEX (D032 PIT universe) | NYSE / Nasdaq with $5M+ ADV and $5+ price |
| **Portfolio Size** | **5 concentrated positions** | 15 equal-weight positions | **5 concentrated positions** (equal weight 20% each) |
| **Rotation Cadence** | **Bi-weekly (every 2 weeks, Monday)** | Monthly (every 4 weeks) | **Bi-weekly (every 2 weeks, Monday open)** |
| **Incumbent Protection** | Protected via hold band buffer in `selector.py` | Strict rank cutoff | **Incumbent tolerance buffer** to minimize churn |
| **Scoring Formula** | 6-part Alpha: Buffett (15-25%), Graham/DCF (15-30%), Piotroski (15-20%), Growth (10%), Momentum (25%), Tech (5%) | Composite value + quality + momentum | Align with MobileInv's 6-part score using SEC PIT fundamentals |
| **Macro Cash Overlay** | 4-tier: Normal (%0), Caution (%25), Defensive (%50), Risk-Off (%75) | Binary O0 (0%) / O1 (20% cash) | **4-tier macro regime cash overlay** |
| **Stop & Exits** | ATR entry stop + Ratchet trailing stop + Target | Trailing stop only | ATR initial stop + Ratchet trailing stop + Target prices |
| **Narrative / Why Buy** | `ai_analyst.py` generates plain 2-3 sentence thesis | Raw factor percentiles | Plain-language 2-sentence thesis per pick in PWA |
| **User Surface** | Simple mobile PWA: 5 cards, cash bar, targets | PWA with tables, JSON, candidates list | Clean, focused 5-card mobile PWA |

---

## 3. Implementation Roadmap

### Phase A: Concentrated 5-Slot Portfolio Selector
- Modify `usinv/config/` default `holdings: 15` to `holdings: 5`.
- Update `usinv/portfolio/selector.py` to support `incumbent` retention rules from `MobileInv/bist_picker/portfolio/selector.py`.
- Ensure 2-week calendar rotations are driven by `usinv/calendar.py`.

### Phase B: Scoring Harmonization
- Harmonize `usinv/scoring/` factor weights:
  - Buffett Quality: ROE, ROIC, debt-to-equity, margin stability.
  - Graham + DCF Value: P/E, P/B, EV/EBITDA, DCF margin of safety.
  - Piotroski F-Score: 9-point fundamental test (veto bad firms).
  - Growth: YoY revenue and operating income expansion.
  - Momentum: 20-day, 60-day, and 120-day relative strength.
  - Technical: Trend positioning relative to 200 SMA and 14-day RSI.

### Phase C: 4-Tier Macro Cash Regime
- In `usinv/regime/`, introduce the 4-tier cash target:
  - `NORMAL`: 0% Cash (100% Equity)
  - `CAUTION`: 25% Cash (75% Equity)
  - `DEFENSIVE`: 50% Cash (50% Equity)
  - `RISK_OFF`: 75% Cash (25% Equity)
- Driven by S&P 500 200-day SMA, credit spreads (HY OAS), and volatility (VIX/UVXY).

### Phase D: Investor-First PWA Experience
- Keep `web/snapshot.json` schema strictly intact for backwards compatibility.
- Ensure the 5 selected names have: `entry_price`, `target_price`, `stop_price`, `thesis`.
- Optimize `web/index.html` and `web/app.js` to render 5 clean mobile cards and a prominent cash allocation badge.
