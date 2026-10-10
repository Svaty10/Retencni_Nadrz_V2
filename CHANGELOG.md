# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-10-10

### Added
- Reorganized complete codebase into 3 dedicated components: `uzel/`, `webserver/`, and `docs/`.
- Lightweight FastAPI / Uvicorn telemetry webserver for Raspberry Pi 3 B+ (`webserver/app.py`) with memory footprint < 35 MB RAM.
- Hardware SPI CC1101 transceiver driver (`webserver/cc1101_receiver.py`) on SPI0 (5 MHz) and BCM GPIO 25 with automatic state-machine watchdog and soft-reset recovery.
- Compact 8-byte Little-Endian binary telemetry packet (`vbat_raw_mv`, `temp_x100`, `hum_x100`, `level_mm`) across both microcontroller firmware and Python backend.
- Thread-safe in-memory cache and lightweight rotating SQLite database (`history.db`) bounded to 10 000 records.
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
