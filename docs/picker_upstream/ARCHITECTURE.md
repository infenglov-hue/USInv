# Architecture

Bu belge sistemin güncel sınırlarını gösterir. Ayrıntılı oturum geçmişi için
`HISTORY_INDEX.md` kullanılır.

## Repo sınırları

```text
MobileInv (bu repo)
  veri alma → temizleme/TTM → skorlama → seçim/çıkış → backtest/NAV
       │                                      │
       ├──────── state ───────────────────────┘
       ▼
MobileInv-state (private)
  runtime SQLite → `state-current` release asset'leri (git değil)
  alerts_state.json + investor-grade JSON → git
       │
       ├──────── snapshot/manifest ───────────► MobileInv-feed / gh-pages
       └──────── live_prices.json ────────────► MobileInv-feed / live-data
                                                  │
                                                  ▼
                                             PWA/WebUI
```

`MobileInv-mobile-v2` bu akışın aktif parçası değildir.

## Backend kod haritası

| Yol | Sorumluluk |
|---|---|
| `bist_picker/data/` | İŞ Yatırım, KAP, TCMB, Yahoo ve cache/freshness |
| `bist_picker/cleaning/` | Dönem ayrıştırma, TTM, enflasyon ve fiyat düzeltme |
| `bist_picker/scoring/` | Faktörler, sektör modelleri, normalizasyon ve kompozit |
| `bist_picker/portfolio/` | Evren, index-aware seçim, rotasyon, stop/target, cash |
| `bist_picker/backtest/` | Aktif point-in-time backtest ve performans özeti |
| `bist_picker/db/` | SQLAlchemy şema, runtime migration ve bağlantı |
| `bist_picker/mobile_snapshot.py` | SQLite public snapshot sözleşmesi |
| `bist_picker/mobile_feed.py` | Manifest + gzip snapshot yayını |
| `bist_picker/live_prices.py` | Küçük canlı-fiyat JSON feed'i |
| `bist_picker/read_service.py` | Snapshot/API/PWA için ortak read model |
| `bist_picker/notifications/` | Telegram ve portföy alarm monitörü |
| `bist_picker/cli.py` | Workflow ve bakım komutlarının giriş noktası |
| `scripts/state_db_artifact.py` | State store/restore + release transport (pull/push), checksum ve parçalara ayırma |

`bist_picker/api/` ve `bist_picker/dashboard/` üretim PWA yolunda çalışmaz;
ancak açık FastAPI/manuel Streamlit girişleri ve regresyon testleri bulunduğu
için korunmuş opsiyonel geliştirme yüzeyleridir. Yeni kullanıcı özelliği
bunlara değil PWA'ya yazılır.

## Ana veri sözleşmeleri

### Runtime state

- Kaynak gerçek `MobileInv-state` içindeki SQLite'tır.
- GitHub tek-dosya sınırı aşıldığında state checksum'lı parçalara bölünür.
- Beş workflow hem yeni split formatı hem eski tek gzip formatını okuyabilir.
- **Taşıma katmanı release'dir, commit değil (2026-07-27):** artifact
  `state-current` etiketli tek rolling release'in asset'leri olarak yaşar;
  `state_db_artifact.py pull/push` bunu yönetir. Her koşuda ~100 MB'lık blob
  commit'lemek repoyu 10,6 GB geçmişe çıkarmış, tam klon runner diskini 67 MB'a
  düşürmüştü. Release asset'leri git nesne deposuna girmez.
- Eski commit'lenmiş artifact'lar geçmişte **duruyor**; silinmedi.
  `Rebuild Historical Score Cache` işi `state_db_commit` verildiğinde hâlâ o
  geçmişten okur — release yolunu yalnızca pin verilmediğinde kullanır.
- Feed export kaynak state'teki tarihsel skor/fiyat satırlarını budamaz.

### Public snapshot

- `MobileInv-feed/manifest.json` küçük pointer ve özet dosyasıdır.
- Detaylı şirket, skor, pozisyon ve NAV verisi `mobile_snapshot.db.gz`
  içindedir.
- Manifest'teki SHA-256, yayımlanan gzip dosyasıyla eşleşmelidir.
- Daha eski bir DB yeni public feed'i yanlışlıkla ezemez; bilinçli rollback
  ayrı override ister.

### Canlı fiyat

- `live-data` branch'indeki `live_prices.json`, büyük snapshot'tan bağımsızdır.
- Quote zamanı, snapshot üretim zamanı ve son kapanış tarihi ayrı tutulur.
- Sağlayıcı arızasında UI snapshot/son işlem fallback'ini etiketler.

## Kritik invariantlar

1. **Point-in-time:** Bir scoring date gelecekte yayımlanmış finansalı göremez.
2. **TTM:** Akış kalemleri tekil çeyreklerden TTM; bilanço kalemleri dönem-sonu
   stok değeri olarak işlenir.
3. **Corporate action:** `close` ham kalır; `adjusted_close` sürekliliği sağlar;
   temizleme idempotent olmalıdır.
4. **Final fiyat:** Açık seans günlük XU100 barı final benchmark sayılmaz.
5. **Tek NAV:** PWA, history ve raporlar aynı ledger sözleşmesini kullanır.
6. **State koruma:** Public export uzun tarihsel cache/state'i küçültmez.
7. **Sürümleme:** Skorlama matematiği veya veri sözleşmesi değişirse cache
   sürümü/invalidation birlikte güncellenir.
8. **Yayın bütünlüğü:** Snapshot hash'i, schema sürümü ve manifest aynı pakete
   aittir.

Bu invariantlardan birini değiştiren PR küçük “cleanup” sayılmaz; ilgili test,
historical kabul ve publish dry-run gerektirir.
