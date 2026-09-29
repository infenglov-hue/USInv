# 2026-07-27 — CI dayanıklılık turu (Opus)

Tetikleyen olay: kullanıcı "bugün yeni gelmemiş" dedi. Kök neden aramasından üç ayrı
üretim sorunu çıktı; ikisi kapatıldı, biri kullanıcı kararına bırakıldı.

## Olay: 27 Temmuz sabahı hiçbir cron tetiklenmedi

`Publish Mobile Feed` 06:17/07:17 UTC, `Publish PWA Live Prices`'ın altı cron'u,
`Monitor Portfolio Alerts` 07:00 — hepsi kaçtı. Kota veya config sorunu değildi:
manuel dispatch anında koştu. GitHub zamanlayıcısı tıkanmış, birikmiş kuyruk 10:10'da
boşalmıştı (~4 saat gecikme). PWA sabah boyu Cuma verisini servis etti; **bunu gören
bir alarm yoktu.**

Rotasyon içeriği etkilenmedi: 2026-07-27 çevrimi Pazar 18:19 UTC ön-hazırlık koşusunda
zaten yayınlanmıştı (SANEL, PSGYO yeni; CCOLA, THYAO, BIMAS devam; YUNSA çıktı).

## Yapılanlar

### 1. Sığ klonlar — PR #67 (`93184b4`)

Altı workflow state ve feed repolarını **tam geçmişle** klonlayıp yalnızca tepeyi
okuyordu:

| repo | boyut | klonlayan workflow |
|---|---|---|
| `MobileInv-state` | 10,6 GB | mobile-feed, monitor-alerts, investor-audit, financial-refresh, rebuild-score-cache |
| `MobileInv-feed` | 1,5 GB | mobile-feed, publish-live-prices |

Sonuç: `Publish Mobile Feed` koşusu `Free space left: 67 MB` uyarısıyla bitti;
`Publish PWA Live Prices` günde 18 kez 1,5 GB çekiyordu.

Hepsine `--depth 1 --no-single-branch`. `--no-single-branch` bilinçli: gh-pages orphan
fallback'i ve live dalının gh-pages'ten türetilmesi buna bağlı.
`rebuild-score-cache` yalnızca `state_db_commit` verildiğinde tam klon alır.

**Ölçüm:** state restore adımı `3dk 37sn → 9sn`, feed klonu `2,3sn`.

### 2. Artifact taşıması git → release — PR #68 (`8dcc9a9`)

Sığ klon semptomu çözdü, büyümeyi değil: pipeline her koşuda ~130 MB'lık parçalı
artifact'ı baştan yazıp commit'liyordu.

Parça formatı değişmedi; yalnız taşıma katmanı değişti. `scripts/state_db_artifact.py`
içine `pull`/`push` alt komutları eklendi: `gh release download|upload` ile tek rolling
`state-current` release'i üzerinde çalışır. Release asset'leri git nesne deposuna
girmediği için repo büyümesi durur.

- `restore` her parçanın size/sha256'sını doğrulamaya devam eder.
- Release yoksa koşu **düşer**; eski `if [ -f ... ]` guard'ı sessizce boş DB'yle devam
  ediyordu.
- `push` ilk kullanımda release'i yaratır, küçülen artifact'tan kalan artık parçaları
  siler.
- Üç workflow'dan commit/rebase/push adımı kalktı → feed yayıncısı ile Telegram
  monitörü arasındaki push yarışı da bitti.

**Hiçbir şey silinmedi.** Commit'lenmiş eski artifact'lar geçmişte duruyor;
`Rebuild Historical Score Cache` `state_db_commit` ile hâlâ git'ten okur. Küçük dosyalar
(`alerts_state.json`, investor-grade JSON) git'te kalmaya devam eder.

**Doğrulama:** suite 792/792; sahte `gh` runner'ıyla 4 yeni test; release son
commit'lenmiş artifact'tan seed edildi ve `pull` gerçek release'e karşı koşuldu
(833 MB restore, `quick_check: ok`, veri 2026-07-24'e kadar). Merge sonrası iki
üretim koşusu: `Monitor Portfolio Alerts` yeşil (restore 21sn),
`Publish Mobile Feed` yeşil — restore 10sn, state push 47sn, feed yayını 7sn.
Release asset'leri 15:11:27'de değişti; state reposunun son **git commit'i**
14:09'da kaldı, yani artifact artık git'e düşmüyor: büyüme durdu.

### 3. Tazelik bekçisi — feed reposu PR #9

`MobileInv-feed` **main** dalında `.github/workflows/feed-watchdog.yml`. Motor
reposunda değil, çünkü oradaki bir bekçi 27 Temmuz'da aynı tıkanan kuyrukta beklerdi;
ayrıca feed public olduğu için runner dakikaları bedava.

Hafta içi 09:00 ve 18:30 UTC'de `gh-pages/manifest.json` yaşına bakar (eşik 3,5 sa).
Bayatsa `feed-stale` issue'su açar; `ENGINE_DISPATCH_TOKEN` secret'ı varsa motoru
dispatch edip kendi kendini onarır. Tazeyse açık issue'yu kapatır.

Uçtan uca test edildi (tespit + tokensiz zarif atlama + issue açma doğrulandı, test
issue'su kapatıldı).

## Kalanlar

- **`ENGINE_DISPATCH_TOKEN`** feed reposuna eklenmeli (PAT, `actions: write` @ MobileInv).
  Secret girme işi kullanıcıya ait; eklenene kadar bekçi alarm-only çalışır.
- **State reposu geçmişi hâlâ 10,6 GB.** Artık büyümüyor ama küçülmüyor da. Geçmişi
  kesmek geri dönüşsüz olduğu için kullanıcı kararına bırakıldı; önerilen yol, kesmeden
  önce birkaç kontrol noktasını release asset'i olarak saklamak.
- **Tedbir/VBTS körlüğü (yeni bulgu, kod yazılmadı).** Kod tabanında tedbir/tek fiyat
  kavramı yok, ama yürütme varsayımı `same_day_open`. 2026-07-27'de SANEL tedbirdeydi
  ve kullanıcı almadı → yayınlanan kitap gerçek kitaptan sapıyor. Seçenekler: (a) seçim
  havuzu filtresi — canlı seçimi değiştirir, backtest'siz shiplenmez, (b) PWA'da rozet,
  (c) `thresholds.yaml idle_cash_yield` (şu an `none`) ile boşta nakdi modellemek.
