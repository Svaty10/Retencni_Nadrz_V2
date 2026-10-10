# Retenční Nádrž V2 – Telemetrický Systém

Kompletní deterministický systém pro bezdrátové monitorování retenční nádrže určený pro ultra-low-power běh na baterie, automatické dopouštění přes **Hunter Hydrawise API**, zabezpečený webový přístup a integraci do **Home Assistanta** přes **Raspberry Pi 3 B+**.

Projekt je rozdělen do tří logických částí:
1. **`uzel/`**: Produkční bare-metal C (AVR-libc) firmware pro mikrokontrolér **ATtiny1624** (SOIC-14) napájený ze 3× Li-FeS2 článků (spotřeba v Power-Down < 1,2 µA).
2. **`webserver/`**: Lehký backend v Pythonu (**FastAPI / Uvicorn**) s přímým SPI rozhraním pro **CC1101**, autentizací uživatelů (**Session, role admin/user, brute-force ochrana**), ovládáním dopouštění přes **Hydrawise REST API**, předpovědí srážek z **Open-Meteo**, velkokapacitní SQLite databází (až 500 000+ záznamů) a responzivním dashboardem se záložkami (spotřeba RAM < 35 MB).
3. **`docs/`**: Hardwarové zapojení pro Raspberry Pi, Home Assistant REST konfigurace, specifikace API a návod k instalaci.

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
│   ├── app.py                   # FastAPI REST API, Auth, Hydrawise, Weather & Dashboard (< 35 MB RAM)
│   ├── cc1101_receiver.py       # SPI0 & GPIO CC1101 listener s auto-recovery
│   ├── config.json              # Rozměry nádrže, Hydrawise klíče, kalibrace, limity
│   ├── users.json               # Uživatelské účty a scrypt hashe hesel
│   ├── secret_key.txt           # Stabilní šifrovací klíč pro session cookies
│   ├── requirements.txt         # Minimalistické závislosti
│   ├── retencka.service         # Systemd unit file (MemoryMax=50M)
│   ├── static/css/style.css     # Moderní glassmorphism CSS se záložkami a steppery
│   └── templates/
│       ├── index.html           # Responzivní webový panel (Dashboard, Nastavení, Logy, Heslo)
│       └── login.html           # Přihlašovací stránka s brute-force ochranou
│
└── docs/                        # Dokumentace a integrace
    ├── home_assistant.yaml      # Připravený senzor pro Home Assistant (rest)
    ├── wiring_rpi.md            # Kompletní schéma zapojení Raspberry Pi a CC1101
    ├── api.md                   # Kompletní REST API specifikace (vč. Hydrawise a Auth)
    └── installation.md          # Návod na instalaci a spuštění na Pi OS Lite
```

---

## 2. Zabezpečení & Přihlašování

- **Webové rozhraní (`/`)**: Chráněno přihlášením. Nepřihlášený uživatel je automaticky přesměrován na `/login`.
- **Uživatelské role**: Podpora rolí `admin` (plná práva, nastavení, dopouštění) a `user` (čtení dat a grafů).
- **Bezpečné hashe hesel**: Hesla jsou ukládána v [users.json](file:///c:/Users/matej/OneDrive/Projekty/Retencni_Nadrz_V2/webserver/users.json) pomocí moderního algoritmu `scrypt`.
- **Brute-Force ochrana**: Po 5 neúspěšných pokusech o přihlášení z jedné IP adresy je přístup na 5 minut uzamčen.
- **Změna hesla**: Přihlášený uživatel si může změnit své heslo přímo v záložce *🔒 Moje heslo*.
- **Home Assistant API**: Endpoint `/api/v1/water/state` zůstává přístupný bez nutnosti session cookies pro přímé a spolehlivé dotazování platformou `rest`.

---

## 3. Rádiový Přenos a Paketový Formát

Modul **CC1101** komunikuje na frekvenci **433,92 MHz**, modulace **2-FSK**, rychlost **4.8 kBaud** s hardwarem počítaným **CRC-16** a datovým whiteningem.

Přenášený užitečný náklad (Payload) má fixní délku **8 bajtů (Little-Endian)**:

| Bajty | Datový typ | Název pole | Význam | Příklad |
| :---: | :---: | :--- | :--- | :--- |
| `0–1` | `uint16_t` | `vbat_raw_mv` | Napětí baterie v milivoltech | `4450` = 4,45 V |
| `2–3` | `int16_t`  | `temp_x100`   | Teplota v šachtě v setinách °C | `2150` = 21,50 °C |
| `4–5` | `uint16_t` | `hum_x100`    | Vlhkost v šachtě v setinách % | `6540` = 65,40 % RH |
| `6–7` | `uint16_t` | `level_mm`    | Hladina vody v milimetrech | `1450` = 1450 mm (145 cm) |

---

## 4. Webserver & Funkce

### 4.1. Záložky Uživatelského Rozhraní (Web UI)
1. **📊 Dashboard**:
   - Vizuální skleněná nádrž s animovanou vodní hladinou, procenty, litry a výškou v cm.
   - Dlaždice s živými metrikami: Teplota, Vlhkost, Napětí baterie, Odhad výdrže ve dnech, Předpověď srážek na 2 dny.
   - **Ovládání dopouštění Hydrawise**: Přímé nastavení cílové hladiny (%) pomocí tactile `[−]` a `[+]` tlačítek, spuštění a okamžité zastavení.
   - Interaktivní časový graf hladiny a teploty přes **Chart.js** (24h / 7 dní / 30 dní).
2. **⚙️ Nastavení & Hydrawise**:
   - Úprava rozměrů nádrže (plocha m², maximální hloubka cm).
   - Konfigurace Hunter Hydrawise (API Key, Relay ID ventilu, průtok řadu L/min, prahy dopouštění).
   - Chytré odkládání srážek podle Open-Meteo (souřadnice, práh srážek).
   - Okamžité uložení konfigurace bez nutnosti restartovat proces.
3. **📜 Historie & Data**:
   - Přehledná tabulka posledních měření telemetrie.
   - Tlačítko pro export kompletní databáze do formátu CSV (`/api/v1/water/export.csv`).
4. **🔒 Moje heslo**:
   - Formulář pro změnu vlastního hesla s ověřením stávajícího hesla.

### 4.2. Úložiště s Vysokou Kapacitou (Flash Disk)
- Databáze `history.db` (SQLite) je škálována na **500 000+ záznamů** (při 15min intervalu odpovídá více než **14 letům** historie na flash disku při spotřebě pouhých ~40 MB).

---

## 5. Rychlé Spuštění na Raspberry Pi 3 B+

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
