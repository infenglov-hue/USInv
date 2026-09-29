# Operations

## GitHub Actions

Tüm cron saatleri UTC'dir; İstanbul UTC+3'tür.

| Workflow | Zamanlama | Görev |
|---|---|---|
| `Publish Mobile Feed` | Hafta içi `06:17`, `07:17`, `16:17`, `16:47`; Pazar `17:17` | State restore, kısa fiyat/evren güncellemesi, T-1 skor → T portföy, snapshot/feed, state store; Cuma/Pazar rotasyon ön hazırlığı |
| `Financial Refresh` | Salı ve Cumartesi `02:07` | Ara dönem finansalları, clean, state store, freshness kapıları |
| `Publish PWA Live Prices` | Hafta içi `07:07–15:37`, 30 dakikada bir | Feed'deki küçük `live_tickers.json` sözleşmesini kullanır; büyük state DB'yi restore etmeden `live_prices.json` yayınlar |
| `Monitor Portfolio Alerts` | Feed başarıyla bitince ve hafta içi `07:00`, `10:00`, `13:00` | Stop/target/KAP/rotasyon Telegram kontrolleri; feed sonrası tetikleme cron yarışını kapatır |
| `Weekly Investor Audit` | Pazar `03:17` | 2018→bugün T-1 veri/T açılış investor-grade stres audit'i; compact JSON ve ısınmış skor cache'i state'e yazar |
| `Rebuild Historical Score Cache` | Yalnız manuel | Uzun corrected TTM/PIT cache ve backtest state bakım işi |

Günlük publish'in kritik hafif fetch komutu:

```bash
python -m bist_picker fetch --prices-only --refresh-universe --price-days 15
```

Bunu scheduled yolda tam history/financial backfill'e çevirmek timeout ve kota
riski yaratır. Finansallar ayrı workflow'dadır.

## Yerel doğrulama

Backend tam suite:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Seçili hızlı kontroller:

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_state_db_artifact.py
.venv\Scripts\python.exe -m pytest -q tests/test_mobile_snapshot_export.py tests/test_real_returns_feed.py
.venv\Scripts\python.exe -m pytest -q bist_picker/tests/test_financial_periods.py bist_picker/tests/test_publication_date_guard.py
```

PWA:

```powershell
node --check app.js
node --check sw.js
node tests/pwa-static-check.mjs
```

## Canlı feed sağlık kontrolü

1. `Publish Mobile Feed` son run'ı başarılı mı?
2. `MobileInv-feed/gh-pages` son commit'i beklenen run'a mı ait?
3. Public `manifest.json` içindeki:
   - `exported_at`,
   - `rotation.next_rotation_date`,
   - `snapshot.sha256`,
   - `snapshot.size_bytes`,
   - `real_returns`
   alanları tutarlı mı?
4. İndirilen `mobile_snapshot.db.gz` SHA-256 manifest ile aynı mı?
5. PWA header'daki model/fiyat tarihleri ve veri-sağlığı kartı beklenen işlem
   gününü mü gösteriyor?

Tarih semantiği:

- `signal_date`: portföy kararının görebildiği son tamamlanmış seans (`T-1`),
- `selection_date`: önerinin işlem için geçerli olduğu seans (`T`),
- `snapshot_date/model date`: feed/model üretim günü,
- `exported_at`: feed paketleme zamanı,
- `latest_price_date`: son tamamlanmış işlem günü,
- live quote zamanı: `live_prices.json` içindeki sağlayıcı zamanı.

Hafta sonu veya seans öncesi `latest_price_date`'in takvim gününden eski olması
normaldir.

Rotasyon portföyünü elle hazırlamak için:

```powershell
.venv\Scripts\python.exe -m bist_picker prepare-portfolio --next-rotation --max-days-ahead 3
```

Komut takvimden bir gün çıkarmak yerine DB'deki son geniş-kapsamlı tamamlanmış
seansı bulur; hafta sonu ve tatilde gelecekteki veriye bakmaz.

## State ve feed güvenliği

- State restore sonrası SQLite `quick_check=ok` olmalıdır.
- Split artifact manifest/chunk checksum'ları doğrulanmadan DB kullanılmaz.
- Runtime DB artifact'ı `MobileInv-state` üzerinde `state-current` etiketli
  rolling release'in asset'lerindedir; workflow'lar `state_db_artifact.py
  pull/push` kullanır. Release yoksa restore **sessizce boş DB ile devam
  etmez**, koşuyu düşürür. Elle çekmek için:
  `GH_TOKEN=<pat> python scripts/state_db_artifact.py pull --repository
  Somethinglikeu-hub/MobileInv-state --target data/bist_picker.db`
- Feed export daha eski snapshot'la public feed'i ezmeye çalışırsa fail etmelidir.
- Public export state DB'den eski skor/fiyat silmemelidir.
- Test çıktıları production `data/output` veya feed dosyalarının üzerine yazmaz;
  geçici dizin kullanır.

## Rollback

GitHub geçmişi korunur. Yanlış merge için tercih edilen yol:

```bash
git switch main
git pull --ff-only
git switch -c revert/<kisa-ad>
git revert <merge_commit_sha>
git push -u origin revert/<kisa-ad>
```

Sonra revert PR'ı açılır. `reset --hard`, force-push veya ana dal geçmişini
yeniden yazma kullanılmaz.

Yalnız feed verisini bilinçli eski sürüme almak gerekiyorsa kod revert'inden
ayrı bir operasyon olarak yapılır; manifest/snapshot hash'i birlikte geri
alınır ve yeni Pages deploy'u doğrulanır.

## Doküman geçmişi

2026-07-13 compact öncesi tüm belgeler `f70384d` commit'inde bulunur:

```bash
git show f70384d:docs/<DOSYA>.md
git restore --source=f70384d -- docs/<DOSYA>.md
```

İkinci komut çalışma ağacını değiştirir; yalnız bilinçli geri yüklemede kullan.
Dosya listesi `HISTORY_INDEX.md` içindedir.
