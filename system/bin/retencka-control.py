#!/usr/bin/env python3
"""retencka-control – root helper for requests from the (unprivileged) hub.

The hub runs sandboxed without root rights. For the few actions that need root it
writes a request file into its data directory; retencka-control.path starts this
helper, which carries the request out:

  control-reboot   restart the whole device
  control-poweroff shut the device down (power it on again by unplugging / plugging in)
  control-servis   apply the wanted remote service access (servis.json):
                   NetBird up / down; "24h" expires automatically
  control-netbird-install
                   {"setup_key": "…", "management_url": "https://…" | ""} – install
                   NetBird (retencka-netbird-install.sh, same as install.sh) and allow
                   access for 24 h; progress in netbird-install.json. The key is read,
                   the file deleted at once, and the key is never logged or written back.
  control-tls      check the HTTPS certificate now (the app asks when it has none yet)

Every run also checks the hub's own HTTPS certificate (retencka_tls.py): the local CA
is created once, the server certificate renewed before it expires or when the device's
name / IP address changed.

retencka-control.timer runs it every 5 minutes as well (expiry of the 24 h access,
status for the web UI in servis-status.json).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retencka_tls  # noqa: E402

DATA = Path("/var/lib/retencka/data")
WANT = DATA / "servis.json"
STATUS = DATA / "servis-status.json"
NB_REQUEST = DATA / "control-netbird-install"
NB_STATUS = DATA / "netbird-install.json"
NB_SCRIPT = Path(__file__).resolve().parent / "retencka-netbird-install.sh"
NB_KEY_RE = re.compile(r"^[A-Za-z0-9-]{20,80}$")
NB_URL_RE = re.compile(r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?/?$")
SERVIS_24H_S = 24 * 3600


def log(msg: str) -> None:
    print(msg, flush=True)                      # journald


def run(*cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


def write_json(path: Path, data: dict) -> None:
    """Atomic; random temp name created exclusively (a symlink planted by the app in the
    data directory is replaced, never written through)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))
        try:
            shutil.chown(tmp, "retencka", "retencka")
        except (LookupError, OSError):
            pass
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_small(path: Path, limit: int = 4096) -> Optional[bytes]:
    """A small regular file written by the app; no symlinks; None when missing."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError:
        return b""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        data = os.read(fd, limit + 1)
        return b"" if len(data) > limit else data
    finally:
        os.close(fd)


def parse_netbird_request(raw: Optional[bytes]) -> Optional[dict]:
    """{"setup_key", "management_url"} validated strictly, else None.

    >>> parse_netbird_request(b'{"setup_key": "ABCDEF01-2345-6789-ABCD-EF0123456789", "management_url": ""}')
    {'setup_key': 'ABCDEF01-2345-6789-ABCD-EF0123456789', 'management_url': ''}
    >>> parse_netbird_request(b'{"setup_key": "x; rm -rf /", "management_url": ""}') is None
    True
    >>> parse_netbird_request(b'{"setup_key": "ABCDEF01-2345-6789-ABCD", "management_url": "http://x.cz"}') is None
    True
    """
    try:
        obj = json.loads((raw or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    key, url = obj.get("setup_key"), obj.get("management_url") or ""
    if not isinstance(key, str) or not NB_KEY_RE.match(key) or not isinstance(url, str):
        return None
    if url and not NB_URL_RE.match(url):
        return None
    return {"setup_key": key, "management_url": url}


def nb_status(state: str, message: str) -> None:
    write_json(NB_STATUS, {"state": state, "message": message, "updated_at": int(time.time())})
    log(f"netbird install: [{state}] {message}")


def install_netbird() -> None:
    raw = read_small(NB_REQUEST)
    NB_REQUEST.unlink(missing_ok=True)                 # contains the secret: delete first
    if raw is None:
        return
    req = parse_netbird_request(raw)
    del raw
    if req is None:
        nb_status("error", "Neplatný setup key nebo adresa serveru.")
        return
    nb_status("installing", "Instaluji vzdálený servis…")
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
           "NB_SETUP_KEY": req["setup_key"], "NB_MANAGEMENT_URL": req["management_url"]}
    try:
        proc = subprocess.Popen(["/bin/bash", str(NB_SCRIPT)], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        last = ""
        assert proc.stdout is not None
        for line in proc.stdout:                       # the script never prints the key
            line = line.strip()
            if line.startswith("STEP: "):
                nb_status("installing", line[6:])
            elif line:
                last = line[-300:]
        rc = proc.wait(timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        nb_status("error", f"Instalace selhala: {e}")
        return
    if rc != 0:
        nb_status("error", "Instalace se nezdařila" + (f": {last}" if last else "") +
                  " – zkontrolujte setup key a připojení k internetu.")
        return
    # access during the setup, afterwards only with the customer's consent (as install.sh)
    write_json(WANT, {"mode": "24h", "until": int(time.time()) + SERVIS_24H_S, "by": "instalace z webu"})
    nb_status("done", "Vzdálený servis je nainstalovaný a připojený, přístup povolen na 24 hodin.")


def wanted() -> dict:
    try:
        w = json.loads((read_small(WANT) or b"").decode("utf-8"))
        if not isinstance(w, dict):
            w = {}
    except (UnicodeDecodeError, ValueError):
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
    if (DATA / "control-poweroff").exists() or (DATA / "control-poweroff").is_symlink():
        (DATA / "control-poweroff").unlink(missing_ok=True)
        (DATA / "control-reboot").unlink(missing_ok=True)
        log("power-off requested from the hub")
        subprocess.run(["systemctl", "poweroff"], check=False)
        return
    if (DATA / "control-reboot").exists():
        (DATA / "control-reboot").unlink(missing_ok=True)
        log("reboot requested from the hub")
        subprocess.run(["systemctl", "reboot"], check=False)
        return
    if NB_REQUEST.exists() or NB_REQUEST.is_symlink():
        install_netbird()
    # drop every other request file too, otherwise the .path unit (glob control-*)
    # would restart this helper in a loop
    for f in DATA.glob("control-*"):
        try:
            if f.is_dir() and not f.is_symlink():
                shutil.rmtree(f)                   # planted directory (rmtree never follows links)
            else:
                f.unlink(missing_ok=True)
        except OSError as e:
            log(f"cannot remove {f.name}: {e}")
    apply_servis()
    try:
        retencka_tls.ensure()
    except Exception as e:                      # never block the other duties
        log(f"tls: certificate check failed: {e}")


if __name__ == "__main__":
    main()
