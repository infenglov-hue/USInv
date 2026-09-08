# USInv AI — Yüksek Riskli & Yüksek Getirili Fon/ETF Otonom Karar Motoru

Bu alt proje (`ai/`), ABD piyasasında işlem gören **yatırım yapılabilir, yüksek riskli ve asimetrik yüksek getiri potansiyeline sahip fonlar ve ETF'ler** (2x/3x kaldıraçlı teknoloji/yarı iletken fonları, tematik yıkıcı inovasyon, kripto spot/türev fonları, yüksek betalı biyoteknoloji ve taktik koruma araçları) için geliştirilmiş **otonom bir karar ve araştırma motorudur**.

Sistem yalnızca statik göstergelere bakmak yerine, **kendi veri toplama kararlarını veren çok aşamalı bir otonom agent hattı** işletir:
1. **Evren Taraması & Likidite Filtresi:** İşlem hacmi (\$5M+ günlük dolar hacmi) ve fiyat (\$5+) eşikleriyle likit olmayan tuzakları eler.
2. **Kantitatif Profilleme:** Momentumu (5G, 20G, 60G), volatiliteyi, Sortino/Sharpe oranlarını, düşüş derinliğini (drawdown), RSI ve EMA/SMA trend dizilimini hesaplar.
3. **Makroekonomik Rejim Analizi:** Çoklu varlık vekil göstergelerini (`SPY`, `QQQ`, `TLT`, `GLD`, `UUP`, `UVXY`, `HYG`, `SMH`) inceleyerek piyasanın Risk-On, Risk-Off, Çalkantılı Rotasyon (Choppy Rotation) veya Volatilite Şoku evresinde olduğunu tespit eder.
4. **Haber & Katalizör İstihbaratı:** Aday varlıklar ve onların temel bileşenleri (`NVDA`, `TSLA`, `MSFT`, `COIN` vb.) için canlı haberleri tarar, boğa katalizörlerini ve ayı risklerini ayrıştırır.
5. **Sentez & Taktik İşlem Planı ("Bak Şunu Al"):** Alım/Satım/Nötr/Koruma aksiyonu, 0-100 güven puanı, somut giriş aralığı, 1:2+ risk/ödül oranlı kar alma seviyeleri (Hedef 1 ve Hedef 2), dinamik ATR zarar kes (stop-loss) ve kaldıraç erimesine karşı azami taşıma vadesi belirler.

---

## Mimari Şema

```
ai/
├── config/
│   ├── settings.yaml              # Eşikler, risk sınırları, puanlama ağırlıkları
│   ├── universe.yaml              # Kategorize edilmiş yüksek riskli ETF evreni
│   └── macro_indicators.yaml      # Makro rejim belirleme parametreleri
├── core/
│   ├── models.py                  # Pydantic veri modelleri (BarData, TradePlan, MacroRegime)
│   ├── metrics.py                 # İstatistiksel metrikler (Sortino, Volatilite, RSI, Beta)
│   └── risk.py                    # Kaldıraç erimesi, ATR stop-loss, portföy payı sınırları
├── tools/
│   ├── alpaca_client.py           # Kimlik doğrulamalı Alpaca REST API istemcisi (Barlar, Haberler)
│   ├── screener_tool.py           # Evren tarama ve kantitatif filtreleme aracı
│   ├── technical_tool.py          # Bireysel varlık teknik ve momentum analiz aracı
│   ├── macro_tool.py              # Makroekonomik rejim tespit aracı
│   └── news_tool.py               # Haber akışı ve katalizör ayrıştırma aracı
├── agent/
│   ├── decision_engine.py         # Kantitatif + Makro + Haber sentezi ve karar üretici
│   └── orchestrator.py            # Otonom araştırma adımlarını yöneten orkestratör
├── storage/
│   └── db.py                      # DuckDB kalıcılık katmanı (geçmiş öneriler ve makro anlık görüntüler)
├── reports/
│   ├── generator.py               # Markdown ve JSON yatırım dosyaları oluşturucu
│   └── output/                    # Üretilen detaylı yatırım raporları
├── tests/                         # Kapsamlı pytest test paketi
└── cli.py                         # Kullanıcı dostu terminal arayüzü
```

---

## Kurulum ve Çalıştırma

Sistem, `.env` dosyasındaki mevcut `ALPACA_KEY_ID` ve `ALPACA_SECRET_KEY` anahtarlarını otomatik olarak kullanır.

### 1. Canlı Makroekonomik Rejim Analizi
Piyasa ortamını ve risk iştahını ölçmek için:
```powershell
python -m ai.cli macro
```

### 2. Otonom Yüksek Risk / Yüksek Getiri Taraması
Tüm evreni tarayıp makro rejimle uyumlu en güçlü adayları ve işlem planlarını listelemek için:
```powershell
python -m ai.cli scan --top 3
```

### 3. Tekil Fon / ETF Derinlemesine Analizi (Deep-Dive)
İstediğiniz herhangi bir fon veya hisse senedi için derinlemesine teknik, haber ve taktik plan çıkarmak için:
```powershell
python -m ai.cli deepdive SOXL
python -m ai.cli deepdive TQQQ
python -m ai.cli deepdive IBIT
```

### 4. Geçmiş Önerileri ve Kararları İnceleme
DuckDB üzerinde saklanan geçmiş tavsiyeleri listelemek için:
```powershell
python -m ai.cli history --limit 10
```

---

## Testler

Test paketini çalıştırmak için:
```powershell
pytest -q ai/tests
```

Tüm USInv projesi genelinde testleri çalıştırmak için:
```powershell
pytest -q
```

---

## Post-Mortem / Yaşanan Sorunlar ve Çözümleri

> [!NOTE]
> Bu bölüm, projenin geliştirilmesi sırasında karşılaşılan teknik engelleri, kök nedenlerini ve uygulanan mimari çözümleri belgeler. Gelecekte benzer problemlerin tekrar yaşanmasını engellemek amacıyla tutulmaktadır.

### 1. Windows Konsolunda Unicode Karakter Kodlama Hatası (`charmap` codec)
- **Sorun:** `ai/cli.py` çalıştırıldığında terminal çıktısında yer alan emoji (`🚀` vb.) karakterler Windows PowerShell/CMD varsayılan kod sayfasında (cp1252) `UnicodeEncodeError: 'charmap' codec can't encode character` hatası vererek komutun çökmesine neden oldu.
- **Kök Neden:** Windows konsolunun varsayılan stdout akış kodlaması UTF-8 yerine cp1252/cp850'dir.
- **Çözüm:** `ai/cli.py` başlangıcında `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` ve `sys.stderr.reconfigure(...)` uygulanarak konsol akışının her koşulda güvenli UTF-8'e zorlanması sağlandı, ayrıca banner simgelerinde ASCII uyumlu etiketler (`[*]`) tercih edildi.

### 2. Alpaca Veri API'sinde Boş Bar Listesi Dönmesi (`feed="iex"`)
- **Sorun:** Alpaca `/v2/stocks/bars` uç noktasına yapılan ilk sorgularda `status_code: 200` dönmesine rağmen barlar listesi boş (`[]`) gelmekteydi.
- **Kök Neden:** Alpaca varsayılan olarak SIP tam piyasa veri beslemesini sorgulamaktadır. Ücretsiz ve Paper API hesaplarında SIP verisi abonelik gerektirdiğinden boş sonuç döner. Ücretsiz hesaplar IEX beslemesine tam erişime sahiptir.
- **Çözüm:** `ai/tools/alpaca_client.py` içerisindeki tüm sorgu parametrelerine `feed="iex"` açıkça eklendi ve başlangıç tarihi varsayılan olarak 120-150 gün öncesine ayarlandı. Böylece tüm hisse ve ETF'lerin günlük OHLCV barları anında ve eksiksiz alınmaya başlandı.

### 3. Python 3.12 `datetime.utcnow()` Amortismanı (Deprecation)
- **Sorun:** Pydantic modellerinde ve karar motorunda `datetime.utcnow()` çağrıldığında Python 3.12 çalışma zamanı `DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version` uyarısı verdi.
- **Kök Neden:** Modern Python standartlarında naive UTC zamanı yerine timezone-aware zaman nesneleri (`datetime.now(timezone.utc)`) zorunlu hale gelmiştir.
- **Çözüm:** `models.py` ve `decision_engine.py` dosyalarında `default_factory=lambda: datetime.now(timezone.utc)` ve `datetime.now(timezone.utc)` kullanılarak tüm zaman damgaları timezone-aware UTC formatına geçirildi, uyarılar tamamen sıfırlandı.

### 4. Kaldıraçlı ETF'lerde Volatilite Erimesi (Leverage Decay) Tuzağı
- **Sorun:** 2x ve 3x kaldıraçlı fonlar (örneğin SOXL, TQQQ, TNA), yön doğru tahmin edilse bile yatay ve yüksek volatiliteli piyasalarda günlük sıfırlama (daily reset) nedeniyle negatif bileşik getiri erimesine maruz kalır. Klasik "al-ve-unut" yaklaşımı bu fonlarda sermayeyi sıfırlayabilir.
- **Çözüm:** `ai/core/risk.py` ve `DecisionEngine` içerisine katı kurallar entegre edildi:
  - 3x kaldıraçlı fonlar için azami taşıma süresi **15 iş günü** ile sınırlandırıldı.
  - Dinamik ATR bazlı zarar kes (stop-loss) ve azami %10-12 mutlak stop sınırı konuldu.
  - Risk-off veya yüksek volatilite şoku rejimlerinde kaldıraçlı boğa fonlarına alım tavsiyesi verilmesi engellendi; piyasa düşüşteyken yalnızca taktik ters/koruma (inverse hedge) fonlarına izin verildi.

### 5. Alpaca Çoklu-Sembol (Batch) Sorgularında Global Limit Açlığı (Starvation)
- **Sorun:** Çoklu sembol listesi (`symbols=A,B,C...`) ile `/v2/stocks/bars` sorgulandığında yalnızca ilk 1-2 sembole ait barlar dönüyor, diğer semboller boş (`[]`) kalıyordu.
- **Kök Neden:** Alpaca REST API'sindeki `limit` parametresi sembol başına değil, dönen **tüm sembollerin toplam bar sayısına** uygulanır. Varsayılan `limit=200` değeri 15 sembol istendiğinde ilk semboller tarafından hızla tüketilmektedir.
- **Çözüm:** `ai/tools/alpaca_client.py` içinde birden fazla sembol sorgulanırken limit değeri sembol sayısıyla dinamik olarak ölçeklendi (`min(10000, limit * len(symbol_list))`). Böylece taranan tüm fonların tam tarihsel barları eksiksiz çekilmeye başlandı.
