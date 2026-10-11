#!/bin/bash
# =============================================================================
# retencka-restore – restore the database from a backup on the boot partition
#
# Runs as root before the server starts (ExecStartPre=+ in retencka.service).
# Typical use after an SD card failure: flash a new card, copy the backup file
# downloaded from the web UI (Nastavení → Záloha) to the boot partition as
#     retencka-restore.db   (or retencka-restore.db.gz)
# (the FAT partition visible in Windows), insert the card and power on.
# The file is checked, the current database is kept aside, and the source is
# renamed to *.done so the restore runs only once.
# =============================================================================
set -u
DATA=/var/lib/retencka/data
DB="$DATA/retencka.db"
for BOOT in /boot/firmware /boot; do
    for SRC in "$BOOT/retencka-restore.db.gz" "$BOOT/retencka-restore.db"; do
        [ -f "$SRC" ] || continue
        TMP="$DATA/restore.tmp"
        rm -f "$TMP"
        case "$SRC" in
            *.gz) gzip -dc "$SRC" > "$TMP" 2>/dev/null || { echo "restore: $SRC is not a valid gzip"; mv -f "$SRC" "$SRC.invalid"; rm -f "$TMP"; continue; } ;;
            *)    cp "$SRC" "$TMP" ;;
        esac
        if ! python3 - "$TMP" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
sys.exit(0 if ok and {"measurements", "users", "settings"} <= tables else 1)
PY
        then
            echo "restore: $SRC is not a valid Retencka database – ignored"
            mv -f "$SRC" "$SRC.invalid"
            rm -f "$TMP"
            continue
        fi
        mkdir -p "$DATA/backup"
        if [ -f "$DB" ]; then
            stamp=$(date +%Y%m%d-%H%M%S)
            for s in "" -wal -shm; do
                [ -f "$DB$s" ] && mv -f "$DB$s" "$DATA/backup/pre-restore-$stamp.db$s"
            done
        fi
        mv -f "$TMP" "$DB"
        chown retencka:retencka "$DB"
        chmod 640 "$DB"
        mv -f "$SRC" "$SRC.done"
        echo "restore: database restored from $SRC"
        exit 0
    done
done
exit 0
