#!/bin/bash
# =============================================================================
# retencka-netwatch – Wi-Fi supervisor with a fallback setup hotspot
#
#  * connected to a known Wi-Fi or by Ethernet cable
#                                           -> nothing to do (hotspot off)
#  * no connection for GRACE_S (or no known network at all)
#                                           -> start hotspot "Retencka-XXXX" +
#                                              captive portal (redirect :80 -> :8088)
#  * hotspot running and known networks exist -> every RETRY_S, if no phone is
#    connected to the hotspot, briefly try the known networks again
#    (a router usually boots slower than the Pi after a power cut)
#  * portal request (/run/retencka/wifi-req/go) -> stop hotspot, connect to the
#    chosen SSID; on failure restart the hotspot and report the error
#
# Runs as root (NetworkManager control). State lives in /run/retencka (tmpfs).
# =============================================================================
set -u

IFACE="${RETENCKA_WIFI_IFACE:-wlan0}"
AP_CON="retencka-ap"
RUN=/run/retencka
GRACE_S="${RETENCKA_AP_GRACE_S:-180}"
RETRY_S="${RETENCKA_AP_RETRY_S:-180}"
PORTAL_PORT=8088
INFO=/boot/firmware/retencka.txt
[ -f "$INFO" ] || INFO=/boot/retencka.txt

mkdir -p "$RUN/wifi-req"
chmod 0755 "$RUN"
chmod 0700 "$RUN/wifi-req"

log() { echo "netwatch: $*"; }

info_value() { sed -n "s/^$1=//p" "$INFO" 2>/dev/null | head -n1; }

wifi_connected() {
    # connected through any profile other than the hotspot
    local con
    con=$(nmcli -t -f GENERAL.STATE,GENERAL.CONNECTION device show "$IFACE" 2>/dev/null)
    echo "$con" | grep -q '^GENERAL.STATE:100' || return 1
    echo "$con" | grep -q "^GENERAL.CONNECTION:$AP_CON\$" && return 1
    return 0
}

eth_connected() {
    # any wired (or other non-Wi-Fi) device that is connected counts as "online":
    # a Pi on a cable without any Wi-Fi profile must not start the hotspot
    nmcli -t -f DEVICE,TYPE,STATE device status 2>/dev/null \
        | awk -F: '$2=="ethernet" && $3=="connected" {found=1} END {exit !found}'
}

lan_connected() {
    wifi_connected || eth_connected
}

ap_active() {
    nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$AP_CON"
}

known_networks() {
    nmcli -t -f NAME,TYPE connection show 2>/dev/null \
        | awk -F: -v ap="$AP_CON" '$2=="802-11-wireless" && $1!=ap {print $1}'
}

ap_clients() {
    iw dev "$IFACE" station dump 2>/dev/null | grep -c '^Station' || true
}

save_scan() {
    # nmcli -t escapes ':' inside fields as '\:'
    if nmcli -t -f SSID,SIGNAL,SECURITY device wifi list --rescan yes ifname "$IFACE" \
            > "$RUN/scan.tmp" 2>/dev/null && [ -s "$RUN/scan.tmp" ]; then
        mv "$RUN/scan.tmp" "$RUN/scan.txt"
    fi
}

ensure_ap_profile() {
    local ssid psk
    ssid=$(info_value hotspot_ssid)
    psk=$(info_value hotspot_password)
    if [ -z "$ssid" ] || [ ${#psk} -lt 8 ]; then
        log "missing hotspot credentials in $INFO"
        return 1
    fi
    if ! nmcli -t -f NAME connection show | grep -qx "$AP_CON"; then
        nmcli connection add type wifi ifname "$IFACE" con-name "$AP_CON" autoconnect no ssid "$ssid" \
            802-11-wireless.mode ap 802-11-wireless.band bg \
            ipv4.method shared ipv4.addresses 192.168.4.1/24 ipv6.method disabled \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk" wifi-sec.proto rsn \
            wifi-sec.pairwise ccmp wifi-sec.group ccmp >/dev/null
    else
        nmcli connection modify "$AP_CON" 802-11-wireless.ssid "$ssid" wifi-sec.psk "$psk" \
            ipv4.addresses 192.168.4.1/24 >/dev/null
    fi
}

portal_redirect() {   # on|off
    nft delete table ip retencka_portal 2>/dev/null
    if [ "$1" = on ]; then
        nft -f - <<EOF
table ip retencka_portal {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        iifname "$IFACE" tcp dport 80 redirect to :$PORTAL_PORT
    }
}
EOF
    fi
}

start_ap() {
    ensure_ap_profile || return 1
    # fresh Pi OS Lite has the Wi-Fi radio blocked until a regulatory country is set
    rfkill unblock wifi 2>/dev/null
    nmcli radio wifi on >/dev/null 2>&1
    save_scan
    log "starting hotspot"
    if nmcli --wait 30 connection up "$AP_CON" >/dev/null 2>&1; then
        portal_redirect on
        systemctl start retencka-portal.service
        AP_SINCE=$(date +%s)
        return 0
    fi
    log "hotspot failed to start"
    return 1
}

stop_ap() {
    systemctl stop retencka-portal.service 2>/dev/null
    portal_redirect off
    nmcli --wait 15 connection down "$AP_CON" >/dev/null 2>&1
}

try_known() {
    # let NetworkManager pick the best known network; 40 s budget
    nmcli device wifi rescan ifname "$IFACE" >/dev/null 2>&1
    sleep 4
    nmcli --wait 40 device connect "$IFACE" >/dev/null 2>&1
    wifi_connected
}

handle_portal_request() {
    local d="$RUN/wifi-req" ssid psk name
    ssid=$(cat "$d/ssid" 2>/dev/null)
    psk=$(cat "$d/psk" 2>/dev/null)
    rm -f "$d/go" "$d/ssid" "$d/psk"
    [ -n "$ssid" ] || return
    name="wifi-$(echo "$ssid" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-24)"
    log "portal: connecting to '$ssid'"
    echo "connecting" > "$RUN/wifi-result"
    sleep 3                                # let the portal deliver its response
    stop_ap
    nmcli connection delete "$name" >/dev/null 2>&1
    if [ -n "$psk" ]; then
        out=$(nmcli --wait 45 device wifi connect "$ssid" password "$psk" ifname "$IFACE" name "$name" 2>&1)
    else
        out=$(nmcli --wait 45 device wifi connect "$ssid" ifname "$IFACE" name "$name" 2>&1)
    fi
    if wifi_connected; then
        nmcli connection modify "$name" connection.autoconnect yes connection.autoconnect-priority 10 >/dev/null
        echo "ok" > "$RUN/wifi-result"
        log "connected to '$ssid'"
        LAST_OK=$(date +%s)
    else
        nmcli connection delete "$name" >/dev/null 2>&1
        echo "error:Nepodařilo se připojit k '$ssid' (špatné heslo nebo slabý signál)." > "$RUN/wifi-result"
        log "connection to '$ssid' failed: $out"
        start_ap
    fi
}

# ---------------------------------------------------------------- main loop
portal_redirect off
LAST_OK=$(date +%s)
AP_SINCE=0
LAST_RETRY=$(date +%s)

while true; do
    now=$(date +%s)

    if [ -f "$RUN/wifi-req/go" ]; then
        handle_portal_request
        continue
    fi

    if ap_active; then
        if [ -f "$RUN/rescan" ]; then
            rm -f "$RUN/rescan"
            save_scan        # may not work in AP mode on every chip; keeps the old list then
        fi
        if [ -n "$(known_networks)" ] && [ $((now - LAST_RETRY)) -ge "$RETRY_S" ] \
                && [ "$(ap_clients)" -eq 0 ]; then
            LAST_RETRY=$now
            log "retrying known networks"
            stop_ap
            if try_known; then
                LAST_OK=$(date +%s)
                log "back online"
            else
                start_ap
            fi
        fi
    elif lan_connected; then
        LAST_OK=$now
    else
        if [ -z "$(known_networks)" ] || [ $((now - LAST_OK)) -ge "$GRACE_S" ]; then
            if start_ap; then
                LAST_RETRY=$(date +%s)
            else
                LAST_OK=$now     # back off for GRACE_S before the next attempt
            fi
        fi
    fi
    sleep 10
done
