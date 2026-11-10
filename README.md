# US Picker

US Picker, [MobileInv BIST Picker](https://github.com/Somethinglikeu-hub/MobileInv) uygulamasının ABD hisse senetleri için uyarlanmış sürümüdür. SEC mali tabloları, Alpaca fiyatları, FRED makrosu ve tarihsel S&P 500 üyeliği ile çalışır. Uygulama model portföyünü, risk kurallarını, SPY karşılaştırmasını ve cihazda tutulan kişisel işlem kayıtlarını gösterir.

- [Web uygulaması](https://infenglov-hue.github.io/USInv/)
- [Kurulum, özel veriyle PWA ve veri sözleşmesi](docs/US_PICKER.md)
- [ABD kalibrasyon planı ve kabul ölçütleri](docs/US_CALIBRATION_PLAN.md)

Aktif uygulama `us_picker/` ve `pwa/` dizinlerindedir. ABD stratejisi için V1 canlı varsayılanları korunur. BIST V2 kuralları ABD verisinde ayrı bir araştırma adayı olarak ölçülür. Performans iddiaları ancak doğrulanabilir koşu çıktısına dayanır.

Herkese açık Pages kurulumu varsayılan olarak yalnız uygulama dosyalarını sunar. Alpaca fiyatlarıyla üretilen akış ve çalışma veritabanı özel kalır. [Alpaca veri dağıtım koşulları](https://alpaca.markets/support/redistribute-alpaca-api)

## Geliştirme

```powershell
uv venv --python 3.12
uv pip install -e ".[dev,enhanced]"
.venv/Scripts/python.exe -m pytest -q
node --check pwa/app.js
node --check pwa/sw.js
node pwa/tests/pwa-static-check.mjs
```

Eski `usinv/` deneyleri için [arşiv devri](docs/HANDOFF.md) ve [tasarım belgeleri](docs/BUILD_GUIDE.md) depoda durur. Bunlardaki simülatör sonuçları ve sabit demo anlık görüntüsü aktif model için performans kanıtı sayılmaz.
