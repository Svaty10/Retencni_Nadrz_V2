#!/usr/bin/env python3
"""retencka-control – root helper for requests from the (unprivileged) hub.

The hub runs sandboxed without root rights. For the few actions that need root it
writes a request file into its data directory; retencka-control.path starts this
helper, which carries the request out:

  control-reboot   restart the whole device
  control-servis   apply the wanted remote service access (servis.json):
                   NetBird up / down; "24h" expires automatically

retencka-control.timer runs it every 5 minutes as well (expiry of the 24 h access,
status for the web UI in servis-status.json).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

DATA = Path("/var/lib/retencka/data")
WANT = DATA / "servis.json"
STATUS = DATA / "servis-status.json"


def log(msg: str) -> None:
    print(msg, flush=True)                      # journald


def run(*cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    shutil.chown(tmp, "retencka", "retencka")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def wanted() -> dict:
    try:
        w = json.loads(WANT.read_text())
    except (OSError, ValueError):
        w = {}
    mode = w.get("mode", "off")
    if mode == "24h" and (w.get("until") or 0) < time.time():
        log("servis: 24 h access expired")
        w.update(mode="off", until=None, by="system (vypršelo)")
        write_json(WANT, w)
        mode = "off"
    return {"mode": mode if mode in ("off", "24h", "on") else "off"}


def netbird_status() -> dict:
    """Connected? + NetBird IP (text output parsed, works across client versions)."""
    try:
        out = run("netbird", "status", timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"connected": False, "ip": None}
    connected = bool(re.search(r"Management:\s*Connected", out))
    m = re.search(r"NetBird IP:\s*([0-9.]+)", out)
    return {"connected": connected, "ip": m.group(1) if m else None}


def apply_servis() -> None:
    installed = shutil.which("netbird") is not None
    mode = wanted()["mode"]
    if installed:
        st = netbird_status()
        try:
            if mode == "off" and st["connected"]:
                log("servis: netbird down")
                run("netbird", "down")
            elif mode != "off" and not st["connected"]:
                log("servis: netbird up")
                run("netbird", "up", timeout=90)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"servis: netbird failed: {e}")
        st = netbird_status()
    else:
        st = {"connected": False, "ip": None}
    write_json(STATUS, {"installed": installed, **st, "checked": int(time.time())})


def main() -> None:
    if (DATA / "control-reboot").exists():
        (DATA / "control-reboot").unlink(missing_ok=True)
        log("reboot requested from the hub")
        subprocess.run(["systemctl", "reboot"], check=False)
        return
    (DATA / "control-servis").unlink(missing_ok=True)
    apply_servis()


if __name__ == "__main__":
    main()
