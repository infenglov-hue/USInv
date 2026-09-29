# MobileInv Geliştirme Planı

Son kapsamlı inceleme: **2026-07-13**
Durum: **Aşama 0/2 production'da; Aşama 1/3/4/5A testleri geçti, yayın turunda.**

## Amaç ve çalışma biçimi

Amaç yalnızca backtest yüzdesini büyütmek değildir. Önlenebilir veri, zamanlama,
işlem ve ölçüm hatalarını kaldırmak; ardından stratejinin gerçek maliyet sonrası
ileri dönem performansını ölçmek ve kontrollü biçimde iyileştirmektir. Kâr garanti
edilemez, fakat sistem kaynaklı gereksiz zararlar ölçülebilir ve azaltılabilir.

Kota ve geri dönüş güvenliği için:

1. Her aşama ayrı branch ve küçük PR'larla yürütülür.
2. Bir sonraki aşama otomatik başlamaz; kabul kapısından sonra kullanıcı onayı
   beklenir.
3. Her PR sonunda bu dosyaya `yapıldı / kaldı / test / geri alma` notu yazılır.
4. Model, veri veya işlem sözleşmesi değişmedikçe pahalı 8 yıllık backtest tekrar
   çalıştırılmaz.
5. Üretim stratejisi, araştırma adayı bütün kapıları geçmeden değiştirilmez.
6. Yanlış değişiklik force-push ile değil, GitHub revert PR'ı ile geri alınır.

Kota sınıfları yaklaşık çalışma kapsamını anlatır:

- **Düşük:** tek dar PR ve hedefli testler.
- **Orta:** iki küçük PR veya bir uzun doğrulama/backtest turu.
- **Yüksek:** ayrı bir goal, araştırma turu ve birden fazla kabul kapısı.

## Yönetici özeti

Şu anki ana riskin yalnızca “kötü hisse seçimi” olduğuna dair kanıt yoktur.
2026-07-13 portföyündeki beş hissenin veri tamlığı `%100`, risk sınıfı `MEDIUM`.
Düzeltilmiş 2018-2026 backtest nominal olarak güçlüdür; fakat canlı icra sözleşmesi
backtest ile birebir aynı değildir, gerçek dolum fiyatı kaydedilmemektedir ve
maliyet yükseldikçe alfa hızla erimektedir. Bu nedenle ağırlıkları hemen değiştirmek
yerine önce kararın zamanında üretilmesi, aynı koşulla test edilmesi ve gerçekçi
icranın ölçülmesi gerekir.

Önerilen sıra:

`güvenli zemin -> güvenilir yayın -> gerçek fill/maliyet profili ->`
`sinyal/icra paritesi -> maliyet/kapasite -> veri yanlılığı ->`
`model araştırması -> PWA -> modularizasyon`

2026-07-13 kullanıcı kararları:

- Aktif broker profili Midas'tır ve güncel alış/satış maliyeti varsayılan olarak
  `%0` kabul edilir.
- Maliyet oranı ayarlanabilir kalır; gelecekte broker maliyeti değişirse geçmiş
  ham işlemleri yeniden yazmadan yeni oran/profil kullanılabilir.
- Arayüzde **Brüt / Net** görünüm switch'i bulunur. Brüt ve maliyet sonrası net
  sonuç birlikte saklanır; switch yalnız sunumu değiştirir.
- Modelin referans fiyatı ile kullanıcının gerçek alış/satış fill'i ayrı
  değerlerdir. Kişisel kâr/zarar gerçek fill'i kullanır; model değerlendirmesi
  referans fiyatını korur.
- `T+1 takas`, emrin ertesi gün verilmesi anlamına gelmez. Kabul edilen yön:
  `T-1` tamamlanmış veriyle `T` açılışından önce sinyal ve `T` günü alış/fill.

## Kanıtlanmış bulgular

| Öncelik | Bulgu | Kanıt ve etkisi |
|---|---|---|
| P0 | Canlı ve backtest işlem zamanı aynı değil | 2026-07-13 canlı seçim, son fiyat tarihi 2026-07-10 olan Cuma kapanışını yeni pozisyon giriş referansı yaptı. Varsayılan backtest ise sinyal tarihinden sonraki ilk açılışı kullanıyor. Canlı karar ile tarihsel simülasyon aynı fill sözleşmesini temsil etmiyor. |
| P0 | GitHub cron kesin saat garantisi vermiyor | 2026-07-06/10 haftasında canlı-fiyat işi beklenen 90 slotun yalnızca 16'sında başladı. 2026-07-13'te otomatik canlı-fiyat işi hiç başlamadı; public JSON 2026-07-10 18:42 İstanbul zamanında kaldı. Ana feed ve monitor çalışmaları da bazı günler saatlerce gecikti. |
| P0 | Feed ve Telegram rotasyon bildirimi yarışıyor | Sabah monitor saati feed tamamlanmadan önce kalabiliyor. 2026-07-13 feed ve Telegram için manuel dispatch gerekti. Bildirim başarılı feed yayınına bağlı değil. |
| P1 | Gerçek/icra edilebilir dolum kaydı yok | `entry_price` model referansıdır; broker dolumu, ilk icra edilebilir quote, komisyon ve slippage ayrı kaydedilmiyor. Bu nedenle forward OOS sonuç gerçek yatırımcı sonucundan ayrıştırılamıyor. |
| P1 | Backtest maliyete hassas | Güncel 8 yıllık sonuç nominal `%1448.68`, CAGR `%39.23`, Sharpe `1.336`, MaxDD `-%28.30`; fakat `%1.5` round-trip sürtünmede BIST100'e karşı toplam alfa yaklaşık `+4.58` puana kadar düşüyor. TÜFE ve USD sonrası toplam reel artış sınırlı, gram altın karşısında sonuç negatiftir. |
| P1 | Test izolasyonu ihlal ediliyor | Bazı backtest testleri geçici klasör vermediği için production `data/output` CSV'lerini sentetik `TEST1` verisiyle ezebiliyor. Bu, yerel araştırma sonucunun yanlış okunmasına yol açabilir. |
| P1 | Sık işler gereğinden büyük state restore ediyor | Runtime DB yerelde yaklaşık `716 MB`; GitHub'daki son restore yaklaşık `770 MB`. Canlı fiyat ve monitor işleri birkaç pozisyon/ticker için bu DB'yi tekrar tekrar indirip açıyor. |
| P2 | Aktif şirket ile işlem gören pay ayrımı zayıf | KAP listesindeki halka açık olmayan ihraççılar da `is_active=True` oluyor. Yerel 778 aktif kaydın yalnızca 612'sinde fiyat var; son canlı istekte 786 sembolden 618'i döndü, 168'i başarısız oldu. Seçici likidite/fiyat filtreleriyle korunuyor, fakat veri hattı şişiyor. |
| P2 | PWA ilk açılışı büyük | Snapshot yaklaşık `10.1 MB` sıkıştırılmış ve `28.0 MB` açık. Açık DB'nin yaklaşık `%97.6`'sı 730 günlük fiyat geçmişi ve indeksidir. Ölçülen snapshot indirmesi yaklaşık `8.3 sn`; karar ekranı bu geçmişin tamamını ilk açılışta indiriyor. |
| P2 | Investor-grade audit güncel feed'e taşınmıyor | PWA, audit JSON yoksa NAV özetine düşüyor. Yerel audit eski; ana publish workflow güncel maliyet/kapasite denetimini üretmiyor. |
| P2 | Tarihsel yanlılık tamamen kapanmadı | Delist olmuş şirketlerin bir kısmında fiyat/finansal backfill eksik; bazı eski finansal satırlarda gerçek KAP yayın tarihi yerine muhafazakâr fallback var. |
| P3 | Tekrarlanabilirlik ve statik kalite kapıları eksik | Dependency lock yok; CI minimum sürüm sınırlarından güncel paketleri kuruyor. `ruff`, `mypy`, `pip-audit` ve Dependabot kurulmamış. Mevcut 118 paket uyum kontrolünü geçiyor. |
| P3 | Bilişsel yük birkaç büyük modülde toplanmış | Repo yaklaşık 2 MB tracked kaynak ve 188 dosya ile kontrol edilebilir boyutta; asıl sorun `cli.py` 2300+, `backtest/engine.py` 2000+, `read_service.py` 1800+ ve `selector.py` 1600+ satırlık odak noktalarıdır. Körlemesine kod silmek doğru ilk adım değildir. |

Mevcut olumlu taban:

- `778` test toplanıyor; son tam doğrulamada hepsi geçmişti.
- PWA JavaScript sözdizimi ve statik kontrolü geçiyor.
- Düzeltilmiş TTM/PIT ve corporate-action hattı çalışıyor.
- Güncel `max_non_core=1`, 2024-2026 test döneminde eski production
  varyantından daha iyi alfa verdi; 8 hisseli varyant aynı dönemde yaklaşık
  `-11.24` puan alfa üretti. Bu nedenle sırf çeşitlendirme için hemen 8/10
  hisseye geçilmemelidir.
- Tracked Markdown bağlamı 12 dosya, yaklaşık 41 KB'dir; doküman tarafı şu an
  aşırı büyük değildir. AI bağlamı `AGENTS.md` üzerinden yönlendirilmelidir.

## Aşama 0 — Güvenli çalışma zemini

**Kota:** Düşük
**Risk:** Düşük
**Hedef:** Sonraki araştırma ve testlerin kanıt dosyalarını bozmamasını sağlamak.

Kapsam:

1. Silinmiş upstream'i olan `agent/dead-code-cleanup` branch'inde geliştirmeye
   devam etme; `origin/main` tabanlı yeni branch aç.
2. Kullanıcının `docs/FABLE5_BULGULARI_2026-07-09.md` yerel değişikliğini aynen
   koru.
3. Backtest testlerinde export'u varsayılan olarak kapat veya her çağrıyı
   `tmp_path` içine yönlendir.
4. Production araştırma artifact'lerinin test öncesi/sonrası değişmediğini
   doğrulayan regresyon testi ekle.

Kabul kapısı:

- 778 test geçer.
- `data/output` sentinel/checksum'ları test koşusunda değişmez.
- Feed, skor, selector veya backtest sonucu değiştirilmez.
- Tek revert PR ile tamamen geri alınabilir.

## Aşama 1 — Zamanında yayın ve bildirim

**Kota:** Orta; üç mikro PR'a bölünür
**Risk:** Orta
**Hedef:** Doğru portföyün ve uyarıların kullanıcıya zamanında ulaşması.

### 1A — Feed sonrası kesin bildirim

- Rotasyon özetini başarılı feed publish adımına bağla.
- Ayrı cron'un yeni feed'i görüp görmemesine güvenme.
- Başarılı publish, Telegram başarı/başarısızlık durumunu machine-readable sağlık
  özetine yazsın.

### 1B — Hafif çalışma payload'ları

- Ana publish sırasında küçük `active_tickers.json` ve `alert_payload.json`
  üret.
- Canlı fiyat ve monitor işleri 716-770 MB runtime DB restore etmesin.
- Canlı fiyat isteğini yakın zamanda fiyatı olan işlem gören paylarla sınırla;
  açık pozisyonlar her durumda dahil edilsin.

### 1C — Güvenilir tetikleme

- Kesin saat gerektiren işleri GitHub cron'a tek başına bırakma.
- Bu mikro aşama başında, GitHub workflow dispatch çağırabilen harici güvenilir
  scheduler seçeneği ve maliyeti kullanıcıyla kararlaştırılır.
- Stop/target kontrolünü seans sonuna kadar kapsayacak sıklığa getir.

Kabul kapısı:

- Beş ardışık işlem gününde sabah feed'i İstanbul `09:50` öncesinde hazır.
- Rotasyon Telegram mesajı başarılı publish'ten en geç 5 dakika sonra gelir.
- Seans içinde açık pozisyon quote yaşı en fazla 45 dakika olur.
- Stop/target uyarı gecikmesi en fazla 35 dakika ve kapanış slotu kapsanır.
- Canlı fiyat/monitor workflow'larında büyük runtime DB restore edilmez.

## Aşama 2 — Gerçek fill ve ayarlanabilir maliyet profili

**Kota:** Orta; iki küçük PR
**Risk:** Orta
**Hedef:** Kullanıcının gerçek kâr/zararını model referansından ayırmak ve Midas
için sıfır maliyeti doğru varsayılan yapmak.

Kapsam:

- Her pozisyon için değişmez kimlik, `model_entry_ref`, otomatik icra referansı,
  opsiyonel `actual_entry_fill` ve `actual_exit_fill` ayrımı.
- Varsayılan broker profili: alış `%0`, satış `%0`, diğer ücret `%0`.
- Oranlar ayarlanabilir; hesap motoru her işlem için brüt ve net sonucu birlikte
  üretir.
- PWA'da Brüt/Net switch'i ve gerçek fill düzenleme alanı.
- Kişisel fill public feed'e yazılmaz. İlk sürüm cihazda IndexedDB içinde tutulur;
  yedek dışa/içe aktarma eklenir. Cihazlar arası private sync ayrı kullanıcı
  kararıdır.
- Model NAV, executable-reference NAV ve kişisel NAV ayrı tutulur; sonradan
  düzenleme ham model kaydını değiştirmez.

Kabul kapısı:

- Gerçek fill model referansından farklı girildiğinde açık pozisyon ve History
  kâr/zararı gerçek fill üzerinden hesaplanır.
- Model referansı ekranda ve audit kaydında korunur.
- Midas `%0` profilinde brüt ve net sonuç aynıdır.
- Test maliyet profili açıldığında net sonuç, alış/satış/diğer ücretler kadar
  azalır; switch geçmiş satırları yeniden yazmaz.
- Kişisel fill snapshot güncellemesinde kaybolmaz ve public artifact'e sızmaz.

## Aşama 3 — Canlı sinyal ve backtest icra paritesi

**Kota:** Orta/Yüksek; önce shadow, sonra geçiş
**Risk:** Yüksek
**Hedef:** Canlı yatırım kararını backtestte birebir aynı bilgi ve fill zamanı ile
test etmek.

Kararlaştırılan sözleşme:

- `T-1` tamamlanmış seans verisiyle `T` sabahı sinyal.
- Kullanıcıya `T` açılışından önce yayın.
- Backtest ve forward kayıtta `T` açılışı/ilk icra edilebilir quote ile fill.
- Bu sözleşmedeki `T`, emrin verildiği ve hissenin alınabildiği aynı gündür;
  takasın T+1/T+2 tamamlanması ayrı konudur.

Kapsam:

1. `signal_as_of`, `published_at`, `execution_date` ve
   `execution_price_source` kavramlarını ayır.
2. Selector'ın görebileceği son veri tarihini açıkça sabitle.
3. Backtest skor tarihi/fill tarihini aynı sözleşmeye taşı.
4. Önce iki sistemi yan yana üreten shadow rapor oluştur; sonuç farkını kaydet.
5. PWA'da model referansı ile gerçek işlem referansını ayrı etiketle.

Kabul kapısı:

- Seçilmiş tarih örneklerinde canlı selector ve backtest aynı bilgi kesitiyle
  aynı aday listesini üretir.
- Fill günü ve fiyat kaynağı otomatik test edilir; future-data erişimi yoktur.
- Eski ve yeni 8 yıllık sonuç, değişimin kaynağıyla birlikte saklanır.
- Kullanıcı shadow karşılaştırmasını görmeden production switch yapılmaz.

## Aşama 4 — Maliyet, kapasite ve güncel investor-grade audit

**Kota:** Orta
**Risk:** Düşük; production modeli değiştirmez
**Hedef:** Stratejinin hesap büyüklüğü ve gerçek maliyet altında hâlâ anlamlı
avantaj üretip üretmediğini ölçmek.

Kapsam:

1. Aşama başında kullanıcıdan yaklaşık portföy büyüklüğü ve broker komisyonu
   alınır. Güncel Midas canlı profili kullanıcı değiştirene kadar `%0`'dır.
2. `%0.75 / %1.0 / %1.5` round-trip stresleri, likidite etkisi ve idle-cash
   getirisi aynı audit'te raporlanır.
3. Full dönem, yakın OOS, drawdown, TÜFE, USD ve gram altın birlikte gösterilir.
4. Pahalı audit günlük feed'i yavaşlatmaz; haftalık veya manuel workflow compact
   JSON üretir, ana feed son doğrulanmış sonucu yayınlar.

Kabul kapısı:

- Maliyet ve hesap büyüklüğü manifest/PWA üzerinde kaynaklı ve tarihli görünür.
- Audit eskiyse `güncel` gibi gösterilmez.
- Kapasiteyi aşan pozisyon adayları raporda işaretlenir.
- Model tuning'e geçmeden önce hangi maliyet eşiğinde alfa kaybolduğu nettir.

## Aşama 5 — Veri evreni ve tarihsel yanlılık

### 5A — İşlem gören pay kimliği

**Kota:** Düşük/Orta
**Risk:** Orta

- `KAP ihraççısı`, `borsada işlem gören pay` ve `aktif yatırım evreni`
  kavramlarını ayrı alan/kurallara taşı.
- KAP-only kurumları silme; audit için sakla, fakat fiyat/score/live isteklerine
  otomatik katma.
- Mevcut beş pozisyon ve BIST100 evreninin migration öncesi/sonrası aynı kaldığını
  doğrula.

### 5B — Delisted ve publication-date coverage

**Kota:** Yüksek; ayrı goal
**Risk:** Yüksek

- Delist olmuş şirketlerin tarihsel fiyat ve finansal backfill'ini tamamla.
- Legacy fallback yayın tarihlerini gerçek KAP timestamp'leriyle yükselt.
- Otomatik corporate-action tespitini mümkün olan resmi olay kayıtlarıyla
  uzlaştır; ham close audit izini koru.

Kabul kapısı:

- Coverage yüzdeleri ve eksik ticker listesi machine-readable artifact olur.
- Backtest yalnız bilinen yayın tarihini kullanır.
- Survivorship/publication fallback oranı her audit'te görünür.
- Backfill tamamlanmadan “tam bias-free” iddiası kullanılmaz.

## Aşama 6 — Model araştırması ve kontrollü terfi

**Kota:** Yüksek; ayrı goal
**Risk:** Yüksek
**Hedef:** Üretimi kurcalamadan gerçekten daha dayanıklı strateji adayı bulmak.

Araştırma sırası:

1. 2 haftalık cadence, trailing stop, incumbent eşiği, sektör/non-core sınırı ve
   pozisyon sayısı çevresinde küçük perturbation grid'i.
2. Purged walk-forward ve 2024-2026 yakın OOS değerlendirmesi.
3. Maliyet/kapasite stresleri ve rejim ayrımı.
4. Cash/regime sinyalini ayrı aday olarak test et.
5. Faktör IC bulgusunu tek başına ağırlık değişimi için kullanma; portföy seviyesi
   net sonucu zorunlu tut.

Terfi kapısı:

- Aday full dönem ve yakın OOS'ta maliyet sonrası production'dan daha iyi olmalı.
- Max drawdown/risk bütçesi kötüleşmemeli.
- Küçük parametre değişimlerinde sonuç çökmemeli.
- En az birden fazla forward rebalance boyunca shadow izlenmeli.
- Her aday için değişmeyen config, commit SHA ve artifact hash'i saklanmalı.

Bugünkü karar: 8 hisseli sleeve yakın OOS'ta negatif alfa verdiği için production
adayı değildir. İlk hafta kaybına bakarak faktör veya hisse sayısı değiştirilmez.

## Aşama 7 — PWA karar ekranını hafifletme

**Kota:** Orta
**Risk:** Orta
**Hedef:** Karar ekranını fiyat geçmişi araştırma verisinden ayırmak.

Kapsam:

- Home/pozisyon/manifest/NAV için küçük karar snapshot'ı.
- 730 günlük fiyat geçmişi ve detay araştırma tablolarını Details/History açılınca
  lazy-load edilen ikinci pakete taşı.
- Offline cache, SHA doğrulama ve son geçerli snapshot fallback'ini koru.

Kabul kapısı:

- İlk karar paketi sıkıştırılmış `<=2 MB` hedefini karşılar.
- Home ekranı fiyat geçmişi paketini beklemeden açılır.
- Mevcut şirket detayları ve grafikler lazy-load sonrası aynı veriyi gösterir.
- PWA statik kontrolleri ve offline senaryoları geçer.

## Aşama 8 — Tekrarlanabilirlik ve kontrollü modularizasyon

**Kota:** Her alt adım düşük/orta; sürekli bakım
**Risk:** Düşük/Orta

Sıra:

1. Python `3.12` için dependency lock/constraints ve CI lock doğrulaması.
2. Dependency audit ve kontrollü otomatik güncelleme.
3. Önce mevcut kodu bozmayan `ruff`; sonra seçilmiş modüllerde kademeli type
   checking.
4. Karakterizasyon testlerinden sonra yalnız bir seam/PR olacak biçimde:
   - CLI komut modülleri,
   - backtest execution/fill ile raporlama/audit,
   - selector aday üretimi ile kısıt/persistence,
   - snapshot schema/build/validation,
   - read-service domain'leri.

Silme kuralı:

- Import, entrypoint, workflow, test ve tarihsel kullanım kanıtı olmadan kod
  silinmez.
- Her davranışsız refactor'da feed schema, seçilen portföy, NAV ve backtest özet
  hash'leri önce/sonra karşılaştırılır.
- Mevcut compact doküman seti büyütülmez; kalıcı bilgi kanonik dosyaya eklenir,
  oturum günlüğü çoğaltılmaz.

## Uygulama durum günlüğü

### 2026-07-13 — Aşama 0 tamamlandı

- `origin/main` tabanlı `codex/phase-0-test-isolation` branch'i açıldı; kullanıcının
  Fable belgesi değişikliği stage edilmeden korundu.
- Varsayılan export klasörüne yazan dört sentetik backtest testi `tmp_path` içine
  yönlendirildi.
- Repo-geneli pytest guard'ı, yedi kanonik backtest artifact'ini test oturumu
  öncesi/sonrası byte düzeyinde karşılaştırıyor; değişiklik olursa dosyayı geri
  yüklüyor ve testi fail ediyor.
- Hedefli sonuç: `13 passed`; tam sonuç: `778 passed, 1 warning`.
- Her iki koşuda da korunan production artifact'lerinde checksum değişikliği yok.
- Model, selector, runtime DB, feed ve production workflow davranışı değişmedi.

### 2026-07-13 — Gerçek fill, pre-open sözleşme ve operasyon paketi

- PWA kişisel portföy katmanı production'a alındı: Midas alış/satış maliyeti
  varsayılan `%0`, Brüt/Net switch'i, ayarlanabilir maliyetler, cihazda saklanan
  gerçek alış/satış fill'leri ve dışa/içe aktarma.
- Model referans fiyatı değişmeden korunuyor; kişisel açık pozisyon ve History
  getirisi gerçek fill varsa onu kullanıyor. ASELS regresyonu tarayıcıda
  `model 370 / gerçek 355 / son 355,50 = +%0,14` olarak doğrulandı.
- AL/SAT işlem kartından hem alış hem satış fill'i düzenlenebiliyor. Gelecek
  tarihli staged rotasyonlar effective gün gelmeden History'de tamamlanmış
  işlem sayılmıyor.
- Backend'de `signal_date (T-1)` ile `selection_date (T)` ayrıldı; snapshot
  sürümü 13'e çıkarıldı. `prepare-portfolio`, hafta sonu/tatil güvenli son
  tamamlanmış seansı buluyor; ilk schema-13 yayında mevcut rotasyonun eksik
  sinyal alanlarını fiyatları değiştirmeden damgalıyor. PWA bugün/yarın işlem
  kartını gösteriyor.
- Canlı fiyat workflow'u büyük runtime DB yerine yayınlanan compact ticker
  sözleşmesini kullanıyor. KAP-only/fiyatsız kayıtlar audit için saklanıp
  canlı istek ve günlük skordan çıkarılıyor; açık pozisyonlar daima korunuyor.
- Telegram monitor başarılı feed workflow'una bağlandı. Haftalık, günlük feed'i
  yavaşlatmayan investor-grade audit workflow'u eklendi; 8 günden eski audit
  güncel diye yayınlanmıyor.
- Doğrulama: backend `788 passed`; PWA sözdizimi, kişisel hesap, statik ve
  tarayıcı akışları geçti. Uzun eski/yeni execution karşılaştırmasının sonucu
  `MODEL_AND_BACKTEST.md` içinde saklanır.
- İlk shadow sonuç cache kapısından geçmedi (`104/2` stale tarih) ve açıkça
  reddedildi. Zorunlu rebuild sonrası full dönem ve 5 yıllık temiz koşuların
  ikisinde de stale sayı `0/0`; yeni contract getiri/Sharpe kapılarını geçti.
  Beş yıllık MaxDD'nin eski mode göre `3,2063` puan kötü olduğu ayrıca kaydedildi.

| Aşama | Durum | Yapıldı | Kaldı | Kanıt / geri alma |
|---|---|---|---|---|
| 0 | TAMAMLANDI | Test export izolasyonu ve repo-geneli artifact guard | Yok | Backend PR #61; revert PR ile geri alınır |
| 1 | KISMEN TAMAMLANDI | Feed sonrası monitor, Pazar retry, compact live ticker payload | Harici SLA scheduler ve compact monitor payload ayrı operasyon kararı | Workflow testleri + 788 tam test |
| 2 | TAMAMLANDI | Midas `%0`, Brüt/Net, gerçek alış/satış fill'i, History ve yedek | Private cihazlar arası sync isteğe bağlı | Feed PR #3/#4; tarayıcı ASELS 355 regresyonu |
| 3 | TAMAMLANDI (YAYINA HAZIR) | `T-1` sinyal / `T` açılış sözleşmesi, schema 13, full + 5Y temiz karşılaştırma | PR merge ve production feed doğrulaması | Backend PR #62; stale `0/0`; 46 hedefli + 788 tam test |
| 4 | KISMEN TAMAMLANDI | Haftalık same-day audit, stale gate, kişisel maliyet profili | Portföy büyüklüğüne bağlı kapasite profili | Investor audit workflow + manifest gate |
| 5 | 5A TAMAMLANDI | KAP-only kayıtlar korunup tradable score/live evreninden ayrıldı | 5B delisted/publication backfill ayrı yüksek-kota goal | Tradable universe regresyon testleri |
| 6 | BEKLEMEDE | Mevcut A/B sonuçları kaydedildi | Önce 0-5 kapıları | Henüz kod/PR yok |
| 7 | PLANLANDI | Snapshot boyut kaynağı ölçüldü | İki paketli PWA | Henüz kod/PR yok |
| 8 | BEKLEMEDE | Monolit ve tool açıkları ölçüldü | Aşamalı bakım | Henüz kod/PR yok |

## İlk önerilen iş

**Aşama 0** ile başla. Düşük kota ve düşük riskle testlerin araştırma kanıtlarını
bozmasını engeller; sonraki bütün model/backtest çalışmalarının güvenilir zeminidir.
Bu aşama bitince burada durulur. Kullanıcının önceliği nedeniyle sonraki fonksiyonel
dilim **Aşama 2 gerçek fill + maliyet profili** olacaktır; yayın zamanlaması Aşama
1 mikro işleriyle paralel olmayan ayrı bir PR olarak ele alınır.
