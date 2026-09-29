# Telegram Alert Bot

Telegram yalnız bildirim kanalıdır; broker işlemi yapmaz. Workflow:
`.github/workflows/monitor-alerts.yml`.

## Çalışma biçimi

```text
GitHub Actions → state restore → monitor-alerts
  ├─ açık pozisyon fiyat/stop/target kontrolü
  ├─ elde tutulan hisseler için KAP özeti
  ├─ rotasyon gününde AL/SAT/TUT raporu
  └─ dedup state → MobileInv-state
```

Hafta içi `07:00`, `10:00`, `13:00` UTC; İstanbul'da `10:00`, `13:00`,
`16:00`. Manuel: Actions → **Monitor Portfolio Alerts** → Run workflow.

## Gerekli GitHub secrets

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GEMINI_API_KEY`
- `TCMB_API_KEY`
- `PUBLISH_REPO_TOKEN`

Secret değerlerini belgeye, issue/log'a veya `settings.yaml` içine yazma.

## Mesajlar

- **STOP_LOSS:** fiyat stop seviyesine geldi; aynı pozisyon için tekrar
  gönderilmez.
- **TAKE_PROFIT:** fiyat target seviyesine geldi; tekrar gönderilmez.
- **KAP:** yalnız açık portföy için anlamlı positive/negative olay; nötr veya
  boş içerik gönderilmez.
- **Rotasyon:** cycle gününde AL/SAT/TUT ve açık pozisyon özeti.

## Dayanıklılık

- `alerts_state.json` tekrar bildirimini önler ve private state repo'da saklanır.
- Boş portföyde gereksiz fiyat/KAP taraması yapılmaz.
- Telegram 429/geçici ağ hataları sınırlı retry alır.
- Dış kaynak metni Telegram HTML'e eklenmeden escape edilir.
- State yazılamazsa veya monitor çökerse komut sıfır dışı kapanır.
- Eşzamanlı feed/monitor state push'ları rebase ile uzlaştırılır.

## Yerel kontrol

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_notifications.py
.venv\Scripts\python.exe -m bist_picker monitor-alerts
```

İkinci komut gerçek Telegram/KAP çağrıları yapabilir; yalnız doğru environment
secret'larıyla ve bilinçli testte çalıştırılır. Mesaj göndermeden teşhis için
önce test suite ve workflow logları tercih edilir.
