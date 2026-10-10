# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-10

### Added
- Git repository initialization with LF normalization (`.gitattributes`) and build exclusion rules (`.gitignore`).
- Complete bare-metal C (AVR-libc) firmware implementation for ATtiny1624 SOIC-14 microcontrollers.
- Low-power clock management module (`src/power.c`, `include/power.h`) configuring CPU prescaler to 3.333 MHz and RTC Periodic Interrupt Timer (PIT) for 15-minute wakeups.
- Digital input buffer elimination (`PORT_ISC_INPUT_DISABLE_gc`) on analog pins and unused buffers for sleep current reduction down to < 1.2 µA.
- Brown-Out Detector (BOD) configuration with active monitoring and complete sleep disablement.
- Oversampled ADC0 driver (`src/adc.c`, `include/adc.h`) with 16× hardware sample accumulation and dedicated internal references (1.024 V for battery, 2.500 V for hydrostatic probe).
- High-side P-MOSFET battery switcher control via PA7 and TPS63900 5V boost control via PB3 with exact RC settling delays.
- Hardware/Software timeout-protected TWI0 driver for Sensirion SHT40 (`src/sht40.c`, `include/sht40.h`) featuring 9-clock SCL bus recovery and Sensirion CRC-8 checksum verification.
- CC1101 transceiver driver (`src/cc1101.c`, `include/cc1101.h`) operating at 433.92 MHz with 4.8 kBaud 2-FSK, hardware whitening, CRC-16, crystal startup handshake, and instant `SPWD` sleep entry.
- Critical battery failsafe shutdown routine protecting 3S Li-FeS2 battery cells below 3.0 V cut-off cliff.
- Production `Makefile` supporting compilation, disassembly generation, size checks, and SerialUPDI flashing via `avrdude`.
- Architectural guide and hardware pinout map in `README.md`.
