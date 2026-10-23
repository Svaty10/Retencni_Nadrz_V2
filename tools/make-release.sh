#!/bin/bash
# Vytvoří podepsaný release balík:  tools/make-release.sh /cesta/k/release.key
#   -> dist/retencka-<ver>.tar.gz + dist/retencka-<ver>.tar.gz.sig
# Jednorázové vygenerování klíče (soukromý klíč NIKDY necommitovat):
#   openssl genpkey -algorithm ed25519 -out release.key
#   openssl pkey -in release.key -pubout -out system/keys/release.pub
set -euo pipefail
KEY="${1:?cesta k soukromému klíči release.key}"
cd "$(dirname "$0")/.."
[ -s "$KEY" ] || { echo "Soukromý klíč $KEY chybí nebo je prázdný (GitHub secret RELEASE_SIGNING_KEY?)"; exit 1; }
[ -f system/keys/release.pub ] || { echo "Chybí system/keys/release.pub – vygenerujte a commitněte veřejný klíč (docs/instalace.md, oddíl 5)"; exit 1; }
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' server/retencka/__init__.py)
mkdir -p dist
OUT="dist/retencka-$VER.tar.gz"
tar --exclude='__pycache__' --exclude='*.pyc' --owner=0 --group=0 --sort=name \
    --mtime='2026-01-01 00:00:00' -czf "$OUT" server system
openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$OUT" -out "$OUT.sig"
openssl pkeyutl -verify -pubin -inkey system/keys/release.pub -rawin -in "$OUT" -sigfile "$OUT.sig"     || { echo "Podpis nesedí se system/keys/release.pub – secret a veřejný klíč nejsou pár."; exit 1; }
echo "Hotovo: $OUT (+ .sig). Nahrajte oba soubory k GitHub Release s tagem v$VER."
