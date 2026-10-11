#!/bin/bash
# =============================================================================
# Retenční nádrž – instalace na Raspberry Pi OS Lite (Bookworm / Trixie)
#
#   git clone https://github.com/Svaty10/Retencni_Nadrz_V2.git
#   cd Retencni_Nadrz_V2 && sudo ./system/install.sh
#
# Volby:
#   --overlay                 po instalaci zapnout read-only root (overlayfs);
#                             jen pokud je /var/lib/retencka samostatný oddíl
#   --cloudflare-token TOKEN  nainstalovat cloudflared a připojit tunel
#   --no-hotspot              nespouštět hlídání Wi-Fi / záložní hotspot
#   --timezone ZÓNA           časová zóna (výchozí Europe/Prague; bez volby se
#                             mění jen výchozí zóna Pi OS – UTC / Europe/London)
#
# Skript je idempotentní – lze jej spustit znovu (např. po ruční aktualizaci).
# =============================================================================
set -euo pipefail

OVERLAY=0
CF_TOKEN=""
HOTSPOT=1
TZONE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --overlay) OVERLAY=1 ;;
        --cloudflare-token) CF_TOKEN="${2:-}"; shift ;;
        --no-hotspot) HOTSPOT=0 ;;
        --timezone) TZONE="${2:-}"; shift ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "Neznámá volba: $1"; exit 1 ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || { echo "Spusťte jako root: sudo $0"; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$SRC/server/retencka/__main__.py" ] || { echo "Chybí $SRC/server – spouštějte z repozitáře."; exit 1; }
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/server/retencka/__init__.py")
BASE=/var/lib/retencka
APP=$BASE/app
DATA=$BASE/data
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot

step() { echo -e "\n\033[1;36m==> $*\033[0m"; }

# --------------------------------------------------------------------- balíčky
step "Balíčky"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-spidev openssl avahi-daemon avahi-utils \
    network-manager iw nftables fake-hwclock sqlite3 >/dev/null
apt-get install -y -qq python3-libgpiod >/dev/null 2>&1 || echo "python3-libgpiod není k dispozici – použije se polling"

# ------------------------------------------------------------ HW: SPI, watchdog
step "SPI a hardwarový watchdog"
command -v raspi-config >/dev/null && raspi-config nonint do_spi 0 || true
CFG="$BOOT/config.txt"
if [ -f "$CFG" ]; then
    grep -q '^dtparam=spi=on' "$CFG" || echo 'dtparam=spi=on' >> "$CFG"
    grep -q '^dtparam=watchdog=on' "$CFG" || echo 'dtparam=watchdog=on' >> "$CFG"
fi
mkdir -p /etc/systemd/system.conf.d
install -m 644 "$SRC/system/config/system-watchdog.conf" /etc/systemd/system.conf.d/retencka-watchdog.conf

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
REL="$APP/releases/$VER"
rm -rf "$REL.new"
mkdir -p "$REL.new"
cp -a "$SRC/server" "$SRC/system" "$REL.new/"
find "$REL.new" -name '__pycache__' -prune -exec rm -rf {} +
rm -rf "$REL.new/server/tools/__pycache__"
python3 -m compileall -q "$REL.new/server/retencka"
chown -R root:root "$REL.new"
chmod -R u=rwX,go=rX "$REL.new"
chmod 755 "$REL.new"/system/bin/*
rm -rf "$REL"
mv "$REL.new" "$REL"
ln -sfn "releases/$VER" "$APP/current.new"
mv -T "$APP/current.new" "$APP/current"

# ---------------------------------------------------------------- konfigurace
step "Konfigurace"
# Denní součty (spotřeba, dopouštění) a „dnes“ se počítají v místním čase Pi.
CUR_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo "")
if [ -n "$TZONE" ] || [ -z "$CUR_TZ" ] || [ "$CUR_TZ" = "Etc/UTC" ] || [ "$CUR_TZ" = "UTC" ]         || [ "$CUR_TZ" = "Europe/London" ]; then
    timedatectl set-timezone "${TZONE:-Europe/Prague}"
fi
echo "Časová zóna: $(timedatectl show -p Timezone --value 2>/dev/null || echo '?')"
[ -f /etc/default/retencka ] || install -m 644 "$SRC/system/config/default-retencka" /etc/default/retencka
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
systemctl restart retencka-identity.service
systemctl enable --now retencka.service retencka-alias.service retencka-update.timer retencka-update.path >/dev/null
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

# --------------------------------------------------------------- overlay FS
if [ "$OVERLAY" -eq 1 ]; then
    step "Read-only root (overlayfs)"
    if mountpoint -q "$BASE"; then
        raspi-config nonint do_overlayfs 0
        echo "Overlay zapnut – projeví se po restartu. Data a aplikace zůstávají zapisovatelné na $BASE."
    else
        echo "PŘESKOČENO: $BASE není samostatný oddíl – s overlayem by se ztrácela data i aktualizace."
        echo "Viz docs/instalace.md, oddíl 'Read-only systém'."
    fi
fi

# --------------------------------------------------------------------- souhrn
step "Hotovo"
sleep 2
INFO="$BOOT/retencka.txt"
cat "$INFO" 2>/dev/null || true
echo
systemctl --no-pager --lines=0 status retencka.service | head -n 5 || true
echo
echo "Web:            http://$(hostname).local  (případně http://retencka.local)"
echo "Home Assistant: http://$(hostname).local:8081/api/v1/water/state  (token v Nastavení)"
echo "Pokud je to první instalace a SPI bylo právě zapnuto, restartujte: sudo reboot"
