# Security Policy

## Supported Versions

Only the latest minor release of **Retenční nádrž V2** receives security updates.
Because hubs support signed Over-The-Air (OTA) updates (`system/bin/retencka-update.py`),
we strongly recommend keeping your hub on the latest release.

| Version | Supported          |
| ------- | ------------------ |
| 1.21.x  | :white_check_mark: |
| < 1.21  | :x:                |

## Security Architecture Overview

Detailed documentation of the threat model, privilege separation, cryptographic protocol,
and security audit history is maintained in [`docs/bezpecnost.md`](../docs/bezpecnost.md)
and [`docs/architektura.md`](../docs/architektura.md):

- **433 MHz Radio Link (`uzel/` ↔ `server/retencka/crypto.py`):**
  - Ephemeral X25519 key agreement with low-order point rejection.
  - AES-128-CTR encryption + AES-CMAC authentication with strictly monotonic 32-bit frame counters backed by dual-bank EEPROM wear-leveling (`CTR_RESERVE = 1024`) and two-step large counter jump verification on the hub.
  - Signed `UNPAIR` frames bound to the sensor's fresh boot public key.
- **Privilege Separation (`system/`):**
  - The main daemon (`retencka.service`) runs as an unprivileged user (`retencka`) with `ProtectSystem=strict` and `DevicePolicy=closed`.
  - Root helper scripts (`retencka-control.py`, `retencka-update.py`, `retencka-identity.sh`) enforce `O_NOFOLLOW`, regular-file checks, strict regex validation, and atomic rename operations.
- **OTA Updates:**
  - Release tarballs are signed with Ed25519 (`system/keys/release.pub`), verified prior to extraction, and automatically rolled back if `/health` fails after restart.
- **Web UI & HTTP Server:**
  - Custom dependency-free asyncio HTTP/1.1 server rejecting `Transfer-Encoding` smuggling, enforcing per-IP connection limits, `scrypt` password hashing, SHA-256 hashed session tokens, `Origin` + `X-CSRF-Token` validation, and strict `Content-Security-Policy`.

## Reporting a Vulnerability

Please **do not** report security vulnerabilities through public GitHub issues.

Instead, report them privately via **[GitHub Private Vulnerability Reporting](https://github.com/Svaty10/Retencni_Nadrz_V2/security/advisories/new)**.

When reporting, please include:
1. Affected component (`uzel/` firmware, `server/` backend, `system/` root helpers, or `server/static/` Web UI) and version.
2. Step-by-step reproduction or proof-of-concept.
3. Assessment of the required attacker position (RF 433 MHz proximity, local network, unprivileged user, or compromised `retencka` service user).

We will acknowledge receipt within **72 hours** and provide a remediation timeline or coordinated release plan.
