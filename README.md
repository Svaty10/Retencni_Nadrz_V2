# Retenční Nádrž V2 – Telemetrický Systém

Kompletní deterministický systém pro bezdrátové monitorování retenční nádrže určený pro ultra-low-power běh na baterie a integraci do **Home Assistanta** přes **Raspberry Pi 3 B+**.

Projekt je rozdělen do tří logických částí:
1. **`uzel/`**: Produkční bare-metal C (AVR-libc) firmware pro mikrokontrolér **ATtiny1624** (SOIC-14) napájený ze 3× Li-FeS2 článků (spotřeba v Power-Down < 1,2 µA).
2. **`webserver/`**: Lehký deterministický backend v Pythonu (**FastAPI / Uvicorn**) s přímým SPI rozhraním pro **CC1101**, rotující SQLite databází a responzivním webovým dashboardem (spotřeba RAM < 35 MB).
3. **`docs/`**: Hardwarové zapojení pro Raspberry Pi, Home Assistant REST konfigurace a specifikace API.

---

## 1. Struktura Projektu

```text
Retencni_Nadrz_V2/
├── uzel/                        # Senzorový uzel v šachtě / nádrži
│   ├── include/
│   │   ├── config.h             # Globální HW konfigurace a 8-bajtový paket
│   │   ├── power.h              # Power management, CLKCTRL, BOD, WDT, RTC-PIT
│   │   ├── adc.h                # 16× oversampled ADC0, VREF, spínání MOSFETů
│   │   ├── sht40.h              # Deterministický TWI0 driver a CRC-8
│   │   └── cc1101.h             # 433 MHz SPI0 radio driver
│   ├── src/
│   │   ├── main.c               # Stavový automat a bateriový failsafe
│   │   ├── power.c
│   │   ├── adc.c
│   │   ├── sht40.c
│   │   └── cc1101.c
│   └── Makefile                 # Sestavení pro ATtiny1624 a SerialUPDI flash
│
├── webserver/                   # Přijímač na Raspberry Pi 3 B+
│   ├── app.py                   # FastAPI REST API & dashboard (< 35 MB RAM)
│   ├── cc1101_receiver.py       # SPI0 & GPIO CC1101 listener s auto-recovery
│   ├── config.json              # Rozměry nádrže, kalibrace, limity
│   ├── requirements.txt         # Minimalistické závislosti
│   ├── retencka.service         # Systemd unit file (MemoryMax=50M)
│   ├── static/css/style.css     # Moderní glassmorphism CSS
│   └── templates/index.html     # Responzivní webový panel s animovanou nádrží
│
└── docs/                        # Dokumentace a integrace
    ├── home_assistant.yaml      # Připravený senzor pro Home Assistant (rest)
    ├── wiring_rpi.md            # Kompletní schéma zapojení Raspberry Pi a CC1101
    ├── api.md                   # REST API specifikace
    └── installation.md          # Návod na instalaci a spuštění na Pi OS Lite
```

---

## 2. Rádiový Přenos a Paketový Formát

Modul **CC1101** komunikuje na frekvenci **433,92 MHz**, modulace **2-FSK**, rychlost **4.8 kBaud** s hardwarem počítaným **CRC-16** a datovým whiteningem.

Přenášený užitečný náklad (Payload) má fixní délku **8 bajtů (Little-Endian)**:

| Bajty | Datový typ | Název pole | Význam | Příklad |
| :---: | :---: | :--- | :--- | :--- |
| `0–1` | `uint16_t` | `vbat_raw_mv` | Napětí baterie v milivoltech | `4450` = 4,45 V |
| `2–3` | `int16_t`  | `temp_x100`   | Teplota v šachtě v setinách °C | `2150` = 21,50 °C |
| `4–5` | `uint16_t` | `hum_x100`    | Vlhkost v šachtě v setinách % | `6540` = 65,40 % RH |
| `6–7` | `uint16_t` | `level_mm`    | Hladina vody v milimetrech | `1450` = 1450 mm (145 cm) |

---

## 3. Webserver & Home Assistant REST API

Webserver běží na portu `8080` a poskytuje následující JSON endpointy:

### `GET /api/v1/water/state`
Primární endpoint pro Home Assistant REST senzor. Pokud uzel nevysílá déle než 30 minut, pole `status` se změní na `"stale"`.
```json
{
  "water_level_cm": 145.0,
  "water_level_percent": 72.5,
  "temperature": 21.5,
  "humidity": 65.4,
  "battery_voltage": 4.45,
  "battery_low": false,
  "current_liters": 14500,
  "last_seen_seconds_ago": 12,
  "status": "online",
  "timestamp": "2026-10-10T09:58:30.123456+00:00"
}
```

### `GET /api/v1/water/history?hours=24`
Vrací pole časových řad pro vykreslení historie hladiny a teploty v Chart.js na webu.

### `GET /health`
Liveness probe pro systemd watchdog a kontrolu stavu SPI přijímače.

---

## 4. Rychlé Spuštění na Raspberry Pi 3 B+

1. Povolit SPI: `sudo raspi-config` -> *Interface Options* -> *SPI* -> *Yes*.
2. Vytvořit venv a nainstalovat závislosti:
   ```bash
   cd webserver
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   pip install spidev gpiod
   ```
3. Povolit a spustit systemd službu:
   ```bash
   sudo cp retencka.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now retencka.service
   ```
4. Otevřít dashboard v prohlížeči na `http://<IP_RASPBERRY_PI>:8080/`.
