# Kullanım Kılavuzu

Karar ekranı [PWA](https://somethinglikeu-hub.github.io/MobileInv-feed/),
Telegram yalnız bildirim kanalıdır. CLI son kullanıcı arayüzü değildir.

## Ana kural

Portföy **iki haftada bir Pazartesi** değişir. Çapa 29 Haziran 2026'dır:
13 Temmuz, 27 Temmuz, 10 Ağustos… Ara günlerde skor/fiyat yenilenebilir ama
liste stop/target dışında değiştirilmez.

## Sistem ritmi

| İstanbul zamanı | Otomasyon | Kullanıcı |
|---|---|---|
| Hafta içi yaklaşık 09:17–10:17 | Sabah feed pipeline | Rotasyon gününde yeni AL/SAT/TUT listesini kontrol et |
| Seans boyunca yaklaşık 30 dk | Live price JSON yenilenir; quote kendisi gecikmeli olabilir | Etiketi kontrol et |
| 10:00 / 13:00 / 16:00 | Telegram stop/target/KAP/rotasyon monitörü | Gerçek uyarı varsa işlem yap |
| Hafta içi yaklaşık 19:17–19:47 | Kapanış feed pipeline | Genellikle işlem gerekmez |
| Salı/Cumartesi yaklaşık 05:07 | Finansal refresh | İş başarısızsa Actions'ı kontrol et |

Rotasyon koşusu kaçarsa aynı cycle içindeki sonraki başarılı koşu telafi
rotasyonu yapabilir.

## Rotasyon günü

1. PWA'yı açıp model tarihi, fiyat tarihi ve veri-sağlığı kartını kontrol et.
2. Broker açılışında önce **SAT**, sonra **AL** işlemlerini uygula.
3. Model normal durumda 5 eşit slot hedefler. Stopla boşalan slot sonraki
   rotasyona kadar nakitte kalabilir; kendin yeni hisse ekleme.
4. PWA giriş referansı önceki kapanış olabilir; gerçek broker alış fiyatını
   kendi kaydında tut.
5. Sonraki rotasyona kadar **TUT**. Tek erken çıkış stop/target uyarısıdır.

Cash kartı `%0/%25/%50/%75` nakit hedefini gösterebilir. CDS girdisi eksik
olduğu için en ağır makro koruma bacağı tam doğrulanmış değildir; bunu garanti
koruma gibi yorumlama.

## Fiyat etiketleri

- **CANLI / gecikmeli:** live feed; işlemden önce broker fiyatını doğrula.
- **SON İŞLEM:** feed var, son quote eski; seans dışında normal olabilir.
- **SNAPSHOT:** live feed alınamadı; son pipeline fiyatı. Emir fiyatı olarak
  kullanma.

`model date`, `latest price date` ve quote zamanı farklıdır. Pazartesi seans
öncesinde Cuma kapanışı görünmesi normaldir.

## Stop veya hedef uyarısı

- Uyarı geldiğinde aynı gün broker'da işlemi uygula.
- Backend pozisyonu kapanmışsa modelle ayrışmamak için taşımaya devam etme.
- Boşalan slotu sonraki rotasyona kadar nakitte bırak.
- Uyarı yoksa gün içi hareket veya skor değişimiyle işlem yapma.

## Sağlık kontrolü

PWA eski görünüyorsa sırasıyla:

1. Uygulama içi yenile.
2. `MobileInv → Actions → Publish Mobile Feed` son run'ına bak.
3. Seans/hafta sonu nedeniyle son işlem tarihinin doğal olarak eski olup
   olmadığını kontrol et.
4. Gerekirse PWA'yı kapat-aç; son çare site cache'ini temizle.

## Yapma

- Rotasyon arasında kendiliğinden hisse değiştirip model performansını bozma.
- Snapshot fiyatıyla kör emir verme.
- Backtest getirisini canlı kâr veya garanti sanma.
- Telegram uyarısını otomatik broker emri sanma; sistem para taşımaz.

Bu araç yatırım danışmanlığı değildir. Pozisyon büyüklüğü, emir uygulaması ve
zarar toleransı kullanıcı sorumluluğundadır.
