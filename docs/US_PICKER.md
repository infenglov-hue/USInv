# US Picker — BIST Picker'ın ABD versiyonu

Durum: **geliştirme (2026-09-29)**. Canlı değil, backtest kanıtı henüz yok.
Bu belge `us_picker/` paketinin kanonik açıklamasıdır.

## Ne bu?

`us_picker/`, MobileInv'deki BIST Picker backend'inin (`bist_picker/`,
commit `b3935e3`, 2026-08-08 — Gemini'nin 2026-09-18 "V2" değişikliklerinden
önceki sürüm) birebir kopyasıdır. Değiştirilen yalnızca piyasaya özgü katman:

| BIST Picker | US Picker |
|---|---|
| İş Yatırım fiyatları | Alpaca günlük bar (ham `close` + toplam getiri `adjusted_close`), 2016+ |
| İş Yatırım mali tablolar | SEC companyfacts → İş Yatırım kalem kodlu tablolar (`data/sources/sec_statements.py`) |
| İş Yatırım/KAP şirket listesi | SEC ticker listesi × Alpaca işlem görebilir hisseler + likidite eleği |
| BIST 100 üyeliği (`is_bist100`) | S&P 500 üyeliği; seçici tarihe göre (point-in-time) `index_memberships` tablosunu kullanır |
| XU100 benchmark | SPY (toplam getiri) — kodda ticker `SPY` |
| TCMB makro | FRED: fed funds, 10y, CPI, 5y5y breakeven, Baa−10y spread |
| Türkiye CDS (`turkey_cds_5y`) | Moody's Baa − 10y kredi spreadi (bps, FRED `BAA10Y`) — aynı kolon |
| Damodaran Türkiye ERP | Damodaran ABD ERP |
| KAP bildirimleri | SEC 8-K/10-Q/10-K/13D (`data/sources/sec_feed.py`) |
| Yahoo canlı fiyat | Alpaca IEX snapshot |
| IAS-29 / bedelsiz uçurum düzeltmesi | kapalı (ABD'de yok; bölünmeler aşağıda) |

Skorlama, normalizasyon, seçici, rotasyon, stop/hedef, nakit rejimi, backtest,
snapshot ve PWA sözleşmesi BIST Picker'la aynıdır. Kolon adları (`is_bist100`,
`sector_bist`, `*_try`, `turkey_cds_5y`) upstream'le fark küçük kalsın diye
korunmuştur; anlamları yukarıdaki tablodadır.

## ABD'ye özgü doğruluk kuralları

1. **Mali tablolar as-first-filed.** Her kalemin SEC'e ilk verildiği değer
   kullanılır; sonraki düzeltmeler geçmişi değiştirmez. `publication_date` =
   dosyalama günü + 1 (seans sonrası dosyalamalar T-1 sinyaline sızmasın).
2. **Takvimleştirme.** Mali yılı Aralık'ta bitmeyen şirketlerin (Apple, Walmart,
   Nike) çeyrekleri en yakın takvim çeyreğine eşlenir; YTD/TTM mantığı BIST ile
   aynı çalışır.
3. **Bölünmeler.** Hisse sayıları bölünmeden bağımsız "baz birim"de saklanır,
   değerleme fiyatı aynı birime çevrilir (`utils/splits.py`). Açık pozisyonların
   giriş/stop/hedef fiyatları bölünme gününde bir kez yeniden ölçeklenir
   (`portfolio/split_positions.py`). Gösterilen tüm fiyatlar gerçek işlem
   fiyatıdır.
4. **`adjusted_close` toplam getiridir** (temettü dahil). Momentum/backtest onu
   kullanır; değerleme ve stop'lar ham `close` kullanır.
5. **Ağırlıklar BIST'ten gelir, ABD'de doğrulanmadı.** BIST'e özgü reel faiz
   ağırlık bloğu kapalıdır. Parametre değişikliği BIST Picker'ın
   `MODEL_AND_BACKTEST.md` kabul kapısına tabidir.

## Bilinen sınırlar

- Evren bugünkü likit NYSE/Nasdaq şirketlerinden kurulur; 2016'dan beri batan
  veya satın alınan ve bugün listede olmayan şirketler yoktur (survivorship).
  S&P 500 üyelik geçmişi point-in-time'dır, evren değildir.
- Banka/sigorta modellerinin BIST'e özgü girdileri (CAR, NPL, NIM) SEC
  verisinde yoktur; bu modeller eksik veriyle çalışır.
- Yabancı ihraççılar (20-F/40-F, ADR'ler) kapsam dışıdır.

## Çalıştırma (yerel)

```bash
export US_PICKER_DB_PATH=~/.us-picker/us_picker.db   # OneDrive dışı
export US_PICKER_SEC_EMAIL=...  ALPACA_KEY_ID=...  ALPACA_SECRET_KEY=...
python -m us_picker fetch --prices-only --refresh-universe --price-days 4000
US_PICKER_COMPANYFACTS_ZIP=~/.us-picker/companyfacts.zip python -m us_picker fetch --history
python -m us_picker fetch   # macro dahil tam koşu
python -m us_picker clean
python -m us_picker prepare-portfolio
python -m us_picker backtest --start-date 2017-01-09 --rebalance-weeks 2 --execution same-day-open
python -m us_picker export-mobile-feed --feed-dir mobile-feed-dist
```

Testler: `python -m pytest -q us_picker/tests tests_picker`; PWA: `cd pwa &&
node --check app.js && node tests/pwa-static-check.mjs`.

## Eski `usinv/` paketi hakkında

`usinv/`, `ai/`, `web/` ve eski dokümanlardaki "Phase 5 PASS (Sharpe 0.94)"
ve "Phase 6 canlı" iddiaları geçersizdir: `usinv/backtest/run_experiments.py`
rastgele sayı üreten bir simülatördür, `decision.yml` elle yazılmış sabit bir
`web/snapshot.json` dosyasını yayınlar. Bu kod tarihçe için tutulur; aktif ürün
`us_picker/` + `pwa/`'dır.
