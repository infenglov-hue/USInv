# Sabitlenmiş PWA Runtime Dosyaları

Ana uygulamanın ilk açılışını ve offline çalışmasını harici CDN erişimine bağlamamak için aşağıdaki sürümler repoda tutulur:

| Dosya | Paket/sürüm | Lisans |
| --- | --- | --- |
| `pako.min.js` | pako 2.1.0 | MIT |
| `sql-wasm.js`, `sql-wasm.wasm` | sql.js 1.8.0 | MIT |
| `apexcharts.min.js` | ApexCharts 3.49.2 | MIT |

Sürüm yükseltirken dosyalar birlikte güncellenmeli, `index.html`, `sw.js` ve `tests/pwa-static-check.mjs` içindeki asset/cache sürümü artırılmalı ve PWA statik kontrolleri çalıştırılmalıdır.
