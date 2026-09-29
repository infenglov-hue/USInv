# Model and Backtest Contract

## Canlı karar modeli

- Ana strateji: `index_aware` ALPHA.
- Hedef: 5 açık pozisyon; stop/target sonrası boş slot sonraki rotasyona kadar
  nakitte kalabilir.
- Rotasyon: 29 Haziran 2026 çapasından başlayan **iki haftada bir Pazartesi**.
- Index kısıtları: en az 2 BIST100; en fazla 1 non-BIST100 (`max_non_bist100=1`).
- Sektör, finansal sleeve, likidite, free-float, risk ve korelasyon sınırları
  seçim sırasında uygulanır.
- Açık pozisyonlar incumbent kuralıyla korunabilir; yalnız ham top-5 sıralaması
  seçilmez.

## Skor bileşimi

Normal faaliyet şirketlerinde baz ALPHA ağırlıkları:

| Bileşen | Ağırlık |
|---|---:|
| Buffett kalite | 0,15 |
| Graham + DCF değer | 0,30 |
| Piotroski | 0,15 |
| Growth | 0,10 |
| Momentum | 0,25 |
| Technical | 0,05 |

Banka, holding, GYO ve sigorta ayrı sektör-model ağırlıkları kullanır.
Index-aware seçim, kompozite XU100 göreli güç ve teknik zamanlama ekler.

Pozitif reel faiz koşulunda etkin koşullu blok:

| Bileşen | Ağırlık |
|---|---:|
| Buffett kalite | 0,25 |
| Graham + DCF değer | 0,15 |
| Piotroski | 0,20 |
| Growth | 0,10 |
| Momentum | 0,25 |
| Technical | 0,05 |

Bu blok reel politika faizi `> 0` iken devreye girer ve koşul kalkınca baz
ağırlıklara döner. 2024–2026 gözlemi tasarımı etkilediği için saf OOS değildir;
forward holdout ayrı izlenir.

## Risk ve çıkış

- Pozisyonlar normal durumda eşit ağırlıklıdır.
- ATR tabanlı giriş stopu ve ratchet edilen trailing stop vardır.
- Stop/target pipeline tarafından uygulanır; kullanıcı broker işlemini kendisi
  yapar.
- Cash state'leri `NORMAL/CAUTION/DEFENSIVE/RISK_OFF` için sırasıyla
  `%0/%25/%50/%75` nakit hedefler.
- CDS coverage'i olmadığı için cash modelinin ağır makro stres bacağı eksiktir;
  `RISK_OFF` davranışı tam doğrulanmış koruma olarak anlatılmaz.
- DCF hedefi ve skor-implied hedef `target_source` ile ayrılır; DCF pahalı
  uyarısı hedefin kaynağını gizlemez.

## Backtest sözleşmesi

- Başlangıç: `2018-03-19`; mevcut verified seri 10 yıl değildir.
- Rebalance: iki haftalık.
- Varsayılan strateji: `index_aware`.
- Production kararında `signal_date=T-1` son tamamlanmış geniş-kapsamlı seanstır;
  `selection_date=T` portföyün geçerli ve işlem yapılabilir olduğu seanstır.
- Skorlar yalnız `signal_date` anında bilinen TTM/PIT girdilerinden oluşur.
- `same_day_open` backtest fill'i, T-1 kararından sonra T seansındaki ilk
  kullanılabilir düzeltilmiş açılıştır. Tatil/işlem kesintisinde ilk gerçek bar
  kullanılır; bar yoksa slot nakitte kalır.
- Giriş/çıkış, friction, stop/target ve cash varsayımları raporda açık olmalıdır.
- Benchmark final XU100 fiyatlarından üretilir.
- Nominal TL yanında TÜFE, USD ve gram-altın deflated seri raporlanır.
- Aynı skor-cache sürümü olmadan iki deney kıyaslanmaz.

## Eski production kabul kaydı — T+1 next-open

Run: [29183406840](https://github.com/Somethinglikeu-hub/MobileInv/actions/runs/29183406840)

| Metrik | Sonuç |
|---|---:|
| Pencere | 2018-03-19 → 2026-06-29 |
| NAV noktası | 217 |
| Nominal toplam | `%1448,6766` |
| Nominal CAGR | `%39,2284` |
| Sharpe | `1,3358` |
| Max drawdown | `-%28,2972` |
| BIST100 | `%547,2053` |
| Nominal alpha | `+901,4712` puan |
| TÜFE-reel | `%27,1763` |
| USD | `%30,1732` |
| Gram-altın | `-%57,3846` |

Sonuç, nominal olarak güçlü ve BIST100'ün üzerindedir; gram-altın karşısında
negatiftir ve ileri dönem vaadi değildir.

Bu tablo Mayıs/Temmuz 2026'daki eski `next_open` sözleşmesinin sabit audit
kaydındadır. Canlı sistem T-1 veri/T açılış sözleşmesine geçirilmeden önce iki
mod aynı DB kopyası, aynı skor pipeline sürümü, iki haftalık cadence, trailing
stop ve `%0,4` model friction ile yan yana çalıştırılır. Güncel production kabul
tablosu bu shadow karşılaştırma tamamlandığında aşağıya eklenir; eski tablo
silinmez.

### Reddedilen ilk shadow koşusu

2026-07-13'teki ilk yan-yana koşu sonuç üretmesine rağmen kabul edilmedi:
`next_open` için `104`, `same_day_open` için `2` scoring tarihi
`2026-07-10-ttm-v1` pipeline sürümüyle tamamen güncel değildi. Araç artık
`rebuild_stale_scores=True` zorlar ve sonuçta tek stale tarih kalırsa fail eder.
Bu ilk yüzdeler model kararı veya production terfisi için kullanılmaz; ham rapor
yalnız disposable çalışma klasöründe `*_untrusted.json` olarak tutulur.

## Kabul edilen execution karşılaştırması — 2018–2026

Tarih: `2026-07-13`; pencere `2018-03-19 → 2026-07-13`; `218` NAV noktası;
iki haftalık cadence; aynı `2026-07-10-ttm-v1` score pipeline; `%0,4` model
round-trip friction; aynı trailing/selector/config. Her iki koşuda da
`stale_score_cache_dates=0`.

| Metrik | Eski `next_open` | Yeni `T-1 → T open` |
|---|---:|---:|
| Nominal toplam | `%1725,7042` | `%4099,9982` |
| CAGR | `%41,7941` | `%56,7323` |
| BIST100 | `%547,8044` | `%547,8044` |
| Nominal alpha | `+1177,8998` puan | `+3552,1938` puan |
| Sharpe | `1,4340` | `1,7349` |
| Max drawdown | `-%22,9119` | `-%22,7896` |
| En kötü dönem | `-%16,0627` | `-%11,0935` |
| Rolling 52w BIST'i geçme | `%62,6506` | `%98,7952` |
| TÜFE-reel toplam | `%49,9256` | `%244,9010` |
| USD toplam | `%52,2359` | `%250,1980` |
| Gram-altın toplam | Veri sağlayıcı ilk case'te başarısız | `%13,5786` |

Yeni sözleşme aynı günün kapanışını görmez: Cuma tamamlanmış veriyle karar
verir ve Pazartesi ilk kullanılabilir düzeltilmiş açılışta fill eder. Sonuç eski
mode göre toplam getiride `+2374,2940` puan, CAGR'da `+14,9382` puan ve
Sharpe'ta `+0,3009` iyidir; max drawdown da kötüleşmemiştir. Bu nedenle canlı
zamanlama sözleşmesinin `same_day_open` olarak terfisi kabul edilmiştir.

Yüksek tarihsel getiri ileri dönem garantisi değildir. Özellikle delisted
coverage, publication-date fallback ve aynı tarihsel yol üzerinde parametre
seçimi riskleri aşağıdaki metodolojik sınırlarda aynen kalır.

## Temiz 5 yıllık doğrulama

Pencere `2021-07-12 → 2026-07-13`; `131` NAV noktası; `4,9829` yıl; iki
execution mode için de `stale_score_cache_dates=0`.

| Metrik | Eski `next_open` | Yeni `T-1 → T open` |
|---|---:|---:|
| Nominal toplam | `%1733,4014` | `%2334,1977` |
| CAGR | `%79,2748` | `%89,7682` |
| BIST100 | `%462,6428` | `%462,6428` |
| Nominal alpha | `+1270,7586` puan | `+1871,5550` puan |
| Sharpe | `2,2066` | `2,2813` |
| Max drawdown | `-%19,5833` | `-%22,7896` |
| En kötü dönem | `-%9,6310` | `-%11,0371` |
| TÜFE-reel toplam | `%149,3902` | `%231,1141` |
| USD toplam | `%232,8729` | `%341,9593` |
| Gram-altın toplam | `%48,5009` | `%97,0789` |

Beş yıllık pencerede yeni contract toplam getiride `+600,7963` puan,
CAGR'da `+10,4934` puan ve Sharpe'ta `+0,0747` daha iyidir; buna karşılık max
drawdown `3,2063` puan ve en kötü iki haftalık dönem `1,4061` puan daha
kötüdür. Terfi gerekçesi “her risk metriğinde üstünlük” değil, canlı kararın
gerçek uygulanabilir zamanlamasıyla birebir parite ve bu paritenin getiri/Sharpe
kapılarını geçmesidir. Risk bütçesi ve beş-hisse yoğunlaşma riski korunur.

## Bilinen metodolojik sınırlar

1. Delisted geçmiş coverage'i eksik olduğundan kalıntı survivorship olabilir.
2. Bazı legacy publication date'leri gerçek KAP timestamp'i değil fallback'tir.
3. Parametre araştırmasının önemli bölümü aynı tarihsel yol üzerinde yapıldı;
   çoklu karşılaştırma ve overfit riski vardır.
4. Beş hisse ve eşit ağırlık gap/tek-isim kuyruk riskini yoğunlaştırır.
5. Likidite-ölçekli market impact ve kapasite, varsayılan manşet metriğine tam
   yansımaz.
6. Cash timing/hedge katkısının ileri rejimlerde tekrarlanacağı garanti değildir.
7. Resmî TÜFE deflatörü alternatif yaşam maliyeti ölçümleriyle aynı değildir.

## Model değişikliği kabul kapısı

Bir ağırlık, filtre, portföy büyüklüğü, stop, cadence veya cash kuralı ancak:

1. mevcut corrected cache üzerinde tek değişkenli aday olarak tanımlanırsa,
2. train/validate/test veya purged walk-forward raporu varsa,
3. maliyet sonrası alpha, drawdown, turnover ve kuyruk riski birlikte iyiyse,
4. küçük parametre perturbasyonunda sonuç uçurumdan düşmüyorsa,
5. nominal + BIST100 + TÜFE + USD + altın kıyasları korunuyorsa,
6. forward/shadow ölçüm planı önceden yazıldıysa

production adayı olabilir. Sadece full-period getiriyi artırması yeterli değildir.
