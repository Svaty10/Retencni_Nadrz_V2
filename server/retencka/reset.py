"""Factory reset: back to the state right after installation.

The web only leaves a marker file and restarts the app; the wipe itself runs at the
next start, before the database is opened (no open handles, nothing half-deleted while
the app runs). An interrupted wipe is simply repeated, the marker goes last.

Deleted: the database (measurements, settings, users, sessions, sensor keys, Telegram,
HA token), local backups, update / service-access preferences and every other file of
the app. Kept: the device identity (setup_code = the label password, so the first login
works again).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from typing import Optional

log = logging.getLogger("retencka.reset")

MARKER = "factory-reset"
KEEP = {"setup_code", MARKER}


def request(data_dir: str, user: str) -> None:
    tmp = os.path.join(data_dir, MARKER + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"user": user[:64], "ts": int(time.time())}, f)
    os.replace(tmp, os.path.join(data_dir, MARKER))


def pending(data_dir: str) -> bool:
    return os.path.isfile(os.path.join(data_dir, MARKER))


def apply(data_dir: str) -> Optional[dict]:
    """Wipe the data directory when a reset was requested. Returns the request
    ({"user", "ts"}) after a wipe, None when nothing was requested."""
    marker = os.path.join(data_dir, MARKER)
    if not os.path.isfile(marker):
        return None
    try:
        with open(marker, encoding="utf-8") as f:
            info = json.load(f)
        if not isinstance(info, dict):
            info = {}
    except (OSError, ValueError):
        info = {}
    log.warning("Factory reset requested by %s – wiping %s", info.get("user", "?"), data_dir)
    for entry in os.scandir(data_dir):
        if entry.name in KEEP:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except OSError as e:
            log.error("factory reset: %s not deleted: %s", entry.name, e)
    os.unlink(marker)
    return {"user": str(info.get("user") or "?"), "ts": info.get("ts")}
