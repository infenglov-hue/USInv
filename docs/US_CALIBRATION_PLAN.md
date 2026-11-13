# US Picker kalibrasyon planı (önceden kayıt)

Tarih: 2026-09-29. Bu belge denemeler başlamadan yazılmıştır; sonuçlar
geldikçe **yalnızca "Sonuçlar" bölümü** eklenir, kurallar değiştirilmez.

## Başlangıç noktası

BIST Picker kuralları olduğu gibi (5 hisse, 2 hafta, V1 ağırlıkları, ATR stop +
%20-35 trailing) ABD'de 2017-01-09 → 2026-09-21: **+%44,4** (CAGR %3,9,
MaxDD −%35,1, Sharpe 0,3); SPY toplam getiri aynı dönemde **+%277**. Kaynak:
`~/.us-picker/backtest_2017.log`, skor sürümü `2026-09-29-us-v1`.

## Dönemler (sabit)

| Dönem | Tarih | Kullanım |
|---|---|---|
| TRAIN | 2017-01-09 → 2021-12-27 | aday üretimi ve eleme |
| VALIDATE | 2022-01-03 → 2023-12-25 | TRAIN'de geçen adayların teyidi |
| TEST | 2024-01-01 → 2026-09-21 | yalnız final aday için **bir kez** |

## Denenecek değişkenler (tek değişken, sonra en iyi birleşim)

1. Portföy boyutu: 5 / 8 / 10 (index kısıtı orantılı: en az %60 S&P 500).
2. Rotasyon: 2 / 4 hafta.
3. Stop: mevcut (ATR giriş %10-25 + trailing %20-35) / yalnız trailing %25-40 /
   stop yok.
4. Ağırlık setleri: BIST V1, BIST V2 bazı, momentum+kalite ağırlıklı
   (kalite 0,30, momentum 0,35, değer 0,15, Piotroski 0,10, teknik 0,10).
5. Nakit rejimi: açık / kapalı.

## Kabul kapısı (BIST Picker MODEL_AND_BACKTEST kapısının ABD hali)

Final aday ancak şunları birlikte sağlarsa canlı varsayılan olur:

- TRAIN ve VALIDATE'te maliyet (%0,4 gidiş-dönüş) sonrası SPY'a karşı pozitif
  alfa ve SPY'dan yüksek olmayan maksimum düşüş/SPY oranı ≤ 1,2.
- Komşu parametrelerde (±1 adım) sonucun uçurumdan düşmemesi.
- TEST'te maliyet sonrası alfa ≥ 0. TEST'te başarısızsa model **SPY'ı geçemiyor**
  kabul edilir ve kullanıcıya açıkça "endeks fonu" alternatifi söylenir.

Hiçbir TEST sonucu ağırlık/parametre seçimine geri beslenmez.

## Sonuçlar

### Tur 1 — yapı (TRAIN, BIST V1 ağırlıkları, %0,4 maliyet)

| Deneme | Toplam | SPY | CAGR | SPY CAGR | Sharpe | MaxDD | SPY MaxDD |
|---|---:|---:|---:|---:|---:|---:|---:|
| A 5 hisse / 2 hafta (BIST) | +%25,1 | +%113,1 | %4,6 | %16,5 | 0,36 | −%19,4 | −%28,6 |
| B 8 / 2 hafta | +%38,2 | +%113,1 | %6,8 | %16,5 | 0,50 | −%16,0 | −%28,6 |
| C 5 / 4 hafta | +%40,5 | +%113,5 | %7,2 | %16,7 | 0,55 | −%17,1 | −%28,6 |
| D 8 / 4 hafta | +%35,1 | +%113,5 | %6,3 | %16,7 | 0,49 | −%11,9 | −%28,6 |
| E 10 / 4 hafta | +%21,6 | +%113,5 | %4,1 | %16,7 | 0,35 | −%14,6 | −%28,6 |
| F 5 / 2 hafta, geniş stop | +%28,6 | +%113,1 | %5,2 | %16,5 | 0,39 | −%23,8 | −%28,6 |
| G 5 / 2 hafta, stop yok | +%25,5 | +%113,1 | %4,7 | %16,5 | 0,36 | −%22,4 | −%28,6 |
| H 8 / 4 hafta, stop yok | +%29,7 | +%113,5 | %5,4 | %16,7 | 0,38 | −%32,1 | −%28,6 |

Sonuç: yapısal ayarlar farkı kapatmıyor (hepsi SPY'ın yıllık ~10 puan
gerisinde). Stoplar düşüşü sınırlıyor, getiriyi değiştirmiyor. Sorun skorun
kendisinde → Tur 2 ağırlıkları dener. Kaynak: `~/.us-picker/calib/results.csv`.

### 10 Kasım 2026 — BIST V2 kuralları, ABD TRAIN

Sekiz pozisyon, en az altı S&P 500 üyesi, dört haftalık rotasyon, V2 baz ağırlıkları ve iki çıkış kırıcısı birlikte denendi. Nakit hedefi rotasyon günlerinde hesaplandı. Aday %0,4 gidiş-dönüş maliyeti içerir. SPY kıyası, toplam getiri serisinden mevcut modelin ek yıllık %0,5 gider kesintisini uygular.

| Ölçüt | V2 ABD adayı | SPY model kıyası |
|---|---:|---:|
| Toplam getiri | %30,94 | %113,51 |
| Yıllık bileşik getiri | %5,65 | %16,72 |
| Maksimum düşüş | −%15,94 | −%28,62 |

65 NAV noktası 23 Ocak 2017 ile 20 Aralık 2021 arasındadır. Eski skor cache'i kalan tarih sayısı sıfırdır. Aday daha küçük düşüş yaşadı, ancak maliyet sonrası getiri farkı −82,58 yüzde puan oldu. TRAIN kapısı geçilmedi. Bu aday için VALIDATE ve saklı TEST çalıştırılmadı. Canlı V1 ayarları korundu.

Koşu girdilerinin ve çıktılarının sağlama toplamları [kanıt kaydında](evidence/us-picker-v2-train-2026-11-10.json), tekrar çalıştırma komutu [US Picker kılavuzunda](US_PICKER.md#v2-araştırma-koşusu) bulunur. Çalışma verisindeki bugün listelenen şirketlere bağlı evren sınırlaması bu koşuda da geçerlidir.

### 13 Kasım 2026 — ABD hisse seçimi, TRAIN

2017-01-09 ile 2021-12-27 arasında 60 rotasyon. Aylık seçim sinyali önceki
XNYS kapanışından, portföy değerlendirmesi açılıştan açılışa. Alpaca'nın
temettü ve bölünme düzeltme oranı ham açılışa uygulanır. SPY aynı fiyat
verisi ve tarihlerle ölçülür. Strateji maliyeti, alınıp satılan tutarın
her yönü için ayrı uygulanır. SPY al-tut kıyasında ek gider kesintisi yoktur.

Sinyal tarihinde S&P 500 üyesi olan işletme şirketleri, iki haneli SIC
başına en fazla dört hisse, 20 seans ortalama günlük en az 20 milyon USD
işlem hacmi, en az 5 USD kapanış fiyatı ve şirket başına tek hisse sınıfı.
15 hisse eşit ağırlıkla alınır. Kalite için son 12 aylık aktif kârlılığı, değer
için split uyumlu son 12 aylık kazanç/fiyat oranı, momentum için son 21 seans dışarıda
bırakılarak 252 seanslık geçmiş getiri kullanılır. SEC yayın tarihi sinyal
tarihini aşamaz. Kalite, değer ve momentumun ayrı koşuları aynı evreni ve
kuralları kullanır. Fiyat stopu ve nakit zamanlaması bu deneyde kapalıdır.
[Fama–French kârlılık araştırması](https://www.sciencedirect.com/science/article/pii/S0304405X14002323) · [Kenneth French momentum tanımı](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/det_mom_factor.html)

| Aday | Toplam getiri | Yıllık bileşik | Maks. düşüş |
|---|---:|---:|---:|
| SPY toplam getiri | %126,29 | %17,88 | −%32,05 |
| Uygun evren, eşit ağırlık | %112,00 | %16,34 | −%36,47 |
| Kalite + değer + momentum | %96,01 | %14,52 | −%37,17 |
| Kalite | %158,07 | %21,05 | −%32,98 |
| Değer | %33,32 | %5,97 | −%56,67 |
| Momentum | %56,48 | %9,44 | −%37,58 |

Tabloda her alım ve satış yönü için 20 baz puan maliyet ve açılış
değerlerinden hesaplanan maksimum düşüş kullanılır. Kalite adayının
getirisi 40 baz puan/yön ile %149,80. Daha yüksek getiri görülen kalite
adayının maksimum düşüşü de SPY'dan yüksek. Evrene giren 652 farklı
tarihsel sembolden 499'u veri tabanında eşleşti. Her rotasyonda faktör
hesabı yapılabilen şirket sayısı 246–334. Tarihsel sembol eşleşmesi ve
kottan çıkan şirketlerin fiyatları eksik. Güncel SIC sınıfları kullanıldı.
Kesirli pozisyon ve tek açılış fiyatı yaklaşımı gerçek emirleri veya
nakit takasını modellemez. VALIDATE ve TEST çalıştırılmadı.

```powershell
python -m scripts_picker.us_equity_train <detached-us-picker-db> train-result.json
```

Özel veri tabanı sağlama toplamı ve maliyet senaryoları [koşu çıktısında](evidence/us-equity-train-2026-11-13.json).
