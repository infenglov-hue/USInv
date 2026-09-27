# CHATGPT_INSTRUCTIONS.md — USInv Proje Vizyonu ve Dönüşüm Yol Haritası

> **⚠️ BU DOSYAYI OKUYAN AI ASİSTANI (ChatGPT / Claude / Cursor / Codex vb.):**
> Bu belge, kullanıcının bu projeden asıl beklentisini, sistemin felsefesini ve projeyi nereye götürmeniz gerektiğini anlatan **en yetkili stratejik yönergedir**.
> Kullanıcı size *"projeye bak, devam et"* dediğinde, bu belgeyi ve kardeş proje `MobileInv`'ı temel alarak doğrudan işe başlamalısınız.

---

## 1. Ana Vizyon: "Amerikan Borsası İçin MobileInv (BIST Picker)"

Bu projenin (`USInv`) nihai hedefi, Türkiye borsası (BIST) için geliştirilmiş olan **MobileInv / BIST Picker** projesinin yatırım felsefesini, portföy yönetim mantığını ve sade kullanıcı deneyimini **ABD Borsalarına (NYSE / Nasdaq)** uyarlamaktır.

Mevcut repo (`USInv`), arka planda çok güçlü bir temel kurmuştur (SEC EDGAR point-in-time bilançoları, Alpaca broker motoru, otomatik GitHub Actions iş akışları, 623 adet geçen test). **ANCAK son kullanıcının istediği ürün tam olarak bu akademik/karmaşık yapı değildir.**

Kullanıcının istediği şey:
BIST Picker'ın Türkiye'de yaptığını (2 haftalık rotasyon, 5 konsantre hisse, rejim bazlı nakit koruması, Buffett/Graham/Piotroski/Momentum puanlaması, sade mobil PWA), ABD hisse senetleri için yapan **canlı, net ve yüksek getirili bir karar ve yönetim makinesidir.**

---

## 2. Kardeş Proje Referansı: `MobileInv` Nerede ve Ne Yapıyor?

Kullanıcının bilgisayarında aynı çalışma alanında kardeş proje bulunmaktadır:
- **Yerel Konum:** `c:\Users\kerem\OneDrive\Έγγραφα\Ai Projects\MobileInv` (veya `../MobileInv`)
- **Canlı Kullanıcı Ürünü:** [BIST Picker PWA](https://somethinglikeu-hub.github.io/MobileInv-feed/)
- **Kanonik Belgeleri:**
  - `MobileInv/docs/MODEL_AND_BACKTEST.md` (Portföy kuralları, skor ağırlıkları ve backtest sözleşmesi)
  - `MobileInv/docs/ARCHITECTURE.md` (Veri ve modül mimarisi)

### `MobileInv`'ın İncelenmesi Gereken Kritik Kodları:
1. **`MobileInv/bist_picker/portfolio/selector.py`**:
   - Portföyün kalbidir. 5 konsantre slot seçer.
   - **Incumbent Kuralı:** Açık pozisyondaki bir hisseyi çıkarmak için, yeni adayın onun puanından belirgin bir farkla (hold band buffer) üstün olması gerekir. Böylece gereksiz işlem ve komisyon kaybı engellenir.
2. **`MobileInv/bist_picker/portfolio/rotation.py`**:
   - Sabit 2 haftalık (Bi-weekly) Pazartesi rotasyon çapasını yönetir.
3. **`MobileInv/bist_picker/portfolio/exit_rules.py`**:
   - ATR dinamik giriş stopu ve yukarı kilitlenen (ratchet) trailing stop kuralları.
4. **`MobileInv/bist_picker/portfolio/cash_signal.py` & `macro_overlay.py`**:
   - Piyasa rejimine göre nakit hedefi: `NORMAL` (%0 nakit), `CAUTION` (%25), `DEFENSIVE` (%50), `RISK_OFF` (%75).
5. **`MobileInv/bist_picker/scoring/composer.py` & `factors/`**:
   - **Skor Bileşimi:**
     - Buffett Kalite (%15 - %25)
     - Graham + DCF Değer (%15 - %30)
     - Piotroski F-Score (%15 - %20)
     - Growth (%10)
     - Momentum (%25)
     - Technical (%5)
6. **`MobileInv/bist_picker/scoring/ai_analyst.py`**:
   - Her seçilen hisse için 2-3 cümlelik sade Türkçe yatırım tezi üretir: *"Neden bu hisseyi alıyoruz, güçlü yanları ve riskleri neler?"*.

---

## 3. USInv'de Mevcut Durum: Neleri Korumalı, Neleri Dönüştürmeliyiz?

### ✅ Korunacak Sağlam Altyapı (Sıfırdan Yazma, Çöpe Atma):
1. **SEC EDGAR & FSDS Veri Katmanı:** As-first-filed point-in-time bilanço motoru (`usinv/data/edgar/`).
2. **Alpaca Broker Entegrasyonu:** Gerçek/sanal işlem emir iletimi ve LOO (Limit-on-Open) yapısı (`usinv/broker/alpaca.py`).
3. **Takvim & Seans Modülü:** NYSE tatil, yarım gün ve seans hesaplamaları (`usinv/calendar.py`).
4. **Otomasyon (GitHub Actions):** `nightly-data.yml`, `decision.yml`, `fill-reconcile.yml`, `weekly-audit.yml`, `deploy-pwa.yml`.
5. **Kalite Altyapısı:** 623 adet geçen test ve Ruff lint sistemi.
6. **`ai/` Alt Sistemi:** Kaldıraçlı ve tematik ETF taktik tarama motoru (`ai/`).

### 🔄 Dönüştürülmesi Gerekenler (MobileInv Mantalitesine Geçiş):

| Konu | Mevcut USInv Hali | Hedeflenen MobileInv / BIST Picker Uyarlaması |
|---|---|---|
| **Pozisyon Sayısı** | 15 hisselik geniş sepet | **5 konsantre pozisyon** (en fazla 5-8 slot). BIST Picker gibi odaklı portföy. |
| **Rotasyon Sıklığı** | Aylık (4 haftalık) | **2 haftada bir (Bi-weekly)** Pazartesi açılışında rotasyon. |
| **Skor Bileşimi** | Salt akademik faktörler | **Buffett (Kalite) + Graham/DCF (Değer) + Piotroski + Büyüme + Momentum + Teknik**. |
| **Incumbent Kuralı** | Basit rank bandı | **Güçlü Incumbent Koruması**: Mevcut pozisyon ciddi şekilde bozulmadıkça satılmaz. |
| **Nakit Modeli** | İkili (O0/O1) overlay | **4 Kademeli Rejim**: Normal (%0), Dikkat (%25), Savunma (%50), Risk-Off (%75). |
| **Kullanıcı Çıktısı** | Detaylı kurumsal JSON/PWA | **Yatırımcı Odaklı PWA**: "Hangi 5 hisseyi alayım, hedef/stop ne, nakit ne kadar?" |

---

## 4. ChatGPT / AI Asistanı İçin Somut Uygulama Adımları

ChatGPT olarak bu projeyi devraldığınızda şu sırayla ilerleyin:

### Adım 1: MobileInv Modüllerini İnceleyin
`c:\Users\kerem\OneDrive\Έγγραφα\Ai Projects\MobileInv\bist_picker\portfolio\selector.py` ve `scoring\composer.py` dosyalarını okuyun. Oradaki mantığı kavrayın.

### Adım 2: 5 Hisselik Konsantre Portföy Seçicisini (`usinv/portfolio/`) Güncelleyin
- `usinv/portfolio/selector.py` içindeki portföy büyüklüğü varsayılanını 15'ten 5'e çekin.
- MobileInv'daki incumbent toleransını ve 2 haftalık rotasyon döngüsünü `usinv/calendar.py` ile uyumlu bağlayın.
- Her hisse için hedef fiyat (DCF / Skor bazlı) ve dinamik ATR zarar kes (stop-loss) hesaplayın.

### Adım 3: Skorlama Ağırlıklarını MobileInv Modeline Yaklaştırın
`usinv/scoring/` altındaki kompozit puanlama motorunu MobileInv'ın 6 bileşenli yapısıyla besleyin:
1. **Kalite (Buffett):** ROE, ROIC, Düşük Borçluluk, İstikrarlı Faaliyet Karı.
2. **Değer (Graham + DCF):** F/K, PD/DD, FD/FAVÖK ve İndirgenmiş Nakit Akımı (DCF) marjı.
3. **Sağlık (Piotroski):** 9 kriterli F-Score (zayıf şirketleri veto etmek için).
4. **Büyüme (Growth):** Satış ve net kar büyüme ivmesi.
5. **Momentum:** 20, 60 ve 120 günlük piyasaya göreceli getiri (Relative Strength).
6. **Teknik Zamanlama:** 200 günlük hareketli ortalama üzerindeki konum ve RSI.

### Adım 4: 4 Kademeli Nakit Rejimini Entegre Edin
Makro göstergeleri (S&P 500 > 200 SMA, HY OAS spreadleri, VIX/UVXY ve 10 Yıllık Tahvil Faizi) kullanarak `NORMAL (%0)`, `CAUTION (%25)`, `DEFENSIVE (%50)`, `RISK_OFF (%75)` nakit hedeflerini `web/snapshot.json` çıktısına yazın.

### Adım 5: PWA Arayüzünü ve Snapshot Formatını Sadeleştirin
`web/index.html` ve `web/app.js` dosyalarını MobileInv PWA sadeliğine getirin:
- Üstte büyükçe: **Mevcut Nakit Durumu (%0 - %75) ve Makro Rejim**.
- Ortada kartlar halinde: **Seçilen 5 Hisse** (Giriş Fiyatı, Hedef Fiyat, Stop Seviyesi, Puanı ve 2 cümlelik "Neden Seçildi?" açıklaması).
- Altta: **Geçmiş Performans ve Rotasyon Geri Sayımı**.

### Adım 6: Testleri ve Lint Standartlarını Asla Bozmayın
Yapacağınız her geliştirmeden sonra mutlaka şu iki komutu çalıştırarak testlerin yeşil kaldığını doğrulayın:
```powershell
python -m ruff check .
pytest -q
```

---

## 5. Özet: Kullanıcıya Ne Sunmalısınız?

Kullanıcı bu projede terminal komutlarıyla boğuşmak istemiyor. 
**Kullanıcının tek görmek istediği şey:**
Telefonundan PWA'yı açtığında, BIST Picker'da olduğu gibi:
> *"Piyasa şu an Normal. Portföy %100 hissede. Bu 2 hafta elimizde tutacağımız 5 hisse: AAPL, NVDA, LLY, CEG, ANET. Stop seviyelerimiz bunlar, hedeflerimiz bunlar."*

Tüm geliştirme kararlarınızı bu sadelik ve odak etrafında verin. Başarılar!
