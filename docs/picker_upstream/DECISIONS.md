# Decisions

Bu dosya uzun tartışmaları değil, geçerli kararları ve nedenlerini saklar.

| ID | Karar | Gerekçe / Sonuç |
|---|---|---|
| D001 | Tek aktif UI PWA'dır; Android arşivdir. | Yeni özellik ve hata düzeltmesi PWA'ya gider; Android bağlamı AI'yi dağıtmaz. |
| D002 | Rotasyon iki haftada bir Pazartesi, çapa 2026-06-29. | Mevcut calibrated davranış ve kullanıcı rutini; değişiklik model deneyi sayılır. |
| D003 | Ana strateji `index_aware`, hedef 5, min 2 BIST100, max 1 non-BIST100. | Küçük-cap yoğunlaşmasını sınırlar; `max_non=1` forward holdout ile izlenir. |
| D004 | Günlük publish yalnız kısa fiyat + hafif evren yenilemesi yapar. | Tam veri çekimi scheduled run'ı timeout/kota riskine sokar; finansallar ayrı workflow'dadır. |
| D005 | Finansal skor girdisi TTM/PIT sözleşmesini kullanır. | Güncel ara dönem verisi skora ulaşır; gelecek bilgi tarihsel seçime sızmaz. |
| D006 | Corporate action ham `close`'u değiştirmez; `adjusted_close` üretilir. | Audit izi korunur, momentum/backtest/stop sahte split çöküşü görmez. |
| D007 | Canlı/history/PWA tek NAV ledger'ından beslenir. | Aynı işlemler farklı yüzeylerde farklı performans üretmez. |
| D008 | Historical state checksum'lı parçalara bölünebilir ve public export tarafından budanmaz. | GitHub boyut sınırı aşılırken uzun corrected cache korunur. |
| D009 | Performans nominal TL ile tek başına sunulmaz. | Hiperenflasyonda yanıltıcı manşeti önlemek için TÜFE, USD, altın ve drawdown birlikte verilir. |
| D010 | Reel-faiz koşullu ağırlık canlıdır ama saf OOS diye etiketlenmez. | Walk-forward destek vardır; 2024–2026 gözlemi tasarımı etkiledi, forward kanıt ayrıca gerekir. |
| D011 | CDS/insider/event/red-flag davranışı veri+coverage+ölçüm olmadan production gate yapılmaz. | Şeması veya tüketicisi olan ama beslenmeyen özellik aktif özellik değildir. |
| D012 | Dated handoff/progress/audit belgeleri aktif context'ten çıkarılır. | Git geçmişi tam kaydı korur; AI kanonik kısa belgeleri okur. |
| D013 | Dead code yalnız import/reference + workflow + test kanıtıyla silinir. | Dinamik Python yollarında statik analiz tek başına güvenilir değildir. |
| D014 | Yanlış değişiklik revert commit/PR ile geri alınır. | Ana dal geçmişi ve audit zinciri korunur; destructive Git kullanılmaz. |

Bir karar değişirse eski satır silinmez: `SUPERSEDED by Dxxx` olarak işaretlenir
ve yeni karar eklenir.
