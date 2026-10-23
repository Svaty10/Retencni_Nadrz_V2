#!/bin/bash
# =============================================================================
# Retenční nádrž – instalace na Raspberry Pi OS Lite (Bookworm / Trixie)
#
# Nejjednodušeji jedním příkazem (stáhne repozitář a spustí tento skript):
#   curl -fsSL https://raw.githubusercontent.com/Svaty10/Retencni_Nadrz_V2/main/system/bootstrap.sh | sudo bash
# Nebo ručně:
#   git clone https://github.com/Svaty10/Retencni_Nadrz_V2.git
#   cd Retencni_Nadrz_V2 && sudo ./system/install.sh
#
# Volby:
#   --overlay                 po instalaci zapnout read-only root (overlayfs);
#                             jen pokud je /var/lib/retencka samostatný oddíl
#   --cloudflare-token TOKEN  nainstalovat cloudflared a připojit tunel (vlastní doména)
#   --netbird-setup-key KLÍČ  vzdálený servis přes NetBird (klíč z app.netbird.io);
#                             přístup je po instalaci povolen 24 h, pak jen se
#                             souhlasem zákazníka (web: Nastavení → Telegram a servis)
#   --netbird-url URL         vlastní (self-hosted) NetBird server
#   --no-hotspot              nespouštět hlídání Wi-Fi / záložní hotspot
#   --timezone ZÓNA           časová zóna (výchozí Europe/Prague; bez volby se
#                             mění jen výchozí zóna Pi OS – UTC / Europe/London)
#   --wifi-country XX         země pro Wi-Fi (výchozí CZ; nastaví se jen když ještě
#                             není nastavena – bez ní je Wi-Fi na čerstvém Pi zablokovaná)
#   --no-reboot               nerestartovat automaticky (ani když by byl potřeba)
#   --force                   dovolit instalaci starší verze, než je nainstalovaná
#
# Skript je idempotentní – lze jej spustit znovu (např. po ruční aktualizaci).
# Nikdy nečte ze stdin (funguje i přes "curl | sudo bash").
# =============================================================================
set -euo pipefail
exec </dev/null          # žádné dotazy – ani apt, ani nic jiného nesmí čekat na vstup

OVERLAY=0
CF_TOKEN=""
NB_KEY=""
NB_URL=""
HOTSPOT=1
TZONE=""
WIFI_COUNTRY=""
NO_REBOOT=0
FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --overlay) OVERLAY=1 ;;
        --cloudflare-token) CF_TOKEN="${2:-}"; shift ;;
        --netbird-setup-key) NB_KEY="${2:-}"; shift ;;
        --netbird-url) NB_URL="${2:-}"; shift ;;
        --no-hotspot) HOTSPOT=0 ;;
        --timezone) TZONE="${2:-}"; shift ;;
        --wifi-country) WIFI_COUNTRY="${2:-}"; shift ;;
        --no-reboot) NO_REBOOT=1 ;;
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "Neznámá volba: $1"; exit 1 ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || { echo "Spusťte jako root: sudo $0"; exit 1; }

if [ -n "$WIFI_COUNTRY" ]; then
    WIFI_COUNTRY=$(echo "$WIFI_COUNTRY" | tr 'a-z' 'A-Z')
    echo "$WIFI_COUNTRY" | grep -Eq '^[A-Z]{2}$' || { echo "--wifi-country: očekáván dvoupísmenný kód země (např. CZ)"; exit 1; }
    WIFI_COUNTRY_EXPLICIT=1
else
    WIFI_COUNTRY=CZ
    WIFI_COUNTRY_EXPLICIT=0
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$SRC/server/retencka/__main__.py" ] || { echo "Chybí $SRC/server – spouštějte z repozitáře."; exit 1; }
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/server/retencka/__init__.py")
BASE=/var/lib/retencka
APP=$BASE/app
DATA=$BASE/data
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
NEED_REBOOT=0

step() { echo -e "\n\033[1;36m==> $*\033[0m"; }

# --------------------------------------------------------------------- balíčky
step "Balíčky"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-spidev openssl avahi-daemon avahi-utils \
    network-manager dnsmasq-base wpasupplicant rfkill iw nftables fake-hwclock sqlite3 curl ca-certificates >/dev/null
apt-get install -y -qq python3-libgpiod >/dev/null 2>&1 || echo "python3-libgpiod není k dispozici – použije se polling"

# ------------------------------------------- nová verze vs. nainstalovaná verze
OLD_VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$APP/current/server/retencka/__init__.py" 2>/dev/null || true)
if [ -n "$OLD_VER" ]; then
    CMP=$(python3 - "$SRC/system/bin/retencka-update.py" "$VER" "$OLD_VER" <<'PYEOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("upd", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("older" if m.vtuple(sys.argv[2]) < m.vtuple(sys.argv[3]) else "ok")
PYEOF
) || CMP=ok
    if [ "$CMP" = "older" ] && [ "$FORCE" -ne 1 ]; then
        echo "Nainstalovaná je novější verze ($OLD_VER) než tato ($VER) – instalace zrušena."
        echo "Pokud chcete opravdu přejít na starší verzi, spusťte znovu s volbou --force."
        exit 1
    fi
fi

# ------------------------------------------------- Wi-Fi: země (regulatorní doména)
step "Wi-Fi (země)"
CUR_CC=$(iw reg get 2>/dev/null | sed -n 's/^country \([A-Z0-9][A-Z0-9]\).*/\1/p' | head -n1 || true)
if [ -z "$CUR_CC" ] && command -v raspi-config >/dev/null; then
    CUR_CC=$(raspi-config nonint get_wifi_country 2>/dev/null | head -n1 || true)
fi
if [ "$WIFI_COUNTRY_EXPLICIT" -eq 1 ] || [ -z "$CUR_CC" ] || [ "$CUR_CC" = "00" ]; then
    if command -v raspi-config >/dev/null; then
        raspi-config nonint do_wifi_country "$WIFI_COUNTRY" || true
    fi
    iw reg set "$WIFI_COUNTRY" 2>/dev/null || true
    echo "Země pro Wi-Fi: $WIFI_COUNTRY"
else
    echo "Země pro Wi-Fi už je nastavena: $CUR_CC"
fi
# bez země je Wi-Fi na čerstvém Pi OS Lite zablokovaná (rfkill) a hotspot by nenaběhl
rfkill unblock wifi 2>/dev/null || true

# ------------------------------------------------------------ HW: SPI, watchdog
step "SPI a hardwarový watchdog"
command -v raspi-config >/dev/null && raspi-config nonint do_spi 0 || true
CFG="$BOOT/config.txt"
if [ -f "$CFG" ]; then
    # řádky patří do sekce [all]; je-li poslední sekce jiná (např. [cm4]), přidá se [all]
    MISSING=()
    grep -q '^dtparam=spi=on' "$CFG" || MISSING+=('dtparam=spi=on')
    grep -q '^dtparam=watchdog=on' "$CFG" || MISSING+=('dtparam=watchdog=on')
    if [ ${#MISSING[@]} -gt 0 ]; then
        [ -z "$(tail -c1 "$CFG")" ] || echo >> "$CFG"
        LAST_SECTION=$(grep -E '^[[:space:]]*\[[^]]*\][[:space:]]*$' "$CFG" | tail -n1 | tr -d '[:space:]' || true)
        if [ -n "$LAST_SECTION" ] && [ "$LAST_SECTION" != "[all]" ]; then
            printf '\n[all]\n' >> "$CFG"
        fi
        printf '%s\n' "${MISSING[@]}" >> "$CFG"
    fi
fi
# SPI zapnout hned, bez restartu
command -v dtparam >/dev/null && dtparam spi=on >/dev/null 2>&1 || true
modprobe spi_bcm2835 2>/dev/null || true
modprobe spidev 2>/dev/null || true
for _ in 1 2 3 4 5 6; do [ -e /dev/spidev0.0 ] && break; sleep 1; done
if [ -e /dev/spidev0.0 ]; then
    echo "SPI: /dev/spidev0.0 je k dispozici (restart kvůli SPI není potřeba)"
else
    echo "SPI: /dev/spidev0.0 zatím chybí – po restartu by se mělo objevit"
    NEED_REBOOT=1
fi
# dtparam=watchdog=on se projeví až po restartu – jen pokud jsme jej právě přidali a zařízení chybí
if [ ! -e /dev/watchdog ] && [[ " ${MISSING[*]:-} " == *"dtparam=watchdog=on"* ]]; then NEED_REBOOT=1; fi
mkdir -p /etc/systemd/system.conf.d
install -m 644 "$SRC/system/config/system-watchdog.conf" /etc/systemd/system.conf.d/retencka-watchdog.conf
systemctl daemon-reexec || true      # RuntimeWatchdog se z system.conf.d načte až po re-exec

# --------------------------------------------------------------------- uživatel
step "Uživatel a adresáře"
getent group spi >/dev/null || groupadd --system spi
getent group gpio >/dev/null || groupadd --system gpio
id retencka >/dev/null 2>&1 || useradd --system --home-dir "$BASE" --no-create-home \
    --shell /usr/sbin/nologin --groups spi,gpio retencka
usermod -aG spi,gpio retencka
install -d -m 755 -o root -g root "$BASE" "$APP" "$APP/releases"
install -d -m 750 -o retencka -g retencka "$DATA"

# ------------------------------------------------------------------- aplikace
step "Aplikace verze $VER"
# Nová verze se připraví vedle staré, teprve pak se přepne odkaz "current"
# (ten nikdy neukazuje do smazaného adresáře).
OLD_TARGET=$(readlink -f "$APP/current" 2>/dev/null || true)
NAME="$VER"
if [ "$OLD_TARGET" = "$APP/releases/$NAME" ]; then
    NAME="$VER+$(date +%s)"          # stejná verze se instaluje znovu
fi
REL="$APP/releases/$NAME"
rm -rf "$REL.new" "$REL"
mkdir -p "$REL.new"
cp -a "$SRC/server" "$SRC/system" "$REL.new/"
find "$REL.new" -name '__pycache__' -prune -exec rm -rf {} +
rm -rf "$REL.new/server/tools/__pycache__"
python3 -m compileall -q "$REL.new/server/retencka"
chown -R root:root "$REL.new"
chmod -R u=rwX,go=rX "$REL.new"
chmod 755 "$REL.new"/system/bin/*
mv "$REL.new" "$REL"
ln -sfn "releases/$NAME" "$APP/current.new"
mv -T "$APP/current.new" "$APP/current"
# stará kopie téže verze už není potřeba (jiné verze zůstávají pro návrat)
case "$(basename "$OLD_TARGET")" in
    "$VER"|"$VER"+*) [ "$OLD_TARGET" != "$REL" ] && rm -rf "$OLD_TARGET" ;;
esac

# ---------------------------------------------------------------- konfigurace
step "Konfigurace"
# Denní součty (spotřeba, dopouštění) a „dnes“ se počítají v místním čase Pi.
CUR_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo "")
if [ -n "$TZONE" ] || [ -z "$CUR_TZ" ] || [ "$CUR_TZ" = "Etc/UTC" ] || [ "$CUR_TZ" = "UTC" ] || [ "$CUR_TZ" = "Europe/London" ]; then
    timedatectl set-timezone "${TZONE:-Europe/Prague}"
fi
echo "Časová zóna: $(timedatectl show -p Timezone --value 2>/dev/null || echo '?')"
DEF=/etc/default/retencka
if [ ! -f "$DEF" ]; then
    install -m 644 "$SRC/system/config/default-retencka" "$DEF"
else
    # existující soubor se nepřepisuje; chybějící klíče se doplní jako zakomentované výchozí hodnoty
    ADDED=0
    while IFS= read -r line; do
        case "$line" in
            [A-Z_]*=*) key="${line%%=*}" ;;
            *) continue ;;
        esac
        grep -Eq "^[[:space:]]*#?[[:space:]]*${key}=" "$DEF" && continue
        if [ "$ADDED" -eq 0 ]; then
            [ -z "$(tail -c1 "$DEF")" ] || echo >> "$DEF"
            printf '\n# --- doplněno instalací verze %s (výchozí hodnoty, odkomentujte podle potřeby) ---\n' "$VER" >> "$DEF"
            ADDED=1
        fi
        printf '# %s\n' "$line" >> "$DEF"
    done < "$SRC/system/config/default-retencka"
fi
install -d -m 755 /etc/retencka
if [ -f "$SRC/system/keys/release.pub" ]; then
    install -m 644 "$SRC/system/keys/release.pub" /etc/retencka/release.pub
else
    echo "Upozornění: system/keys/release.pub chybí – automatické aktualizace budou odmítnuty."
fi
mkdir -p /etc/systemd/journald.conf.d
install -m 644 "$SRC/system/config/journald-retencka.conf" /etc/systemd/journald.conf.d/retencka.conf
install -m 644 "$SRC/system/config/tmpfiles-retencka.conf" /etc/tmpfiles.d/retencka.conf
systemd-tmpfiles --create /etc/tmpfiles.d/retencka.conf
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
install -m 644 "$SRC/system/config/nm-captive.conf" /etc/NetworkManager/dnsmasq-shared.d/retencka-captive.conf

# ------------------------------------------------------ šetrnost k SD kartě
step "Ochrana SD karty"
if grep -qE '^\S+\s+/\s+ext4\s+defaults,noatime\s' /etc/fstab && ! grep -qE '^\S+\s+/\s+ext4\s+\S*commit=' /etc/fstab; then
    sed -i -E 's#^(\S+\s+/\s+ext4\s+)defaults,noatime(\s)#\1defaults,noatime,commit=60\2#' /etc/fstab
fi
if systemctl list-unit-files dphys-swapfile.service >/dev/null 2>&1; then
    systemctl disable --now dphys-swapfile.service >/dev/null 2>&1 || true
    command -v dphys-swapfile >/dev/null && dphys-swapfile uninstall >/dev/null 2>&1 || true
fi
# zbytečné služby (šetří RAM)
for s in bluetooth.service hciuart.service triggerhappy.service ModemManager.service; do
    systemctl disable --now "$s" >/dev/null 2>&1 || true
done

# ------------------------------------------------------------- systemd units
step "Služby"
for u in "$SRC"/system/systemd/*; do
    install -m 644 "$u" /etc/systemd/system/
done
systemctl daemon-reload
systemctl restart systemd-journald || true
systemctl enable --now avahi-daemon.service >/dev/null
systemctl enable retencka-identity.service >/dev/null
systemctl restart retencka-identity.service || echo "Upozornění: identita zařízení se nepodařila vytvořit (chybí síťové rozhraní?) – zkuste za chvíli: sudo systemctl restart retencka-identity"
systemctl enable --now retencka.service retencka-alias.service retencka-update.timer retencka-update.path \
    retencka-control.path retencka-control.timer >/dev/null
systemctl restart retencka.service
if [ "$HOTSPOT" -eq 1 ]; then
    systemctl enable --now NetworkManager.service retencka-netwatch.service >/dev/null
    systemctl restart retencka-netwatch.service
else
    systemctl disable --now retencka-netwatch.service >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- cloudflared
if [ -n "$CF_TOKEN" ]; then
    step "Cloudflare Tunnel"
    if ! command -v cloudflared >/dev/null; then
        install -d -m 755 /usr/share/keyrings
        curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
        echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
            > /etc/apt/sources.list.d/cloudflared.list
        apt-get update -qq && apt-get install -y -qq cloudflared >/dev/null
    fi
    cloudflared service install "$CF_TOKEN" || true
    echo "V Cloudflare Zero Trust nasměrujte hostname na http://localhost:80 (NE na port 8081)."
fi

# ------------------------------------------------------------------- NetBird
if [ -n "$NB_KEY" ]; then
    step "NetBird (vzdálený servis)"
    # stejná instalace jako z webu (retencka-control.py → retencka-netbird-install.sh)
    if NB_SETUP_KEY="$NB_KEY" NB_MANAGEMENT_URL="$NB_URL" bash "$SRC/system/bin/retencka-netbird-install.sh"; then
        # access during the installation; afterwards only if the customer allows it
        printf '{"mode": "24h", "until": %d, "by": "instalace"}\n' $(( $(date +%s) + 86400 )) > "$DATA/servis.json"
        chown retencka:retencka "$DATA/servis.json"
        systemctl start retencka-control.service || true
        echo "NetBird připojen. Servisní přístup je povolen na 24 h (pak jen se souhlasem zákazníka)."
    else
        echo "Upozornění: NetBird se nepřipojil – zkontrolujte setup key."
    fi
fi

# --------------------------------------------------------------- overlay FS
if [ "$OVERLAY" -eq 1 ]; then
    step "Read-only root (overlayfs)"
    if mountpoint -q "$BASE"; then
        raspi-config nonint do_overlayfs 0
        echo "Overlay zapnut – projeví se po restartu. Data a aplikace zůstávají zapisovatelné na $BASE."
        NEED_REBOOT=1
    else
        echo "PŘESKOČENO: $BASE není samostatný oddíl – s overlayem by se ztrácela data i aktualizace."
        echo "Viz docs/instalace.md, oddíl 'Read-only systém'."
    fi
fi

# --------------------------------------------------------------------- souhrn
step "Hotovo"
INFO="$BOOT/retencka.txt"
HOST=$(hostname)
SSID=$(sed -n 's/^hotspot_ssid=//p' "$INFO" 2>/dev/null | head -n1 || true)
PASS=$(sed -n 's/^hotspot_password=//p' "$INFO" 2>/dev/null | head -n1 || true)

# adresa a port zdravotního dotazu z /etc/default/retencka (stejně jako ji čte služba)
HA_PORT=$( ( set +u; . /etc/default/retencka >/dev/null 2>&1; echo "${RETENCKA_HA_PORT:-8081}" ) || echo 8081 )
BIND=$( ( set +u; . /etc/default/retencka >/dev/null 2>&1; echo "${RETENCKA_BIND:-0.0.0.0}" ) || echo 0.0.0.0 )
case "$BIND" in ""|0.0.0.0|::) BIND=127.0.0.1 ;; esac

# počkat (max. 30 s), až web odpoví
HTTP=000
for _ in $(seq 1 15); do
    HTTP=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://$BIND:$HA_PORT/health" 2>/dev/null || true)
    case "$HTTP" in 200|503) break ;; esac
    sleep 2
done

ok()   { printf '  \033[1;32m[ OK ]\033[0m %s\n' "$*"; }
fail() { printf '  \033[1;31m[CHYBA]\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m[POZOR]\033[0m %s\n' "$*"; }

ETH=0
nmcli -t -f TYPE,STATE device 2>/dev/null | grep -q '^ethernet:connected' && ETH=1
IPADDR=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1 || true)

echo
echo "╔══════════════════════════════════════════════════════════════════"
echo "║  RETENČNÍ NÁDRŽ – instalace dokončena (verze $VER)"
echo "╠══════════════════════════════════════════════════════════════════"
echo "║  Web:          http://$HOST.local   (případně http://retencka.local)"
[ -n "$IPADDR" ] && echo "║  Podle IP:     http://$IPADDR"
echo "║  Zařízení:     $HOST"
echo "║  Hotspot:      ${SSID:-?}   (heslo: ${PASS:-?})"
echo "║  První přihlášení na web: uživatel admin, heslo stejné jako heslo hotspotu"
echo "║  (údaje jsou i v $INFO – vytiskněte je na štítek)"
echo "╚══════════════════════════════════════════════════════════════════"
echo
echo "Kontrola:"
if systemctl is-active --quiet retencka.service; then ok "služba retencka běží"; else fail "služba retencka neběží (journalctl -u retencka -n 50)"; fi
if [ -e /dev/spidev0.0 ]; then ok "SPI zařízení /dev/spidev0.0 existuje"; else fail "SPI zařízení /dev/spidev0.0 chybí (po restartu se objeví)"; fi
case "$HTTP" in
    200) ok "web odpovídá (http://$BIND:$HA_PORT/health)" ;;
    503) ok "web odpovídá"; warn "rádio CC1101 zatím neodpovídá – zkontrolujte zapojení (docs/zapojeni_rpi.md)" ;;
    *)   fail "web neodpovídá (kód $HTTP)" ;;
esac
if [ -n "$PASS" ]; then ok "heslo zařízení vytvořeno"; else fail "heslo zařízení chybí ($INFO)"; fi
if [ "$HOTSPOT" -eq 1 ]; then
    if [ "$ETH" -eq 1 ]; then
        echo
        echo "Zařízení je připojeno kabelem (Ethernet) – záložní hotspot zůstane vypnutý,"
        echo "dokud je kabel zapojen. Bez kabelu a bez známé Wi-Fi se hotspot spustí sám."
    else
        echo
        echo "Bez kabelu a bez známé Wi-Fi se do 3 minut spustí hotspot $SSID – připojte se k němu"
        echo "telefonem a vyberte domácí Wi-Fi."
    fi
fi

if [ "$NEED_REBOOT" -eq 1 ]; then
    echo
    if [ "$NO_REBOOT" -eq 1 ]; then
        warn "Je potřeba restart (SPI / watchdog / overlay): sudo reboot"
    else
        echo "Je potřeba restart – zařízení se restartuje za 10 s (Ctrl+C zruší)."
        trap 'echo; echo "Restart zrušen. Restartujte později: sudo reboot"; exit 0' INT
        for i in 10 9 8 7 6 5 4 3 2 1; do printf '\r  restart za %2d s ' "$i"; sleep 1; done
        echo
        systemctl reboot
    fi
else
    echo
    echo "Restart není potřeba. Hotovo."
fi
