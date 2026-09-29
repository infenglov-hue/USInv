# Current State

Son doğrulama: **2026-07-13**. Bu belge üretim gerçeğinin kısa özetidir;
oturum günlüğü değildir.

## Ürün

- Tek aktif kullanıcı arayüzü [PWA/WebUI](https://somethinglikeu-hub.github.io/MobileInv-feed/).
- Backend kullanıcıya emir göndermez; veri, skor, portföy önerisi ve risk
  uyarısı üretir.
- Android repo yalnız arşivdir.
- Ana portföy `index_aware` ALPHA, hedef 5 hisse ve iki haftalık rotasyondur.

## Son doğrulanmış production baseline

| Alan | Kanıt |
|---|---|
| Historical state/cache | [Rebuild run 29183406840](https://github.com/Somethinglikeu-hub/MobileInv/actions/runs/29183406840), başarılı |
| Canlı feed üretimi | [Publish run 29190864895](https://github.com/Somethinglikeu-hub/MobileInv/actions/runs/29190864895), başarılı |
| Kişisel fill/maliyet | [MobileInv-feed PR #3](https://github.com/Somethinglikeu-hub/MobileInv-feed/pull/3), Midas `%0`, Brüt/Net, gerçek fill |
| Pre-open PWA v23 | [MobileInv-feed PR #4](https://github.com/Somethinglikeu-hub/MobileInv-feed/pull/4) + [PR #5](https://github.com/Somethinglikeu-hub/MobileInv-feed/pull/5) + [PR #6](https://github.com/Somethinglikeu-hub/MobileInv-feed/pull/6), model/gerçek fiyat ayrımı, doğru T-1 sinyal/T portföy tarihi ve sabit-yıl audit etiketi temizliği |
| Snapshot sözleşmesi | Backend geçişiyle schema `13`; legacy schema `12` PWA fallback'i korunur |

Historical kabul:

- 218 NAV noktası, `2018-03-19 → 2026-07-13`.
- Corrected TTM/PIT cache sürümü `2026-07-10-ttm-v1`; kabul koşusunda
  `stale_score_cache_dates=0`.
- Production execution: `T-1` tamamlanmış veri → `T` ilk açılış.
- Nominal `%4099,9982`; CAGR `%56,7323`; Sharpe `1,7349`;
  MaxDD `-%22,7896`.
- BIST100 `%547,8044`; nominal alpha `+3552,1938` puan.
- TÜFE-reel `%244,9010`; USD `%250,1980`; gram-altın `%13,5786`.

Bu baseline bir garanti değildir. Forward kanıt 2026-07-13 ve sonrasındaki
canlı rotasyonlardan oluşur; geçmiş simülasyonla karıştırılmaz.

Temiz 5 yıllık kontrol (`2021-07-12 → 2026-07-13`) yeni execution için
`%2334,1977` toplam, `%89,7682` CAGR, `2,2813` Sharpe ve `-%22,7896` MaxDD
üretti. Eski next-open mode toplam/CAGR/Sharpe'ta daha düşük, MaxDD'de
`3,2063` puan daha iyiydi. Ayrıntı ve kabul gerekçesi `MODEL_AND_BACKTEST.md`.

## Kapatılan ana kusurlar

- Güncel ara dönem finansallarının alınması ve freshness kapıları.
- Ara dönem → tekil çeyrek → TTM üretimi ve point-in-time tüketimi.
- Score cache sürümleme/invalidation ve tarihsel cache bakım workflow'u.
- Split/bedelsiz kaynaklı sahte fiyat çöküşlerinin düzeltilmesi.
- XU100 açık-seans satırının final kapanış gibi kalmasının engellenmesi.
- Canlı ve tarihsel performans için kanonik NAV/ledger sözleşmesi.
- Feed export sırasında uzun tarihsel state'in budanmasının engellenmesi.
- GitHub boyut sınırı için checksum'lı split state artifact.
- Fiyat sağlayıcı kesintisinde hızlı circuit breaker/fallback.
- Reel/USD/gram-altın deflator tarihinin tam NAV penceresiyle hizalanması.
- PWA v18 veri-sağlığı, risk görünürlüğü ve mobil kullanılabilirlik katmanı.
- Model referans fiyatı ile cihazda özel tutulan gerçek alış/satış fill'inin
  ayrılması; broker maliyet profili ve Brüt/Net kişisel History hesabı.
- T-1 tamamlanmış sinyal tarihi ile T effective/işlem tarihinin DB, backtest,
  snapshot ve arayüzde ayrı alanlar olarak taşınması.
- Canlı fiyat işinin büyük runtime DB yerine compact işlem gören ticker
  sözleşmesi kullanması; KAP-only kayıtların audit için korunması.
- Feed başarıyla bittikten sonra Telegram monitor'un tetiklenmesi ve haftalık
  güncel investor-grade audit'in günlük yayın yolundan ayrılması.

İlgili backend PR'ları: `#39`, `#41`–`#43`, `#49`–`#56`.

## Açık riskler

### Model ve veri

- Backtest yaklaşık 8,3 yıldır; başlıkta “10 yıllık” denmemelidir.
- Delist olmuş ve hiç backfill edilmemiş şirketler nedeniyle survivorship
  riski tamamen kapanmış değildir.
- Bazı eski finansal satırlarda gerçek KAP yayın tarihi yerine muhafazakâr
  yayın-lag fallback'i kullanılır.
- Yapısal parametrelerin bir bölümü aynı 2018–2026 yolunda seçildi; saf OOS
  kanıt sınırlıdır.
- Beş eşit ağırlıklı hisse tek-isim/gap riskini büyütür.
- Flat/varsayılan işlem maliyeti büyük sermayede piyasa etkisini tam temsil
  etmez; likidite/kapasite modu ayrıca ölçülmelidir.

### Eksik/etkisiz girdiler

- CDS kaynağı bağlı değildir; cash modelinin en ağır `RISK_OFF` makro bacağı
  tasarlandığı gibi tetiklenemez. Bu değişiklik ölçülmeden yeniden ölçeklenmez.
- Insider/KAP event tabloları güvenilir coverage/freshness sözleşmesine sahip
  değildir; bu girdiler aktif alpha gibi sunulmaz.
- `red_flags` görünürlük/açıklama sağlar ama selector hard-gate değildir.

### Operasyon

- Seans dışı veya hafta sonu `latest_price_date` son işlem günü olur; bu tek
  başına bayatlık değildir.
- Live quote alınamazsa PWA son işlem/snapshot fallback'ini açık etiketler.
- GitHub Actions/state/feed üçlüsünden biri başarısızsa son geçerli PWA verisi
  kalır; güncellik ayrıca kontrol edilmelidir.
- GitHub cron kesin zaman SLA'sı değildir. Cuma/Pazar pre-stage ve Pazartesi
  sabahı iki deneme riski azaltır; kesin `09:50` SLA istenirse harici scheduler
  ayrıca seçilmelidir.

## Sıradaki ölçülü işler

1. 2026-07-13 sonrası forward portföy/NAV/slippage kaydını değiştirmeden tut.
2. CDS'siz cash modeli veya yeni CDS kaynağını tek değişkenli A/B ile ölç.
3. Delisted backfill + gerçek publication-date coverage'i tamamla.
4. 5/8/10 isim ve risk bütçesi seçeneklerini maliyet sonrası walk-forward ile
   karşılaştır; yalnız sağlamlık gösteren varyantı değerlendir.
5. PWA'nın yaklaşık 10 MB snapshot ilk-açılış süresini ayrı UX/performance işi
   olarak optimize et; model performansıyla karıştırma.

Canlı durum bu belgeden daha yeniyse GitHub Actions ve public `manifest.json`
otoritatiftir; doğrulama komutları `OPERATIONS.md` içindedir.
