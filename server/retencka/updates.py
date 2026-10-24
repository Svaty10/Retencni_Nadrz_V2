"""Updates – the application side of the root updater (system/bin/retencka-update.py).

The app runs unprivileged and talks to the updater only through files in the data
directory:
  update-request      {"action": "check"} | {"action": "install", "version": "X"}
                      (its existence starts retencka-update.service via a .path unit)
  update-prefs.json   {"auto": bool, "channel": "stable"|"beta"}
  update-status.json  written by the updater: state, current, latest, notes, message …
The updater validates everything again on its side; the checks here only give the user
a clear error early.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Optional

REQUEST = "update-request"
PREFS = "update-prefs.json"
STATUS = "update-status.json"
CHANNELS = ("stable", "beta")
VERSION_RE = re.compile(r"^\d{1,4}(\.\d{1,4}){1,3}(-[0-9A-Za-z.]{1,20})?$")
# states in which the updater is working; an install must not be requested again
BUSY = ("checking", "downloading", "verifying", "installing", "restarting")
STALE_S = 20 * 60          # a "busy" status older than this is a crashed / killed run


def valid_version(v: Any) -> bool:
    return isinstance(v, str) and bool(VERSION_RE.match(v))


def parse_prefs(obj: Any) -> Optional[dict]:
    """Strict: exactly a bool and a known channel, otherwise None."""
    if not isinstance(obj, dict):
        return None
    auto, channel = obj.get("auto"), obj.get("channel")
    if not isinstance(auto, bool) or channel not in CHANNELS:
        return None
    return {"auto": auto, "channel": channel}


def default_prefs() -> dict:
    """Fallback = /etc/default/retencka (systemd passes it in the environment)."""
    channel = os.environ.get("RETENCKA_CHANNEL", "stable")
    return {"auto": os.environ.get("RETENCKA_AUTO_UPDATE", "1") == "1",
            "channel": channel if channel in CHANNELS else "stable"}


def _write_atomic(data_dir: str, name: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix="." + name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(data_dir, name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(data_dir: str, name: str, limit: int = 1_000_000) -> Any:
    try:
        with open(os.path.join(data_dir, name), "rb") as f:
            raw = f.read(limit + 1)
        return None if len(raw) > limit else json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None


def read_prefs(data_dir: str) -> dict:
    return parse_prefs(_read_json(data_dir, PREFS, 4096)) or default_prefs()


def write_prefs(data_dir: str, auto: Any, channel: Any) -> dict:
    p = parse_prefs({"auto": auto, "channel": channel})
    if p is None:
        raise ValueError("Neplatné nastavení aktualizací.")
    _write_atomic(data_dir, PREFS, json.dumps(p))
    return p


def read_status(data_dir: str) -> Optional[dict]:
    st = _read_json(data_dir, STATUS)
    return st if isinstance(st, dict) else None


def pending(data_dir: str) -> bool:
    return os.path.exists(os.path.join(data_dir, REQUEST))


def busy(status: Optional[dict], now: Optional[float] = None) -> bool:
    """The updater is working right now (a stale busy state does not count)."""
    if not status or status.get("state") not in BUSY:
        return False
    ts = status.get("updated_at") or status.get("checked_at") or 0
    return isinstance(ts, (int, float)) and (now or time.time()) - ts < STALE_S


def request(data_dir: str, action: str, version: Optional[str] = None) -> dict:
    if action == "install":
        if not valid_version(version):
            raise ValueError("Neplatná verze.")
        body: dict = {"action": "install", "version": version}
    elif action == "check":
        body = {"action": "check"}
    else:
        raise ValueError("Neplatná akce.")
    body["ts"] = int(time.time())
    _write_atomic(data_dir, REQUEST, json.dumps(body))
    return body


def summary(data_dir: str, running: str) -> dict:
    """GET /api/update/status: updater status + running version + prefs + pending."""
    st = read_status(data_dir)
    return {"status": st, "running": running, "prefs": read_prefs(data_dir),
            "pending": pending(data_dir), "busy": busy(st)}


# ------------------------------------------------------------- audit of results
# The updater (root) cannot write the audit log; the app records what it sees in
# update-status.json. A key per result keeps one entry per event: an "available"
# version or an identical error repeats on every daily check and is logged once.
AUDIT_ACTIONS = {"available": "UPDATE_AVAILABLE", "done": "UPDATE_INSTALLED",
                 "rolled_back": "UPDATE_ROLLED_BACK", "error": "UPDATE_ERROR",
                 "up_to_date": "UPDATE_UP_TO_DATE"}
FIRST_RUN_MAX_AGE_S = 3600     # without a remembered key only fresh results are logged


def audit_key(st: Optional[dict]) -> Optional[str]:
    """Identity of a finished result, None while the updater works (or no status)."""
    if not st:
        return None
    state = st.get("state")
    if state == "available":
        return f"available|{st.get('latest')}"
    if state == "error":
        return f"error|{st.get('message')}"
    if state in ("done", "rolled_back", "up_to_date"):
        return f"{state}|{st.get('target') or st.get('latest')}|{st.get('updated_at')}"
    return None


def audit_event(st: Optional[dict], seen: Optional[str], now: Optional[float] = None
                ) -> Optional[tuple[str, str, str]]:
    """(key, action, detail) of a result not logged yet, else None.
    up_to_date only after a check from the web / Telegram (the timer checks daily)."""
    key = audit_key(st)
    if key is None or key == seen:
        return None
    assert st is not None
    if st.get("state") == "up_to_date" and st.get("trigger") == "timer":
        return None
    if seen is None:
        ts = st.get("updated_at")
        if not isinstance(ts, (int, float)) or (now or time.time()) - ts > FIRST_RUN_MAX_AGE_S:
            return None
    return key, AUDIT_ACTIONS[st["state"]], str(st.get("message") or "")[:400]
