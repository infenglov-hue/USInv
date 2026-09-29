# Fable 5 + Codex Bulguları — Compact Kayıt

İlk audit: 2026-07-09. Son kabul: 2026-07-12. Compact: 2026-07-13.

Bu belge “son haftalarda neden kötü performans oluştu?” incelemesinin kısa,
güncel sonucudur. 1.305 satırlık tam oturum kaydı Git'te korunur:

```bash
git show f70384d:docs/FABLE5_BULGULARI_2026-07-09.md
```

## Kısa hüküm

Fable ve Codex aynı ana aileleri buldu: bayat/yanlış tüketilen finansal veri,
rejim zayıflaması, split bozulması ve ölü risk girdileri. Codex ayrıca kaybın
yalnız veriden gelmediğini; incumbent+kota seçimi, kısa-vadeli faktör davranışı,
beş hisselik yoğunlaşma, XU100 finalizasyonu ve performans muhasebesinin sonucu
büyüttüğünü kanıtladı.

Sonuç “model tamamen kötü” veya “veri tek başına bozuktu” değildir:

1. Veri/cache/muhasebe hattında gerçek kusurlar vardı ve düzeltildi.
2. Canlı sepet gerçekten zayıf hisseler seçmişti; piyasa tek açıklama değildi.
3. Değer/small-cap avantajı pozitif reel faiz rejiminde zayıfladı.
4. Beş eşit ağırlıklı isim ve başlangıç stopları kısa dönem zararı büyüttü.
5. Düzeltilmiş sistemde de zarar/drawdown mümkündür; garanti oluşmadı.

## Birleşik bulgular

| ID | Bulgu | Güncel durum |
|---|---|---|
| B1/C1 | Q1-2026 finansalları alınmıyor ve alınsa bile score input'a ulaşmıyordu | **Kapatıldı:** financial refresh + freshness + TTM/PIT + cache v1 |
| B2/C4 | Pozitif reel faizde eski value tilt zayıfladı | **Koşullu çözüm canlı:** reel-faiz bloğu; saf OOS değil, forward izleniyor |
| B3 | Legacy macro overlay iç içe config'te `dict*float` ile çöküyordu | **Kapatıldı:** izinli blok/typelı döngü + regresyon testi |
| B4 | Split/bedelsiz ham close'da sahte çöküş ve stop üretiyordu | **Kapatıldı:** adjusted-close üretimi + idempotence/invariant testleri |
| B5 | CDS beslenmiyor; ağır `RISK_OFF` makro bacağı etkisiz | **Açık:** ölçülmeden kaynak/yeniden ölçekleme yapılmayacak |
| B6 | Insider verisi güvenilir şekilde beslenmiyor | **Açık:** coverage/freshness olmadan aktif özellik sayılmıyor |
| B7 | KAP event scoring kodu/tablesi alpha'da kullanılmıyor | **Dead-code adayı:** şema uyumluluğu korunarak ayrı cleanup |
| B8 | Red flags UI'da görünür ama selector hard-gate değil | **Bilinçli:** marjinal değer ölçülmeden gate yapılmayacak |
| C2/C3 | Incumbent + sleeve kotaları gerçek ham top-5 yerine kısıtlı sepet seçti | **Azaltıldı:** `max_non_bist100 2 → 1`; forward kanıt bekleniyor |
| C5 | 5 hisse ve stop whipsaw zararı yoğunlaştırdı | **Açık model riski:** 5/8/10 + maliyet A/B gerekir |
| C6 | Açık seans XU100 satırı final kapanış gibi kalıyordu | **Kapatıldı:** final bar politikası/upsert |
| C7 | KPI/history aynı performansı göstermiyordu | **Kapatıldı:** kanonik NAV/ledger + reel return manifest |
| OPS | Commit'siz çalışma `reset --hard` ile kaybedildi | **Kural:** destructive Git yasak; revert PR kullanılır |

## Kök neden kalıpları

- **Tüketici var, kaynak yok:** schema/feature yazılmış ama besleyen job ve
  coverage alarmı kurulmamıştı.
- **Sessiz fallback:** eksik veri çökme yerine nötr değer/ham close üreterek
  kusuru performans düşene kadar sakladı.
- **Aynı bozuk veriyle backtest:** veri hatası simülasyonda da bulunduğu için
  sonuç iç tutarlı ama yanlış görünebildi; invariant testleri gerekliydi.
- **Cache provenance eksikliği:** eski skor matematiği yeni kodla aynıymış gibi
  kullanılabiliyordu.
- **Tek tarihsel yol:** çok sayıda parametre aynı dönem üzerinde seçildi;
  güçlü full-period sonuç ileri taşınabilirlik kanıtı değildir.

## Uygulanan düzeltme zinciri

- PR `#39`: financial refresh ve güncel ara dönem veri çekimi.
- PR `#41/#42`: reel-faiz koşullu ağırlık ve overlay crash düzeltmesi.
- PR `#43`: corporate-action adjusted-close hattı.
- PR `#49`: TTM/PIT, cache sürümü, XU100 finalizasyonu, NAV ve non-BIST cap.
- PR `#50`: historical score-cache maintenance workflow'u.
- PR `#51/#52`: historical composition import ve boş dönem dayanıklılığı.
- PR `#53`: feed export'un state geçmişini budamasının engellenmesi.
- PR `#54`: checksum'lı split runtime state.
- PR `#55`: fiyat sağlayıcı circuit breaker/fallback.
- PR `#56`: tam NAV deflator penceresi.
- MobileInv-feed PR `#1`: PWA v18 risk/veri-sağlığı/UX görünürlüğü.

## Remote kabul

Historical run `29183406840`:

- SQLite `quick_check=ok`, state `768.462.848` byte restore.
- `1.186.276` fiyat satırı; XU100 `2016-02-04 → 2026-07-10`.
- 216 TTM/PIT skor tarihi, 217 NAV noktası.
- Nominal `%1448,6766`, CAGR `%39,2284`, Sharpe `1,3358`,
  MaxDD `-%28,2972`.
- BIST100 `%547,2053`, alpha `+901,4712` puan.

Publish run `29190864895`:

- Full quality gate başarılı.
- State, makro/cash kapıları ve public snapshot/manifest başarılı.
- Cash `NORMAL`, `%0`.
- Nominal `%1448,6766`; TÜFE-reel `%27,1763`; USD `%30,1732`;
  gram-altın `-%57,3846`.

## Dürüst son karar

Veri/cache/muhasebe kaynaklı sahte performans ve yoğunlaştırıcı seçim kusurlarının
önemli kısmı giderildi. Buna rağmen corrected backtest MaxDD'si yaklaşık `-%28,3`
ve kötü dönemler negatiftir. Yeni kuralların gerçek kanıtı 2026-07-13 sonrası
forward portföydür. Geçmiş getiri kâr garantisi değildir.

Aktif açık risk ve sıradaki işler için `CURRENT_STATE.md`; model değişikliği
kabul kapısı için `MODEL_AND_BACKTEST.md` kanoniktir.
