# Historical Documentation Index

2026-07-13'te AI context'ini küçültmek için tekrar eden handoff, progress,
audit ve plan belgeleri aktif ağaçtan çıkarıldı. İçerik kaybolmadı: compact
öncesi kanonik Git commit'i **`f70384d`**.

Herhangi bir eski belgeyi okumak:

```bash
git show f70384d:docs/FABLE5_BULGULARI_2026-07-09.md
```

Dosyayı çalışma ağacına bilinçli geri almak:

```bash
git restore --source=f70384d -- docs/<DOSYA>.md
```

## Handoff ve oturum kayıtları

- `HANDOFF_2026-06-24_PWA_FIRST_AND_PORTFOLIO_V2.md`
- `HANDOFF_2026-07-02_FABLE5.md`
- `HANDOFF_2026-07-07_PORTFOLIO_AUDIT.md`
- `PROGRESS_2026-07-02.md`
- `PROGRESS_2026-07-09_FINANCIAL_REFRESH.md`
- `PROGRESS_2026-07-11_PWA_POLISH.md`
- `WORK_SUMMARY_MAY_2026.md`

## Audit ve araştırmalar

- `ADVISOR_FLAW_REVIEW_2026-07-07.md`
- `BUG_AUDIT_2026-06-23.md`
- `PORTFOLIO_SELECTION_AUDIT_2026-07-07.md`
- `STRATEGY_AUDIT_2026-06-24.md`
- `stock_picking_audit.md`
- `stock_picking_audit_2026-07-02.md`
- `data_health_audit.md`
- `factor_attribution_2026-07.md`
- `PWA_LIVE_PRICES_AND_PICKER_REVIEW_2026-06-23.md`
- `PWA_MAIN_APP_AND_HISTORY_FIX_2026-06-23.md`
- `PORTFOLIO_V2_INDEX_AWARE_BACKTEST_2026-06-23.md`

## Eski plan ve roadmap'ler

- `10_year_backtest_data_plan.md`
- `10_year_backtest_results.md`
- `A3_DELISTED_BACKFILL_PLAN_2026-07-07.md`
- `backtest_data_backfill_plan.md`
- `development_roadmap.md`
- `IMPROVEMENT_PLAN_2026-07-02.md`
- `master_reboot_plan.md`
- `PWA_IMPROVEMENT_PLAN_2026-07-02.md`
- `phase_e_dynamic_exits.md`

## Legacy mobil/ürün tasarımları

- `app_tp_sl_integration_plan.md`
- `mobile_app_v2_plan.md`
- `mobile_cloud_sync.md`
- `NOTIFICATION_SYSTEM_DESIGN.md`
- `PRODUCT_SCOPE_2026-07-01.md`

## Birleştirilen aktif bilgiler

- Eski `strategy_logic.md` → `MODEL_AND_BACKTEST.md`.
- Ürün kapsamı/handoff → `CURRENT_STATE.md`, `ARCHITECTURE.md`, `DECISIONS.md`.
- Workflow ve rollback notları → `OPERATIONS.md`.
- Fable/Codex uzun uygulama günlüğü → compact
  `FABLE5_BULGULARI_2026-07-09.md`.

Tarihsel belgeler karar verirken otomatik olarak okunmaz; güncel kod ve kanonik
belgelerle çelişirse güncel sözleşme geçerlidir.

## 2026-07-13 kaynak kodu temizliği

Kod temizliği öncesi kaynak ağacı `0d4d31d` commit'indedir. Şunlar production
importu/CLI/workflow referansı olmadığı doğrulandıktan sonra çıkarıldı:

- takip edilen `scratch/` tek-seferlik teşhis/backfill scriptleri,
- üretilmiş `bist_picker.egg-info/` ve nested backtest CSV'si,
- eski `web/review.html`,
- duplicate `portfolio/backtest.py`,
- kullanılmayan EVDS-nowcast/event/macro-nowcast/heuristic optimizer/logging
  modülleri ve yalnız EVDS deneyi için olan test.

DB'deki legacy `KapEvent`, `MacroNowcast` ve ilgili kolonlar snapshot/runtime
uyumluluğu için silinmedi. Bir eski aracı görmek için:

```bash
git show 0d4d31d:scratch/backfill_prices.py
git show 0d4d31d:bist_picker/scoring/optimizer.py
```
