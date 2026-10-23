#!/bin/bash
# =============================================================================
# Retenční nádrž – instalace jedním příkazem (Raspberry Pi OS Lite):
#
#   curl -fsSL https://raw.githubusercontent.com/Svaty10/Retencni_Nadrz_V2/main/system/bootstrap.sh | sudo bash
#
# S volbami instalace (viz system/install.sh --help):
#
#   curl -fsSL https://raw.githubusercontent.com/Svaty10/Retencni_Nadrz_V2/main/system/bootstrap.sh | sudo bash -s -- --cloudflare-token TOKEN
#
# Skript nainstaluje git (je-li potřeba), stáhne repozitář do /opt/retencka-src
# (nebo jej aktualizuje) a spustí system/install.sh se stejnými volbami.
# Nikdy nečte ze stdin, takže funguje i přes rouru.
# =============================================================================
set -euo pipefail
exec </dev/null

REPO_URL="${RETENCKA_REPO_URL:-https://github.com/Svaty10/Retencni_Nadrz_V2.git}"
BRANCH="${RETENCKA_BRANCH:-main}"
DEST=/opt/retencka-src

[ "$(id -u)" -eq 0 ] || { echo "Spusťte jako root:  curl -fsSL <adresa> | sudo bash"; exit 1; }

# Raspberry Pi OS / Debian s apt a systemd
if [ ! -r /etc/os-release ] || ! grep -Eqi '^(ID|ID_LIKE)=.*(debian|raspbian)' /etc/os-release; then
    echo "Tento instalátor je určen pro Raspberry Pi OS (Debian). Jiný systém nebyl rozpoznán."
    exit 1
fi
command -v apt-get >/dev/null && command -v systemctl >/dev/null || {
    echo "Chybí apt-get nebo systemctl – podporován je jen Raspberry Pi OS Lite."; exit 1; }
if [ -r /proc/device-tree/model ] && ! tr -d '\0' < /proc/device-tree/model | grep -qi 'raspberry'; then
    echo "Upozornění: nejde o Raspberry Pi – instalace pokračuje, ale SPI/rádio nemusí fungovat."
fi

export DEBIAN_FRONTEND=noninteractive
if ! command -v git >/dev/null; then
    echo "==> Instaluji git"
    apt-get update -qq
    apt-get install -y -qq git ca-certificates >/dev/null
fi

if [ -d "$DEST/.git" ]; then
    echo "==> Aktualizuji $DEST"
    git -C "$DEST" fetch --quiet origin "$BRANCH"
    git -C "$DEST" checkout --quiet "$BRANCH"
    git -C "$DEST" pull --ff-only --quiet origin "$BRANCH" || {
        echo "git pull --ff-only selhal (místní změny v $DEST?). Smažte složku a spusťte znovu:  sudo rm -rf $DEST"
        exit 1
    }
else
    echo "==> Stahuji repozitář do $DEST"
    rm -rf "$DEST"
    git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$DEST"
fi

exec bash "$DEST/system/install.sh" "$@"
