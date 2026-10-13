#!/bin/bash
# =============================================================================
# retencka-identity – first boot identity (idempotent, runs before network)
#   * hostname retencka-xxxx (last 4 hex digits of the Wi-Fi MAC) -> unique
#     retencka-xxxx.local even with several devices in one network
#   * ONE per-device password, printed on the device label, used for
#       - the setup hotspot "Retencka-XXXX" (WPA2) and
#       - the first web login (user "admin"), which forces creating an own account
#     stored in /boot/firmware/retencka.txt and /var/lib/retencka/data/setup_code
# =============================================================================
set -eu
IFACE="${RETENCKA_WIFI_IFACE:-wlan0}"
DATA=/var/lib/retencka/data
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
INFO="$BOOT/retencka.txt"

gen() {  # unambiguous characters only (no 0/O, 1/I/l)
    LC_ALL=C tr -dc 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789' < /dev/urandom | head -c "$1"
}

mac=$(cat "/sys/class/net/$IFACE/address" 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null || echo "00:00:00:00:00:00")
suffix=$(echo "$mac" | tr -d ':' | tail -c 5 | tr 'A-F' 'a-f')
name="retencka-$suffix"

if [ "$(hostname)" != "$name" ]; then
    hostnamectl set-hostname "$name"
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$name/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$name" >> /etc/hosts
    fi
    # --no-block: this unit is ordered Before=avahi-daemon, so waiting for the
    # restart job here would deadlock (avahi waits for us, we wait for avahi)
    systemctl --no-block try-restart avahi-daemon.service || true
fi

mkdir -p "$DATA"
if ! grep -q '^hotspot_password=' "$INFO" 2>/dev/null; then
    pw=$(gen 10)
    cat > "$INFO" <<INFOEOF
# Retenční nádrž – identita zařízení (vytisknout na štítek)
# Heslo platí pro Wi-Fi hotspot i pro první přihlášení na web (jméno: admin).
hostname=$name
web=http://$name.local  (nebo http://retencka.local)
hotspot_ssid=Retencka-${suffix^^}
hotspot_password=$pw
INFOEOF
    sync
fi
# the web bootstrap password always equals the label password
pw=$(sed -n 's/^hotspot_password=//p' "$INFO" | head -n1)
if [ -n "$pw" ] && [ "$(cat "$DATA/setup_code" 2>/dev/null)" != "$pw" ]; then
    printf '%s\n' "$pw" > "$DATA/setup_code"
fi
chown retencka:retencka "$DATA" "$DATA/setup_code" 2>/dev/null || true
chmod 600 "$DATA/setup_code" 2>/dev/null || true
