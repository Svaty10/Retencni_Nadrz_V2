#!/bin/bash
# Vzdálený servis přes NetBird – instalace klienta a připojení setup klíčem.
# Volá ho install.sh (--netbird-setup-key) i retencka-control.py (instalace z webu).
#
#   NB_SETUP_KEY=…  [NB_MANAGEMENT_URL=https://…]  retencka-netbird-install.sh
#
# Klíč se předává jen proměnnou prostředí (ne argumentem tohoto skriptu) a nikdy se
# nevypisuje. Řádky "STEP: …" jsou průběh pro webové rozhraní. Spouští root.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Spusťte jako root." >&2
    exit 1
fi
KEY="${NB_SETUP_KEY:-}"
URL="${NB_MANAGEMENT_URL:-}"
unset NB_SETUP_KEY
if ! [[ "$KEY" =~ ^[A-Za-z0-9-]{20,80}$ ]]; then
    echo "Neplatný setup key." >&2
    exit 2
fi
if [ -n "$URL" ] && ! [[ "$URL" =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:[0-9]{1,5})?/?$ ]]; then
    echo "Neplatná adresa serveru (jen https://…)." >&2
    exit 2
fi
export DEBIAN_FRONTEND=noninteractive

if ! command -v netbird >/dev/null; then
    echo "STEP: Přidávám repozitář NetBird…"
    apt-get install -y -qq ca-certificates curl gnupg >/dev/null
    install -d -m 755 /usr/share/keyrings
    curl -fsSL https://pkgs.netbird.io/debian/public.key \
        | gpg --dearmor --yes --output /usr/share/keyrings/netbird-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/netbird-archive-keyring.gpg] https://pkgs.netbird.io/debian stable main" \
        > /etc/apt/sources.list.d/netbird.list
    echo "STEP: Instaluji NetBird…"
    apt-get update -qq
    apt-get install -y -qq netbird >/dev/null
fi
echo "STEP: Spouštím službu NetBird…"
netbird service install >/dev/null 2>&1 || true
netbird service start >/dev/null 2>&1 || true
echo "STEP: Připojuji k serveru…"
if [ -n "$URL" ]; then
    netbird up --setup-key "$KEY" --management-url "$URL" >/dev/null
else
    netbird up --setup-key "$KEY" >/dev/null
fi
echo "STEP: Připojeno."
