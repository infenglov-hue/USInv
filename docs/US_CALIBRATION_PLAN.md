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
