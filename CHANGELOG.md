# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] - 2026-10-10

### Added
- Complete user authentication and session management system using Starlette `SessionMiddleware` and persistent `secret_key.txt`.
- Responsive glassmorphism login portal (`webserver/templates/login.html`) with route protection redirecting unauthenticated users from `/` to `/login`.
- User account database (`webserver/users.json`) with role-based access control (`admin`, `user`) and Werkzeug `scrypt` password hashing.
- Brute-force protection tracking failed login attempts per client IP with a 5-minute lockout policy after 5 consecutive failures.
- User session badge in dashboard header displaying current username, role, and logout action (`/logout`).
- Dedicated "🔒 Moje heslo" tab allowing logged-in users to securely change their password (`POST /api/v1/user/password`).

## [2.1.0] - 2026-10-10

### Added
- Hunter Hydrawise REST API integration (`/api/v1/water/refill/start`, `/api/v1/water/refill/stop`) allowing automated and manual tank refilling.
- Dynamic refill duration calculations based on tank dimensions, missing volume, and measured municipal water flow rate.
- Open-Meteo precipitation forecast integration with smart rain deferral logic protecting against unnecessary municipal water consumption.
- Three-tab web dashboard interface:
  - **Dashboard**: Live tank animation, telemetry cards, tactile `[−]` and `[+]` refill stepping buttons, and Chart.js history.
  - **Nastavení & Hydrawise**: Live configuration form persisting to `config.json` without server restart.
  - **Historie & Data**: Real-time tabular logs with streaming CSV export (`/api/v1/water/export.csv`).
- High-capacity SQLite storage scaled for 8 GB+ USB flash drives (up to 500 000 records / 14+ years retention).
- Battery runtime estimation algorithm based on Li-FeS2 3S multi-day discharge trend.
- Updated documentation in `docs/api.md`, `docs/home_assistant.yaml`, and `README.md`.

## [2.0.0] - 2026-10-10

### Added
- Reorganized complete codebase into 3 dedicated components: `uzel/`, `webserver/`, and `docs/`.
- Lightweight FastAPI / Uvicorn telemetry webserver for Raspberry Pi 3 B+ (`webserver/app.py`) with memory footprint < 35 MB RAM.
- Hardware SPI CC1101 transceiver driver (`webserver/cc1101_receiver.py`) on SPI0 (5 MHz) and BCM GPIO 25 with automatic state-machine watchdog and soft-reset recovery.
- Compact 8-byte Little-Endian binary telemetry packet (`vbat_raw_mv`, `temp_x100`, `hum_x100`, `level_mm`) across both microcontroller firmware and Python backend.
- Thread-safe in-memory cache and lightweight rotating SQLite database (`history.db`).
- Home Assistant REST endpoints: `GET /api/v1/water/state` with online/stale/offline state tracking, `GET /api/v1/water/history`, and `GET /health`.
- Responsive single-page web dashboard (`webserver/templates/index.html`) featuring an animated water tank graphic, live telemetry cards, and interactive historical Chart.js graphs.
- Production systemd unit file (`webserver/retencka.service`) enforcing strict memory caps (`MemoryMax=50M`) and automatic crash recovery.
- Complete Home Assistant sensor configuration YAML (`docs/home_assistant.yaml`).
- Raspberry Pi 3 B+ 40-pin GPIO wiring diagram and hardware setup manual (`docs/wiring_rpi.md`).
- REST API specification document (`docs/api.md`) and deployment guide (`docs/installation.md`).

## [1.0.0] - 2026-10-10

### Added
- Git repository initialization with LF normalization (`.gitattributes`) and build exclusion rules (`.gitignore`).
- Complete bare-metal C (AVR-libc) firmware implementation for ATtiny1624 SOIC-14 microcontrollers.
- Low-power clock management module (`power.c`, `power.h`) configuring CPU prescaler to 3.333 MHz and RTC Periodic Interrupt Timer (PIT) for 15-minute wakeups.
- Digital input buffer elimination (`PORT_ISC_INPUT_DISABLE_gc`) on analog pins and unused buffers for sleep current reduction down to < 1.2 µA.
- Brown-Out Detector (BOD) configuration with active monitoring and complete sleep disablement.
- Oversampled ADC0 driver (`adc.c`, `adc.h`) with 16× hardware sample accumulation and dedicated internal references (1.024 V for battery, 2.500 V for hydrostatic probe).
- High-side P-MOSFET battery switcher control via PA7 and TPS63900 5V boost control via PB3 with exact RC settling delays.
- Hardware/Software timeout-protected TWI0 driver for Sensirion SHT40 (`sht40.c`, `sht40.h`) featuring 9-clock SCL bus recovery and Sensirion CRC-8 checksum verification.
- CC1101 transceiver driver (`cc1101.c`, `cc1101.h`) operating at 433.92 MHz with 4.8 kBaud 2-FSK, hardware whitening, CRC-16, crystal startup handshake, and instant `SPWD` sleep entry.
- Critical battery failsafe shutdown routine protecting 3S Li-FeS2 battery cells below 3.0 V cut-off cliff.
- Production `Makefile` supporting compilation, disassembly generation, size checks, and SerialUPDI flashing via `avrdude`.
- Architectural guide and hardware pinout map in `README.md`.
