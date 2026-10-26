# Contributing to Retenční nádrž V2

Thank you for your interest in contributing to **Retenční nádrž V2**! This document outlines the project structure, development workflow, testing requirements, and pull request guidelines.

## Project Structure

- `uzel/` — Ultra-low-power C firmware for the ATtiny1624 sensor node (CC1101 433 MHz radio, SHT40, TPS63900 boost, hydrostatic probe, X25519 + AES-128-CTR/CMAC).
- `server/` — Python 3.11+ zero-dependency hub server (`retencka/`), Vanilla JS/CSS SPA (`static/`), and test suite (`tests/`).
- `system/` — Raspberry Pi installation scripts, hardened `systemd` units, and privilege-separated root helpers.
- `docs/` — Technical documentation, wiring guides, API reference, and security review.

## Development & Verification

### 1. Python Backend & Web Server Tests
The server uses only the Python 3.11+ standard library (with optional `spidev`/`gpiod` on Raspberry Pi hardware). Run the full test suite from the `server/` directory:

```bash
cd server
python -m unittest discover -s tests -v
```

### 2. Sensor Node Firmware & Host Self-Tests
To build the ATtiny1624 firmware (`avr-gcc` required) and run host-side cryptographic and scheduler unit tests (`gcc` required):

```bash
cd uzel
make          # build/firmware.hex
make test     # crypto self-test against official vectors + scheduler test (host gcc)
```

## Coding & Architecture Guidelines

1. **Zero Runtime Dependencies on Hub:** Keep `server/retencka/` restricted to the Python standard library so it runs reliably within 128 MB RAM on Raspberry Pi Zero 2 W.
2. **Non-Blocking Execution:** Never block the `asyncio` event loop; use bounded timeouts on all hardware, SPI, I²C, and network operations.
3. **Privilege Boundaries:** Root helpers in `system/bin/` must never trust files inside `/var/lib/retencka/data`—always open with `O_NOFOLLOW`, verify regular files, and write via atomic `mkstemp` + `os.replace`.
4. **Documentation & Changelog Sync:** Any functional, API, UI, or configuration change must include corresponding updates to `README.md`, `CHANGELOG.md` (following [Keep a Changelog](https://keepachangelog.com/)), and relevant files in `docs/`.
5. **Tests Required:** Every bug fix or new feature must include unit or integration test coverage in `server/tests/` or `uzel/tools/`.
