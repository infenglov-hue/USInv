# ABD Piyasası Versiyonu — Plan

Tarih: **2026-07-17**. Durum: **Faz 0 bekliyor — henüz kod yok.** Bu belge bir
araştırma/plan notudur; production iddiası içermez. Fiyat ve API bilgileri
2026-07-17 web araştırmasıyla doğrulanmıştır (3 paralel araştırma, 138 kaynak
sorgusu); satın alma öncesi yeniden doğrulanmalıdır.

## Amaç ve sınırlar

1. **Metodoloji testi:** Aynı disiplin (PIT, walk-forward, rejim farkındalığı,
   fragilite ölçümü) ikinci ve bambaşka bir piyasada da çalışıyor mu? Çalışıyorsa
   BIST sonucunun şans olmadığına dair en güçlü kanıt; çalışmıyorsa o da bilgidir.
2. **TRY-dışı çeşitlendirme** için altyapı hazırlığı.
3. **KATI KURAL:** Bu proje BIST üretim sistemine dokunmaz. BIST forward kanıt
   penceresi (2026-07-13'ten itibaren) bu proje bahanesiyle kirletilmez; iki
   sistem ayrı veri tabanı, ayrı state ve ayrı yayın hattı kullanır.

## Dürüst beklenti çerçevesi

- ABD, BIST'ten çok daha verimli bir piyasadır. Yayınlanmış faktör sinyalleri
  (Piotroski, Magic Formula, değer ekranları) orada onlarca yıldır bilinir ve
  büyük ölçüde fiyatlanmıştır; yayın sonrası faktör primi erimesi literatürde
  belgelidir. Alfa beklentisi BIST'ten mütevazı tutulur.
- **BIST ağırlıkları taşınmaz.** A4 dersi: faktör haritası piyasaya özgüdür
  (BIST'te değer hâkim, momentum tutma-horizonunda negatifti). ABD ağırlıkları
  ABD verisi üzerinde sıfırdan atribüsyonla bulunur.
- Kapasite argümanı ABD'de de geçerlidir (kurumsal paranın giremediği
  mikro-cap'ler), ama o evrenin çöp riski yüksektir: shell şirketler, SPAC
  artıkları, seyreltme makineleri, pump-and-dump. BIST'te görsel kalan
  `red_flags`'in ABD karşılığı **seçimi kapılayan sert filtre** olmak zorundadır.
- Doğal benchmark çıtası yüksektir: strateji küçük/mikro-cap'te avlanacaksa ana
  kıyas Russell 2000 toplam getiri; SPY ikincil olarak raporlanır. "Endeks fonu
  almak" her zaman rapor edilen alternatiftir.

## Veri katmanı (2026-07-17 araştırma sonucu)

### Temel veri: SEC EDGAR — ücretsiz, BIST'tekinden kaliteli PIT

- `data.sec.gov/api/xbrl/companyfacts/CIK##########.json`: şirket başına tüm
  raporlanmış XBRL kalemleri (form, `filed` tarihi, dönem, değer).
- `data.sec.gov/submissions/CIK##########.json`: dosyalama dizini,
  **`acceptanceDateTime` saniye hassasiyetinde** — PIT zaman damgasının altın
  standardı (BIST'te SPK son-tarih tahmini kullanıyoruz; burada gerçek zaman var).
- **Financial Statement Data Sets** (sec.gov/dera): 2009Q2'den beri çeyreklik
  ZIP'ler; `sub.txt` (dosyalama başına satır: form, dönem, FILED, ACCEPTED) +
  `num.txt` (ilk-dosyalandığı-haliyle her sayısal kalem). XBRL parse etmeden
  as-first-filed PIT deposu kurmanın kestirme yolu — **önerilen başlangıç**.
- Delist olmuş şirketler EDGAR'dan asla silinmez → temel veride survivorship
  problemi kökten çözülür. XBRL geçmişi fiilen ~15 yıl (2009-2011 zorunluluk
  geçişi; küçük şirketler en son).
- Kısıtlar: ~10 istek/sn, `User-Agent: isim email` başlığı zorunlu (yoksa 403).
  Zayıf nokta: delist isimlerde CIK→tarihsel ticker eşlemesi
  (`company_tickers.json` sadece güncel; `formerNames` alanından reconstruct).
- Mühendislik tahmini: sağlam otomatik PIT deposu için ~2-6 hafta (tag eşleme
  fallback zincirleri, 10-K/A restatement'ta ilk-değeri-koru kuralı, Q4 = FY − 3Q
  türetimi, TTM montajı).

### Fiyat verisi: bedavası survivorship-biased — tek gerçek masraf burası

| Kaynak | Maliyet | Not |
|---|---|---|
| **EODHD All World** | **$19.99/ay** | Önerilen başlangıç: 2000'den beri 11.000+ delist ABD hissesi dahil, split/temettü, raw+adjusted close, bulk endpoint, 100k çağrı/gün. Endeks üyelik geçmişi yok. Küçük delist isimlerde adjustment'ları örneklemle denetle. |
| Norgate Platinum | $630/yıl (~$52,5/ay) | Fiyat tarafında altın standart: delist dahil 1990'a kadar, endeks üyelik geçmişi (S&P 500 1957+, Russell 1990+), adjusted+unadjusted, resmi Python paketi. Windows updater gerektirir; aylık faturalama yok. |
| Sharadar Core US Bundle (Nasdaq Data Link) | login arkasında (~$69/ay ikinci-el referans, doğrulanmadı) | Tek pakette PIT fundamentals (SF1, 10.000+ delist, 1997+) + fiyat (SEP) + S&P 500 üyeliği 1957+. Ücretsiz Nasdaq hesabıyla fiyatı doğrula; uygunsa EDGAR mühendisliğine kısmen alternatif. |
| Alpaca Basic | $0 | Canlı işletim için: meşru ücretsiz API (IEX feed), corporate actions endpoint'i. Backtest için değil (2016+, delist belirsiz). |

- Yardımcı ücretsizler: Alpha Vantage `LISTING_STATUS` (tüm delist ticker listesi
  — hangi kaynağı alırsak alalım coverage denetim aracı); SimFin free (5 yıl,
  `PUBLISH_DATE`/`RESTATED_DATE` kolonlu — EDGAR pipeline çapraz doğrulaması);
  GitHub `fja05680/sp500` (S&P 500 üyelik geçmişi ~1996+, küçük hatalarla).
- **Master veri olarak kullanılmayacaklar:** yfinance/Stooq (delist isimler
  kaybolur = survivorship bias, sessiz back-adjustment değişimleri, ToS gri
  alanı — sadece prototip/sanity-check); IEX Cloud (Ağu 2024'te kapandı);
  Databento (equities geçmişi çok kısa); Finnhub (retail katmanı yok, $3.500/ay).

### Maliyet özeti

Doğrulama/backtest dönemi: **~$20/ay** (EODHD) + $0 (EDGAR) + $0 (Alpaca).
Canlıya geçince fiyat ihtiyacı aktif hisselerle sınırlanır; abonelik gözden
geçirilir. Opsiyonel yükseltmeler: Norgate ($630/yıl) veya Sharadar (fiyatı
doğrulanınca).

## Fazlar

Çalışma biçimi `DEVELOPMENT_PLAN.md` kurallarına tabidir: küçük PR'lar, kabul
kapıları, kapı geçilmeden sonraki faz başlamaz, geri alma revert PR ile.

### Faz 0 — Kararlar (kod yok, bir sayfalık tasarım)

1. **Evren:** öneri Russell 2000 + mikro-cap, OTC hariç; min fiyat/likidite
   filtresi; shell/SPAC/finansal-olmayan-raporlayan dışlama kuralları.
2. **Benchmark ve başarı kriteri:** öneri Russell 2000 TR ana, SPY ikincil;
   maliyet sonrası alfa + MaxDD bütçesi baştan yazılır.
3. **Repo stratejisi:** öneri aynı motor + `market` soyutlaması (fetcher,
   evren, takvim, maliyet profili piyasaya özgü; skorer/PIT/rotasyon/backtest
   çekirdeği ortak); ayrı DB ve state repo'su.
4. ABD'ye özgü kalemler: işlem takvimi (NYSE/Nasdaq tatilleri), ticker değişim
   zinciri, USD raporlama (D009'un ABD karşılığı: nominal USD + SPY-relatif;
   TÜFE deflatörü ABD'de ikincil).

### Faz 1 — Veri katmanı

- EDGAR PIT deposu: önce Financial Statement Data Sets ZIP yolu (DuckDB/SQLite),
  sonra companyfacts ile artımlı güncelleme; freshness gate'lerin ABD karşılığı.
- EODHD fiyat katmanı + corporate action doğrulaması (D006 mimarisi: ham close
  korunur, `adjusted_close` üretilir).
- Coverage denetimi: Alpha Vantage delist listesi + SimFin çapraz kontrol;
  eksik oranı machine-readable artifact olur (5B kuralının aynısı).

### Faz 2 — Faktör keşfi (ABD verisinde sıfırdan)

- Skorer başına IC/atribüsyon ABD evreninde ölçülür; BIST ağırlıkları referans
  bile alınmaz. IC tek başına ağırlık değişimi gerekçesi değildir (PR #30 dersi);
  portföy seviyesi net sonuç zorunlu.

### Faz 3 — Doğrulama

- Purged walk-forward (TRAIN/VAL/TEST), fragilite paneli, maliyet stresleri
  (mikro-cap spread'leri BIST'ten ağır — flat maliyet varsayımı yetmez),
  rejim ayrımı (ABD reel faiz/kredi koşulları).

### Faz 4 — Kağıt-forward penceresi

- BIST kuralının aynısı: ruleset donar, önceden yazılmış kabul kriterleriyle
  en az 6 rotasyon kağıt-forward izlenir. Para kararı bu belgeye ait değildir.

## BIST'ten taşınan dersler (zorunlu çeklist)

- D005: PIT sözleşmesi — skor girdisi yalnız o gün bilinebilir veriyi kullanır.
- D006: ham fiyat korunur, adjustment ayrı kolonda.
- D009: performans tek para birimiyle sunulmaz (ABD'de: nominal USD + benchmark-relatif).
- D011: beslenmeyen/coverage'sız girdi aktif özellik sayılmaz (red_flags dahil).
- PR #30 dersi: in-sample kazanan walk-forward kanıtı olmadan shiplenmez.
- Aşama 3 sözleşmesi: `T-1` tamamlanmış veri → `T` icra; backtest=canlı parite.
- Test izolasyonu (Aşama 0): araştırma artifact'leri test koşusunda değişmez.
