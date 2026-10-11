#!/bin/bash
# =============================================================================
# retencka-alias – publish the friendly name "retencka.local" via mDNS.
# Every device always has its unique retencka-xxxx.local. The shared alias is
# claimed only if no other device in the network owns it yet (Avahi refuses a
# colliding name), so several units never fight over it.
# =============================================================================
set -u
IFACE="${RETENCKA_WIFI_IFACE:-wlan0}"
ALIAS="${RETENCKA_MDNS_ALIAS:-retencka.local}"
FLAG=/run/retencka/alias-owned
mkdir -p /run/retencka

ip_of() { ip -4 -o addr show dev "$IFACE" scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1; }

while true; do
    rm -f "$FLAG"
    addr=$(ip_of)
    if [ -z "$addr" ] || [ "$addr" = "192.168.4.1" ]; then
        sleep 15; continue
    fi
    avahi-publish -a -R "$ALIAS" "$addr" &
    pid=$!
    sleep 5
    if kill -0 "$pid" 2>/dev/null; then
        touch "$FLAG"
        # keep publishing until the address changes or avahi exits
        while kill -0 "$pid" 2>/dev/null && [ "$(ip_of)" = "$addr" ]; do sleep 30; done
        kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    else
        wait "$pid" 2>/dev/null
        sleep 600      # alias owned by another device – check again later
    fi
done
