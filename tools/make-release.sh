#!/bin/bash
# Vytvoří podepsaný release balík:  tools/make-release.sh /cesta/k/release.key
#   -> dist/retencka-<ver>.tar.gz + dist/retencka-<ver>.tar.gz.sig
# Jednorázové vygenerování klíče (soukromý klíč NIKDY necommitovat):
#   openssl genpkey -algorithm ed25519 -out release.key
#   openssl pkey -in release.key -pubout -out system/keys/release.pub
set -euo pipefail
KEY="${1:?cesta k soukromému klíči release.key}"
cd "$(dirname "$0")/.."
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' server/retencka/__init__.py)
mkdir -p dist
OUT="dist/retencka-$VER.tar.gz"
tar --exclude='__pycache__' --exclude='*.pyc' --owner=0 --group=0 --sort=name \
    --mtime='2026-01-01 00:00:00' -czf "$OUT" server system
openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$OUT" -out "$OUT.sig"
openssl pkeyutl -verify -pubin -inkey system/keys/release.pub -rawin -in "$OUT" -sigfile "$OUT.sig"
echo "Hotovo: $OUT (+ .sig). Nahrajte oba soubory k GitHub Release s tagem v$VER."
