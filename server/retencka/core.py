"""Application core – runs on the asyncio loop thread.

Receives packets from the radio thread, stores them per sensor node, keeps the live
state, controls refilling and answers data queries for the web/HA layers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Optional

from . import crypto, model, updates
from .auth import IpLimiter, Sessions
from .db import Database
from .external import Geocoder, Http, Hydrawise, HydrawiseAccount, Weather
from .model import Packet
from .settings import MAX_NODES, NODE_SCHEMA, Nodes, Settings, strip_schema_defaults

log = logging.getLogger("retencka.core")

FRESH_S = 15 * 60                 # data younger than this may drive the valve
REFILL_WAIT_S = 15 * 60           # manual refill without valid data waits this long for a measurement
AUTO_COOLDOWN_S = 30 * 60
FLOWING_WINDOW_S = 15 * 60        # flow diagram: a level change this recent counts as flowing now
OUTLOOK_DAYS = 7                  # refill prediction horizon (= forecast length)
NO_RISE_LOCK_S = 24 * 3600
AUTO_CANCEL_S = 24 * 3600         # user cancelled an automatic refill -> automation paused this long
AUTO_PAUSE_KEY = "auto_refill_pause"   # settings row: {node_id: until} (survives a restart)


PAIR_TARGET_S = 15 * 60           # user's confirmation of an ID: new PAIR_ACKs this long
# A key handed out by PAIR_ACK (after the confirmation) stays acceptable on the first
# DATA frame this long, even after the pairing window closed (first DATA lost); it is
# persisted, so a hub restart does not strand the sensor either. Then it is archived.
PENDING_KEY_S = 24 * 3600
PENDING_MAX = 4                   # pending keys kept per node (strongest RSSI preferred when full)
ARCHIVE_MAX = 8                   # archived keys kept per node (see db.py v10)
ARCHIVE_SEEN_WRITE_S = 600        # node_key_archive.seen_at is refreshed at most this often
# unpairing: the sensor answers an UNPAIR by an unpaired PAIR_REQ ~6 s later (next
# attempt in its pairing window, uzel/src/main.c). Such a request is unauthenticated:
# it counts only after an UNPAIR was sent (persisted, also across a hub restart), and
# the key is dropped only when the sensor did not come back with a paired request
# (lost UNPAIR) meanwhile. The dropped key is archived, so even a forged confirmation
# never strands the sensor (re-pair from the archive).
UNPAIR_SETTLE_S = 20              # no paired request for this long after it -> key deleted
CTR_MAX_STEP = 4096               # jump > 4x CTR_RESERVE needs a 2nd frame to rule out a 1:2^32 MAC hit
STOP_RETRY_MARGIN_S = 60          # refill row closed this long after the planned end at latest
LOW_DISK_MB = 300                 # below this, the oldest raw data are thinned out
BACKUP_KEEP = 7                   # nightly local backups kept
IMPORT_MAX_BYTES = 4 << 30        # an imported backup, uncompressed (years of data are < 1 GiB)
SERVIS_24H_S = 24 * 3600
# same rules as system/bin/retencka-control.py (the root helper validates again)
NETBIRD_KEY_RE = re.compile(r"^[A-Za-z0-9-]{20,80}\Z")
NETBIRD_URL_RE = re.compile(r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?/?\Z")

# audit actions forwarded to Telegram: action -> admins only
NOTIFY_ACTIONS = {"AUTO_REFILL": False, "MANUAL_REFILL": False, "REFILL_ERROR": False, "REFILL_FAULT": False,
                  "RAIN_WAIT": False, "NODE_BOOT": True, "NODE_PAIRED": True, "NODE_UNPAIRED": True,
                  "DISK_LOW": True, "SERVICE_ACCESS": True, "UPDATE_AVAILABLE": True, "UPDATE_INSTALLED": True,
                  "UPDATE_ROLLED_BACK": True, "UPDATE_ERROR": True, "FACTORY_RESET": True}
NOTIFY_ICONS = {"AUTO_REFILL": "💧", "MANUAL_REFILL": "💧", "REFILL_ERROR": "⚠️", "REFILL_FAULT": "⚠️",
                "RAIN_WAIT": "🌧", "NODE_BOOT": "🔄", "NODE_PAIRED": "📡", "NODE_UNPAIRED": "📡",
                "DISK_LOW": "💾", "SERVICE_ACCESS": "🛠", "UPDATE_AVAILABLE": "⬆️", "UPDATE_INSTALLED": "✅",
                "UPDATE_ROLLED_BACK": "⚠️", "UPDATE_ERROR": "⚠️", "FACTORY_RESET": "🧹"}


def time_synced() -> bool:
    """True once systemd-timesyncd has synchronised the clock (always True off-Pi)."""
    if os.path.isdir("/run/systemd/timesync"):
        return os.path.exists("/run/systemd/timesync/synchronized")
    return True


class Core:
    def __init__(self, db: Database, settings: Settings, data_dir: str):
        self.db = db
        self.settings = settings
        self.nodes = Nodes(db)
        self.data_dir = data_dir
        self.sessions = Sessions(db)
        self.ip_limiter = IpLimiter()
        self.http = Http()
        self.weather = Weather(self.http)
        self.hydrawise = Hydrawise(self.http)
        self.hydrawise_account = HydrawiseAccount(self.http)
        self.geocoder = Geocoder(self.http)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.radio = None                     # set by __main__
        self.started = time.time()
        self._disk: tuple[float, Optional[float]] = (0.0, None)

        self.last: dict[int, dict] = {}       # node_id -> last stored measurement
        self.filters: dict[int, model.LevelFilter] = {}
        self.heard: "OrderedDict[int, dict]" = OrderedDict()
        self._cache: dict[str, tuple[float, Any]] = {}

        # pairing (radio thread <-> loop thread)
        self._pair_lock = threading.Lock()
        self.pair_requests: dict[int, dict] = {}   # node_id -> {ts, rssi, count}
        self.pair_targets: dict[int, dict] = {}    # node_id -> {priv, pub, expires, name, user}
        # node_id -> [{k_enc, k_mac, ts, name, user, id}] keys sent in a PAIR_ACK, not yet proven
        self._pending_keys: dict[int, list] = {}
        # node_id -> [{k_enc, k_mac, ts, reason, name, seen_at, id}] keys given up by the hub
        self._archive: dict[int, list] = {}
        self._unpair_sent: dict[int, float] = {}   # node_id -> last UNPAIR reply sent
        self._unpair_confirm: dict[int, float] = {}  # node_id -> unpaired request after UNPAIR
        self._unpair_rot: dict[int, int] = {}      # node_id -> next UNPAIR key candidate
        self._last_ctr: dict[int, int] = {}
        self._jump_ctr: dict[int, int] = {}        # node_id -> unconfirmed large counter jump
        self._bad_cal_logged: set[int] = set()
        self._tasks: set[asyncio.Task] = set()     # fire-and-forget tasks (keep a reference)

        # refill controller state, per node
        self.refills: dict[int, dict] = {}         # node_id -> open row of the refills table
        self.refill_notes: dict[int, str] = {}
        self.refill_wait: dict[int, dict] = {}     # node_id -> manual refill waiting for fresh data
        self._auto_block_until: dict[int, float] = {}
        self._auto_pause: dict[int, float] = self._load_auto_pause()   # cancelled by the user
        self._rain_wait: dict[int, dict] = {}       # node_id -> {start_pct, since, deadline}
        # start/stop of any valve one at a time: the "already running" checks and the
        # bookkeeping must not interleave with a pending Hydrawise call (double start)
        self._refill_lock = asyncio.Lock()

        # Telegram (set by __main__): notifier(text, admin_only)
        self.notifier = None
        self.telegram = None
        self._watch: dict[int, dict] = {}          # node_id -> last notified conditions

        # clock correction after a late NTP sync (no RTC on the Pi)
        self._time_anchor: Optional[tuple[float, float, int]] = None   # (wall, monotonic, first id)

        self._migrate_v2()
        self._restore_keys_mirror()
        self._load_keys()
        self._restore()
        self._sync_keys_mirror()

    # ================================================================ helpers
    def audit(self, action: str, detail: str = "", user: str = "system", ip: str = "") -> None:
        try:
            self.db.execute("INSERT INTO audit(ts, username, ip, action, detail) VALUES(?,?,?,?,?)",
                            (int(time.time()), user[:64], ip[:64], action, detail[:500]))
        except Exception:
            log.exception("audit write failed")
        if action in NOTIFY_ACTIONS:
            self.notify(f"{NOTIFY_ICONS.get(action, '')} {detail}", NOTIFY_ACTIONS[action])

    def _on_loop_thread(self) -> bool:
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def notify(self, text: str, admin_only: bool = False) -> None:
        """Thread-safe: the notifier (Telegram) runs on the loop thread, so a call from
        a worker thread (daily maintenance) is handed over to the loop."""
        if self.notifier is None:
            return
        loop = self.loop
        if loop is not None and not loop.is_closed() and not self._on_loop_thread():
            try:
                loop.call_soon_threadsafe(self._notify_now, text, admin_only)
            except RuntimeError:                    # loop closed meanwhile (shutdown)
                log.warning("notification dropped (event loop closed): %s", text)
            return
        self._notify_now(text, admin_only)

    def _notify_now(self, text: str, admin_only: bool) -> None:
        if self.notifier is not None:
            try:
                self.notifier(text, admin_only)
            except Exception:
                log.exception("notification failed")

    def _spawn(self, coro) -> None:
        """Fire-and-forget task with a kept reference (a bare ensure_future task can
        be garbage-collected mid-flight). Without a usable loop the coroutine is dropped."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(coro)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif self.loop is not None and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        else:
            coro.close()

    def watch_tick(self) -> None:
        """Every minute: notify state changes that have no event of their own
        (low level, sensor silent / back, battery low / empty). The first pass only
        records the current state, so a restart does not repeat old warnings."""
        try:
            self.unpair_tick()
        except Exception:
            log.exception("unpair tick failed")
        try:
            self.update_tick()
        except Exception:
            log.exception("update audit tick failed")
        for n in self.nodes.all():
            nid, name = n["node_id"], n["name"]
            if n.get("_unpair") or nid not in self.last:
                continue
            st, b, lv = self.status(nid), self.battery(nid), self.level_now(nid)
            prev = self._watch.get(nid)
            thr = float(n.get("notify_low_pct") or 0)
            low = False
            if thr > 0 and lv is not None:
                low = lv.percent < (thr + 3.0 if prev and prev["low"] else thr)   # 3 % hysteresis
            cur = {"offline": st == "offline" and not b["off"], "bat_off": b["off"],
                   "bat_low": b["low"] and not b["off"], "low": low}
            self._watch[nid] = cur
            if prev is None:
                continue
            if cur["low"] and not prev["low"]:
                self.notify(f"🔻 {name}: hladina {lv.percent:.0f} % (pod {thr:.0f} %), {lv.volume_l:.0f} L")
            if cur["offline"] and not prev["offline"]:
                self.notify(f"❌ {name}: čidlo neposílá data – zkontrolujte baterii nebo dosah signálu")
            if prev["offline"] and not cur["offline"] and st == "online":
                self.notify(f"✅ {name}: čidlo znovu posílá data")
            if cur["bat_low"] and not prev["bat_low"]:
                self.notify(f"🪫 {name}: baterie čidla je téměř vybitá – připravte výměnu")
            if cur["bat_off"] and not prev["bat_off"]:
                self.notify(f"🪫 {name}: čidlo se vypnulo kvůli vybité baterii – vyměňte baterii")

    # ------------------------------------------------- root helper requests
    def control_request(self, kind: str) -> None:
        """Ask the root helper (retencka-control / retencka-update path units)."""
        name = {"update": "update-request", "reboot": "control-reboot", "poweroff": "control-poweroff",
                "servis": "control-servis"}[kind]
        with open(os.path.join(self.data_dir, name), "w") as f:
            f.write(str(int(time.time())))

    # ------------------------------------- events seen in files of the root helpers
    def _seen_path(self) -> str:
        return os.path.join(self.data_dir, "audit-seen.json")

    def seen(self, kind: str) -> Optional[str]:
        try:
            with open(self._seen_path(), encoding="utf-8") as f:
                v = json.load(f).get(kind)
        except (OSError, ValueError, AttributeError):
            return None
        return v if isinstance(v, str) else None

    def mark_seen(self, kind: str, key: str) -> None:
        try:
            with open(self._seen_path(), encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        d[kind] = key
        tmp = self._seen_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self._seen_path())
        except OSError:
            log.exception("audit-seen.json not written")

    def update_tick(self) -> None:
        """Audit results of the root updater (available / installed / rolled back / error)."""
        ev = updates.audit_event(updates.read_status(self.data_dir), self.seen("update"))
        if ev is None:
            return
        key, action, detail = ev
        self.mark_seen("update", key)
        self.audit(action, detail)

    def disk_free_mb(self) -> Optional[float]:
        """Free space on the data card, cached for a minute (polled by /api/state)."""
        now = time.monotonic()
        if now - self._disk[0] > 60 or self._disk[1] is None:
            try:
                self._disk = (now, self._free_mb())
            except OSError:
                self._disk = (now, None)
        return self._disk[1]

    def disk_low(self) -> bool:
        free = self.disk_free_mb()
        return free is not None and free < LOW_DISK_MB

    def servis_state(self) -> dict:
        """Remote service access (NetBird): wanted mode + status reported by the helper."""
        def load(name):
            try:
                with open(os.path.join(self.data_dir, name)) as f:
                    return json.load(f)
            except (OSError, ValueError):
                return {}
        want, st = load("servis.json"), load("servis-status.json")
        mode, until = want.get("mode", "off"), want.get("until")
        if mode == "24h" and (not until or until < time.time()):
            mode, until = "off", None
        return {"installed": bool(st.get("installed")), "connected": bool(st.get("connected")),
                "ip": st.get("ip"), "mode": mode, "until": until, "by": want.get("by"),
                "checked": st.get("checked"), "install": self.netbird_install_state()}

    def netbird_install(self, setup_key: str, url: str, user: str, ip: str = "") -> dict:
        """Ask the root helper to install NetBird (control-netbird-install). The setup key is
        written only into the request file (deleted by the helper at once), never logged."""
        setup_key, url = str(setup_key or "").strip(), str(url or "").strip()
        if not NETBIRD_KEY_RE.match(setup_key):
            raise ValueError("Neplatný setup key (20–80 znaků: písmena, číslice, pomlčka).")
        if url and not NETBIRD_URL_RE.match(url):
            raise ValueError("Adresa serveru musí být https://… (jen písmena, číslice, tečka, pomlčka).")
        st = self.netbird_install_state()
        if st.get("state") in ("queued", "installing") and time.time() - (st.get("updated_at") or 0) < 20 * 60:
            raise ValueError("Instalace už probíhá.")
        if self.servis_state()["installed"]:
            raise ValueError("Vzdálený servis už je nainstalovaný.")
        now = int(time.time())
        self._write_json("netbird-install.json", {"state": "queued", "message": "Čekám na spuštění instalace…",
                                                  "updated_at": now})
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".control-netbird.")
        with os.fdopen(fd, "w") as f:
            json.dump({"setup_key": setup_key, "management_url": url}, f)
        os.replace(tmp, os.path.join(self.data_dir, "control-netbird-install"))
        self.audit("SERVICE_ACCESS", "Instalace vzdáleného servisu (NetBird) spuštěna", user, ip)
        return self.netbird_install_state()

    def netbird_install_state(self) -> dict:
        try:
            with open(os.path.join(self.data_dir, "netbird-install.json"), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(st, dict):
            return {}
        if st.get("state") == "done" and not st.get("audited"):
            self.audit("SERVICE_ACCESS", "Vzdálený servis (NetBird) nainstalován", "system")
            st["audited"] = True
            try:
                self._write_json("netbird-install.json", st)
            except OSError:
                pass
        return {k: st.get(k) for k in ("state", "message", "updated_at")}

    def _write_json(self, name: str, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix="." + name + ".")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, os.path.join(self.data_dir, name))

    def set_servis(self, mode: str, user: str, ip: str = "") -> dict:
        if mode not in ("off", "24h", "on"):
            raise ValueError("mode")
        data = {"mode": mode, "until": int(time.time()) + SERVIS_24H_S if mode == "24h" else None,
                "by": user, "ts": int(time.time())}
        tmp = os.path.join(self.data_dir, "servis.json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, os.path.join(self.data_dir, "servis.json"))
        self.control_request("servis")
        txt = {"off": "vypnut", "24h": "zapnut na 24 hodin", "on": "zapnut trvale"}[mode]
        self.audit("SERVICE_ACCESS", f"Vzdálený servisní přístup {txt}", user, ip)
        return self.servis_state()

    def cached(self, key: str, ttl: float, fn):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (time.time(), val)
        return val

    def invalidate(self) -> None:
        self._cache.clear()

    def resolve_node(self, raw: Optional[str]) -> Optional[int]:
        """Node from a hex query parameter; default = primary node."""
        if raw:
            try:
                nid = int(raw, 16)
            except ValueError:
                return None
            return nid if self.nodes.get(nid) else None
        return self.nodes.primary()

    def _migrate_v2(self) -> None:
        """Move the single-node settings of the first v3 build into the nodes table.

        One-time: the legacy key is deleted afterwards (and whenever nodes already exist),
        otherwise a sensor the user removed would come back on the next start."""
        old = {r["key"]: json.loads(r["value"]) for r in self.db.query("SELECT key, value FROM settings")}
        paired = old.get("paired_node_id")
        if paired is None:
            return
        if not self.nodes.ids():
            cfg = {k: old[k] for k in NODE_SCHEMA if k in old}
            try:
                self.nodes.add(int(paired), "Retenční nádrž", cfg)
            except Exception:
                log.exception("migration of the paired node failed")
                return
        self.db.execute("DELETE FROM settings WHERE key='paired_node_id'")

    def _restore(self) -> None:
        for nid in self.nodes.ids():
            self._restore_node(nid)
            self._last_ctr[nid] = self._stored_ctr(nid)
            n = self.nodes.get(nid)
            if n.get("_unpair") and n.get("_unpair_sent"):
                self._unpair_sent[nid] = float(n["_unpair_sent"])   # UNPAIR sent before the restart
        for r in self.db.query("SELECT * FROM refills WHERE end_ts IS NULL"):
            r = dict(r)
            nid = r.get("node_id") or self.nodes.primary()
            if nid is not None and r["start_ts"] + (r["planned_s"] or 0) > time.time():
                r["node_id"] = nid
                self.refills[nid] = r
                self.refill_notes[nid] = "Dopouštění pokračuje (obnoveno po restartu)"
            else:
                self.db.execute("UPDATE refills SET end_ts=?, result=? WHERE id=?",
                                (int(time.time()), "Ukončeno časovačem (během restartu)", r["id"]))

    def _stored_ctr(self, nid: int) -> int:
        """Replay watermark after a restart / import: the persisted last accepted
        counter (survives retention and the disk guard), never lower than the newest
        stored measurement of the current key."""
        n = self.nodes.get(nid) or {}
        from_meas = self.db.scalar("SELECT MAX(seq) FROM measurements WHERE node_id=? AND ts >= ?",
                                   (nid, n.get("paired_at") or 0), -1)
        stored = self.db.scalar("SELECT last_ctr FROM nodes WHERE node_id=?", (nid,), -1)
        return max(int(from_meas), int(stored))

    def _save_ctr(self, nid: int, ctr: Optional[int]) -> None:
        try:
            self.db.execute("UPDATE nodes SET last_ctr=? WHERE node_id=?", (ctr, nid))
        except Exception:
            log.exception("could not persist the frame counter of %04X", nid)

    # =========================================================== radio input
    def on_frame(self, payload: bytes, rssi: float, lqi: int) -> None:
        """Radio thread -> event loop."""
        if self.loop:
            self.loop.call_soon_threadsafe(self._on_frame, payload, rssi, lqi)

    def responder(self, payload: bytes) -> Optional[bytes]:
        """Radio thread: answer a PAIR_REQ immediately (the node listens only ~0.6 s).

        A node that holds a key (paired) accepts nothing but an UNPAIR signed with
        that key – a foreign hub can never take it over. The hub sends UNPAIR only
        when the user asked for it here (unpair, or (re-)pair = unpair + pair), and the
        node asks only after its battery was re-inserted, so unpairing needs both the
        hub login and physical access to the sensor. The hub does not know which key
        the sensor holds (the request is unauthenticated), so it tries its current key,
        then pending and archived keys of that ID, one per request (the sensor asks
        every 5 s for 10 min).

        An unpaired node gets a PAIR_ACK only when the user confirmed it here
        (start_pairing for that node id) – never automatically, so a sensor cannot
        attach itself to a hub by accident (e.g. a neighbour's). New keys become
        valid only after the node proves them with its first authenticated DATA frame."""
        req = crypto.parse_pair_req(payload)
        if req is None:
            return None
        nid, node_pub, node_paired = req
        now = time.time()
        with self._pair_lock:
            t = self.pair_targets.get(nid)
            if t is not None and t["expires"] < now:
                t = None
            node = self.nodes.get(nid)
            unpairing = bool(node and node.get("_unpair"))
            if node_paired:
                if not unpairing and t is None:
                    return None                     # battery swap etc.: nothing to do
                cands = self._unpair_candidates(nid, node)
                if not cands:
                    return None                     # paired to a hub we know nothing of
                i = self._unpair_rot.get(nid, 0)
                self._unpair_rot[nid] = i + 1
                # the sensor still holds a key: any earlier "unpaired" request was not
                # its confirmation (lost UNPAIR, or forged)
                self._unpair_sent[nid] = now
                self._unpair_confirm.pop(nid, None)
                return crypto.build_unpair(cands[i % len(cands)], nid, node_pub)
            if unpairing:
                return None                         # being unpaired: no new key until removed
            if t is None:                           # not confirmed by the user
                return None
            if t["priv"] is None:
                t["priv"], t["pub"] = crypto.x25519_keypair()
            keys = crypto.derive_keys(crypto.x25519(t["priv"], node_pub))
            if keys is None:
                return None
            lst = self._pending_keys.setdefault(nid, [])
            if not any(e["k_mac"] == keys[1] for e in lst):     # a retry gives the same key
                lst.append({"k_enc": keys[0], "k_mac": keys[1], "ts": now, "rssi": -120.0,
                            "pub": node_pub.hex(), "name": t.get("name") or "",
                            "user": t.get("user") or "system", "id": None})
            return crypto.build_pair_ack(nid, t["pub"])

    def _unpair_candidates(self, nid: int, node: Optional[dict]) -> list[bytes]:
        """MAC keys an UNPAIR for this node may be signed with (under _pair_lock):
        current key, then keys seen in DATA frames, then the newest."""
        out = []
        keys = self.nodes.keys(nid) if node else None
        if keys:
            out.append(keys[1])
        rest = list(self._pending_keys.get(nid, [])) + list(self._archive.get(nid, []))
        rest.sort(key=lambda e: (-(e.get("seen_at") or 0), -e["ts"]))
        for e in rest:
            if e["k_mac"] not in out:
                out.append(e["k_mac"])
        return out

    # ------------------------------------------------------- key archive
    def _load_keys(self) -> None:
        """Pending + archived keys from the DB (start, after an import)."""
        self.db.execute("UPDATE node_key_archive SET reason='expired' WHERE reason='pending' AND removed_at < ?",
                        (int(time.time() - PENDING_KEY_S),))
        pend: dict[int, list] = {}
        for r in self.db.query("SELECT * FROM node_key_archive WHERE reason='pending' ORDER BY id"):
            e = self._archive_entry(r)
            if e:
                pend.setdefault(r["node_id"], []).append(e)
        with self._pair_lock:
            for nid, lst in pend.items():
                cur = self._pending_keys.setdefault(nid, [])
                for e in lst:
                    if not any(x["k_mac"] == e["k_mac"] for x in cur):
                        cur.append(e)
        self._reload_archive()

    @staticmethod
    def _archive_entry(r) -> Optional[dict]:
        try:
            k_enc, k_mac = bytes.fromhex(r["k_enc"]), bytes.fromhex(r["k_mac"])
        except (TypeError, ValueError):
            return None
        if len(k_enc) != 16 or len(k_mac) != 16:
            return None
        return {"id": r["id"], "k_enc": k_enc, "k_mac": k_mac, "ts": r["removed_at"], "reason": r["reason"],
                "name": r["name"] or "", "user": r["username"] or "system", "seen_at": r["seen_at"]}

    def _reload_archive(self) -> None:
        arch: dict[int, list] = {}
        for r in self.db.query("SELECT * FROM node_key_archive WHERE reason<>'pending' ORDER BY id"):
            e = self._archive_entry(r)
            if e:
                arch.setdefault(r["node_id"], []).append(e)
        with self._pair_lock:
            self._archive = arch

    def _archive_key(self, nid: int, k_enc: bytes, k_mac: bytes, reason: str, name: str = "",
                     user: str = "system", row_id: Optional[int] = None, ts: Optional[float] = None) -> None:
        """Keep a key the sensor may still hold (loop thread). Same key again = one row."""
        if row_id is None:
            row = self.db.one("SELECT id FROM node_key_archive WHERE node_id=? AND k_mac=?", (nid, k_mac.hex()))
            row_id = row["id"] if row else None
        if row_id is None:
            self.db.execute("INSERT INTO node_key_archive(node_id, k_enc, k_mac, removed_at, reason, name, username) "
                            "VALUES(?,?,?,?,?,?,?)", (nid, k_enc.hex(), k_mac.hex(), int(ts or time.time()),
                                                      reason, name or "", user or "system"))
        else:
            self.db.execute("UPDATE node_key_archive SET reason=?, name=COALESCE(NULLIF(?, ''), name) WHERE id=?",
                            (reason, name or "", row_id))
        ids = [r["id"] for r in self.db.query(
            "SELECT id FROM node_key_archive WHERE node_id=? AND reason<>'pending' "
            "ORDER BY seen_at IS NOT NULL DESC, IFNULL(seen_at, 0) DESC, removed_at DESC, id DESC", (nid,))]
        for old in ids[ARCHIVE_MAX:]:
            self.db.execute("DELETE FROM node_key_archive WHERE id=?", (old,))

    def _persist_pending(self, nid: int) -> None:
        """Write keys the radio thread handed out to the DB; at most PENDING_MAX stay pending
        (strongest RSSI preferred so weak spoofed requests cannot evict a nearby sensor)."""
        with self._pair_lock:
            lst = self._pending_keys.get(nid) or []
            new = [e for e in lst if e["id"] is None]
            over = []
            if len(lst) > PENDING_MAX:
                ranked = sorted(lst, key=lambda e: (e.get("rssi", -120.0), e["ts"]))
                over = ranked[:-PENDING_MAX]
                keep_ids = {id(e) for e in ranked[-PENDING_MAX:]}
                self._pending_keys[nid] = [e for e in lst if id(e) in keep_ids]
        for e in new:
            cur = self.db.execute("INSERT INTO node_key_archive(node_id, k_enc, k_mac, removed_at, reason, name, username) "
                                  "VALUES(?,?,?,?,'pending',?,?)", (nid, e["k_enc"].hex(), e["k_mac"].hex(),
                                                                    int(e["ts"]), e["name"], e["user"]))
            e["id"] = cur.lastrowid
        if over:
            self._retire(nid, over, "expired")

    def _retire(self, nid: int, entries: list, reason: str) -> None:
        for e in entries:
            self._archive_key(nid, e["k_enc"], e["k_mac"], reason, e.get("name", ""), e.get("user", "system"),
                              e.get("id"), e.get("ts"))
        if entries:
            self._reload_archive()

    def _drop_pending(self, nid: int, reason: str) -> None:
        """The user gave up this pairing: its keys are no longer accepted, only archived."""
        with self._pair_lock:
            lst = self._pending_keys.pop(nid, [])
        self._retire(nid, lst, reason)

    def _expire_pending(self) -> None:
        now = time.time()
        old: dict[int, list] = {}
        with self._pair_lock:
            for nid, lst in list(self._pending_keys.items()):
                gone = [e for e in lst if now - e["ts"] >= PENDING_KEY_S]
                if gone:
                    old[nid] = gone
                    keep = [e for e in lst if e not in gone]
                    if keep:
                        self._pending_keys[nid] = keep
                    else:
                        self._pending_keys.pop(nid, None)
        for nid, gone in old.items():
            self._retire(nid, gone, "expired")
            log.info("Pairing key of %04X was never confirmed by data – archived", nid)

    def _match_archive(self, nid: int, payload: bytes) -> bool:
        """An unaccepted DATA frame signed with an archived key: the sensor still holds
        that key, so the UI can offer re-pairing (UNPAIR with that key)."""
        with self._pair_lock:
            entries = list(self._archive.get(nid, []))
        for e in entries:
            if crypto.open_data(e["k_enc"], e["k_mac"], payload) is not None:
                now = int(time.time())
                if not e.get("seen_at") or now - e["seen_at"] >= ARCHIVE_SEEN_WRITE_S:
                    e["seen_at"] = now
                    self.db.execute("UPDATE node_key_archive SET seen_at=? WHERE id=?", (now, e["id"]))
                return True
        return False

    def archive_info(self, nid: int) -> dict:
        """For the UI: does the hub hold an old / unproven key of this sensor?"""
        with self._pair_lock:
            entries = list(self._archive.get(nid, [])) + list(self._pending_keys.get(nid, []))
        return {"archived": bool(entries), "key_match": any(e.get("seen_at") for e in entries),
                "archived_name": next((e["name"] for e in reversed(entries) if e.get("name")), "")}

    def _heard(self, nid: int, rssi: float, kind: str) -> None:
        h = self.heard.get(nid, {"count": 0})
        h.update(node_id=nid, ts=int(time.time()), rssi=rssi, count=h["count"] + 1, kind=kind)
        self.heard[nid] = h
        self.heard.move_to_end(nid)
        while len(self.heard) > 20:
            self.heard.popitem(last=False)

    def _on_frame(self, payload: bytes, rssi: float, lqi: int) -> None:
        req = crypto.parse_pair_req(payload)
        if req is not None:
            nid, node_pub, node_paired = req
            self._heard(nid, rssi, "pair")
            with self._pair_lock:
                pr = self.pair_requests.get(nid, {"count": 0, "pubs": set()})
                pubs = set(pr.get("pubs") or ())
                pubs.add(node_pub.hex())
                pr.update(ts=int(time.time()), rssi=rssi, count=pr["count"] + 1,
                          node_paired=node_paired, pubs=pubs, pub_count=len(pubs))
                self.pair_requests[nid] = pr
                for e in self._pending_keys.get(nid, ()):
                    if e.get("pub") == node_pub.hex():
                        e["rssi"] = max(e.get("rssi", -120.0), rssi)
            self._persist_pending(nid)
            node = self.nodes.get(nid)
            if node and node.get("_unpair"):
                with self._pair_lock:
                    sent = self._unpair_sent.get(nid)
                    if sent is not None and not node_paired:
                        # the sensor says it has forgotten its key. The request is not
                        # authenticated: it counts only after an UNPAIR was sent, and the
                        # key is deleted only once the sensor stays unpaired (unpair_tick).
                        self._unpair_confirm.setdefault(nid, time.time())
                if sent is not None and node_paired and not node.get("_unpair_sent"):
                    self.nodes.set_unpair_sent(nid, sent)   # survives a hub restart
            self.unpair_tick()
            return

        hdr = crypto.data_header(payload)
        if hdr is None:
            if self.radio:
                self.radio.foreign_packets += 1
            return
        nid, ctr = hdr
        self._heard(nid, rssi, "data")
        node = self.nodes.get(nid)
        if node and node.get("_unpair"):
            return                                  # being unpaired: data no longer accepted
        fields = None
        keys = self.nodes.keys(nid)
        if keys:
            fields = crypto.open_data(keys[0], keys[1], payload)
        if fields is None:
            now = time.time()
            with self._pair_lock:
                pending = [e for e in self._pending_keys.get(nid, []) if now - e["ts"] < PENDING_KEY_S]
            for e in pending:
                new_fields = crypto.open_data(e["k_enc"], e["k_mac"], payload)
                if new_fields is not None:
                    if self._commit_pairing(nid, e):
                        fields = new_fields
                    break
        if fields is None:
            if keys and self.radio:
                self.radio.auth_failures += 1
            self.heard[nid]["key_match"] = self._match_archive(nid, payload)
            return                                  # unknown node or forged / damaged frame
        last_ctr = self._last_ctr.get(nid, -1)
        if ctr == last_ctr:
            if self.radio:
                self.radio.duplicates += 1
            return                                  # second copy of an important packet
        if ctr < last_ctr:
            if self.radio:
                self.radio.replays += 1
            return                                  # replay
        if last_ctr >= 0 and ctr - last_ctr > CTR_MAX_STEP:
            cand = self._jump_ctr.get(nid)
            if cand is None or not (0 < ctr - cand <= CTR_MAX_STEP):
                if cand != ctr:
                    log.warning("Node %04X: counter jump %d -> %d held until the next frame confirms it",
                                nid, last_ctr, ctr)
                self._jump_ctr[nid] = ctr
                return                              # huge jump: wait for the next frame (rules out a 1:2^32 MAC hit)
        self._jump_ctr.pop(nid, None)
        self._last_ctr[nid] = ctr
        self._save_ctr(nid, ctr)
        flags, vbat, temp, hum, probe, fw, diag, sag = fields
        pkt = model.Packet(
            flags=flags, node_id=nid, seq=ctr,
            vbat_mv=None if (flags & model.FLAG_BAT_ERR or vbat == model.U16_INVALID) else vbat,
            temp_x100=None if (flags & model.FLAG_SHT_ERR or temp == model.TEMP_INVALID) else temp,
            hum_x100=None if (flags & model.FLAG_SHT_ERR or hum == model.U16_INVALID) else hum,
            probe_100uv=None if (flags & model.FLAG_PROBE_ERR or probe == model.U16_INVALID) else probe,
            rssi_dbm=rssi, lqi=lqi, diag=diag,
            vbat_sag_mv=None if sag == model.SAG_UNKNOWN else sag * model.SAG_UNIT_MV)
        self._store(pkt, fw)

    def _commit_pairing(self, nid: int, entry: dict) -> bool:
        """First DATA frame proved a key from a confirmed PAIR_ACK: it becomes the node's
        key. Older pending and archived keys of the ID are useless now and dropped."""
        if self.nodes.get(nid) is None and len(self.nodes.ids()) >= MAX_NODES:
            log.warning("Node %04X proved its pairing key, but the hub is full (%d sensors)", nid, MAX_NODES)
            return False                            # key stays pending; frame not accepted
        with self._pair_lock:
            t = self.pair_targets.pop(nid, {})
            self._pending_keys.pop(nid, None)
            self.pair_requests.pop(nid, None)
            self._unpair_sent.pop(nid, None)
            self._unpair_confirm.pop(nid, None)
            self._unpair_rot.pop(nid, None)
            old_name = next((e["name"] for e in reversed(self._archive.get(nid, [])) if e.get("name")), "")
        new = self.nodes.get(nid) is None
        if new:
            self.nodes.add(nid, t.get("name") or entry.get("name") or old_name)
        elif self.nodes.get(nid).get("_unpair"):
            self.nodes.set_unpair(nid, False)
        self.nodes.set_keys(nid, entry["k_enc"], entry["k_mac"])
        self.db.execute("DELETE FROM node_key_archive WHERE node_id=?", (nid,))
        self._reload_archive()
        self._last_ctr[nid] = -1                    # new key: the counter starts over
        self._jump_ctr.pop(nid, None)
        self._save_ctr(nid, None)
        if nid in self.heard:
            self.heard[nid].pop("key_match", None)
        self.audit("NODE_PAIRED", f"Čidlo {nid:04X} {'spárováno' if new else 'znovu spárováno (nový klíč)'}",
                   t.get("user") or entry.get("user") or "system")
        self._sync_keys_mirror()
        log.info("Node %04X paired", nid)
        return True

    # ------------------------------------------------------------- pairing
    def start_pairing(self, nid: int, name: str, user: str) -> None:
        """The user confirmed this ID: answer its next requests (UNPAIR with any key the
        hub holds for it if the sensor is still paired, then PAIR_ACK)."""
        node = self.nodes.get(nid)
        if node and node.get("_unpair"):
            self.nodes.set_unpair(nid, False)       # re-pair instead of removing
        with self._pair_lock:
            self.pair_targets[nid] = {"priv": None, "pub": None, "expires": time.time() + PAIR_TARGET_S,
                                      "name": name, "user": user}
            self._unpair_sent.pop(nid, None)
            self._unpair_confirm.pop(nid, None)
            self._unpair_rot.pop(nid, None)
        # keys already handed out stay pending: the sensor may hold one of them

    def cancel_pairing(self, nid: int) -> None:
        with self._pair_lock:
            self.pair_targets.pop(nid, None)
        self._drop_pending(nid, "cancelled")

    def unpair_tick(self) -> None:
        """Finish unpairings the sensor confirmed and did not take back (it did not
        send a paired request again within UNPAIR_SETTLE_S); archive stale pairing keys."""
        now = time.time()
        with self._pair_lock:
            done = [k for k, ts in self._unpair_confirm.items() if now - ts >= UNPAIR_SETTLE_S]
            expired = any(now - e["ts"] >= PENDING_KEY_S for lst in self._pending_keys.values() for e in lst)
        if expired:
            self._expire_pending()
        for nid in done:
            node = self.nodes.get(nid)
            if node is None or not node.get("_unpair"):
                with self._pair_lock:
                    self._unpair_confirm.pop(nid, None)
                continue
            self.forget_node(nid, "NODE_UNPAIRED",
                             f"Čidlo {nid:04X} ({node['name']}) odpárováno – potvrzeno čidlem")

    def _clear_unpair(self, nid: int) -> None:
        with self._pair_lock:
            self._unpair_sent.pop(nid, None)
            self._unpair_confirm.pop(nid, None)
            self._unpair_rot.pop(nid, None)

    def request_unpair(self, nid: int, user: str, ip: str = "") -> None:
        """Step 1 of unpairing: stop accepting data, wait for the sensor to confirm."""
        self.cancel_pairing(nid)
        self._clear_unpair(nid)
        self.nodes.set_unpair(nid, True)
        self.last.pop(nid, None)
        self.invalidate()
        node = self.nodes.get(nid) or {}
        self.audit("NODE_UNPAIR_REQUESTED",
                   f"Odpárování čidla {nid:04X} ({node.get('name', '')}) – čeká na vložení baterie do čidla",
                   user, ip)

    def cancel_unpair(self, nid: int, user: str, ip: str = "") -> None:
        self.nodes.set_unpair(nid, False)
        self._clear_unpair(nid)
        self._restore_node(nid)
        self.invalidate()
        self.audit("NODE_UNPAIR_CANCELLED", f"Odpárování čidla {nid:04X} zrušeno", user, ip)

    def forget_node(self, nid: int, action: str, detail: str, user: str = "system", ip: str = "") -> None:
        """Delete the node (measured data stay in the database). Its key goes to the
        archive: if the sensor still holds it, a later pairing of this ID releases it
        with a signed UNPAIR (no UPDI erase). Data signed with it are never accepted."""
        node = self.nodes.get(nid) or {}
        keys = self.nodes.keys(nid)
        reason = "unpaired" if action == "NODE_UNPAIRED" else "removed"
        with self._pair_lock:
            self.pair_targets.pop(nid, None)
        self._drop_pending(nid, reason)
        self._clear_unpair(nid)
        if keys:
            self._archive_key(nid, keys[0], keys[1], reason, node.get("name", ""), user)
            self._reload_archive()
        self.nodes.remove(nid)
        self.last.pop(nid, None)
        self.filters.pop(nid, None)
        self._last_ctr.pop(nid, None)
        self._jump_ctr.pop(nid, None)
        self.invalidate()
        self._sync_keys_mirror()
        self.audit(action, detail + "; naměřená data zůstávají", user, ip)
        log.info("Node %04X removed (%s)", nid, action)

    def _keys_mirror_path(self) -> str:
        return f"{self.db.path}.keys"

    def _sync_keys_mirror(self) -> None:
        """Keep paired sensor keys in a separate small atomic file next to the DB so
        a corrupted database without a recent backup never loses sensor pairings."""
        path = self._keys_mirror_path()
        try:
            nodes = [
                dict(r)
                for r in self.db.query(
                    "SELECT node_id, name, config, created, sort, secret, paired_at, last_ctr "
                    "FROM nodes WHERE secret IS NOT NULL ORDER BY sort, node_id"
                )
            ]
            arch = [
                dict(r)
                for r in self.db.query(
                    "SELECT node_id, k_enc, k_mac, removed_at, reason, name, username, seen_at "
                    "FROM node_key_archive WHERE reason<>'pending' ORDER BY id"
                )
            ]
            d = os.path.dirname(os.path.abspath(path))
            fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"nodes": nodes, "archive": arch}, f)
                f.flush()
                if hasattr(os, "fchmod"):
                    os.fchmod(f.fileno(), 0o600)
            os.replace(tmp, path)
        except Exception:
            log.exception("could not write keys mirror %s", path)

    def _restore_keys_mirror(self) -> None:
        """If the database lost paired sensors (e.g. fresh DB after corruption or older
        nightly backup), recover them from the atomic keys mirror."""
        path = self._keys_mirror_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
        except (OSError, ValueError):
            return
        restored = 0
        archived_nids = {
            r["node_id"]
            for r in self.db.query("SELECT DISTINCT node_id FROM node_key_archive WHERE reason IN ('removed', 'unpaired')")
        }
        for row in data.get("nodes") or ():
            if not isinstance(row, dict) or not row.get("secret"):
                continue
            nid = int(row["node_id"])
            existing = self.db.one("SELECT secret FROM nodes WHERE node_id=?", (nid,))
            if existing is None and nid not in archived_nids and len(self.nodes.ids()) < MAX_NODES:
                self.db.execute(
                    "INSERT INTO nodes(node_id, name, config, created, sort, secret, paired_at, last_ctr) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        nid,
                        row.get("name") or f"Čidlo {nid:04X}",
                        row.get("config") or "{}",
                        int(row.get("created") or time.time()),
                        int(row.get("sort") or len(self.nodes.ids())),
                        row["secret"],
                        row.get("paired_at"),
                        row.get("last_ctr"),
                    ),
                )
                self.nodes.reload()
                restored += 1
            elif existing is not None and not existing["secret"]:
                self.db.execute(
                    "UPDATE nodes SET secret=?, paired_at=?, last_ctr=COALESCE(last_ctr, ?) WHERE node_id=?",
                    (row["secret"], row.get("paired_at"), row.get("last_ctr"), nid),
                )
                self.nodes.reload()
                restored += 1
        for a in data.get("archive") or ():
            if not isinstance(a, dict) or not a.get("k_enc") or not a.get("k_mac"):
                continue
            nid = int(a["node_id"])
            if not self.db.one("SELECT 1 FROM node_key_archive WHERE node_id=? AND k_mac=?", (nid, a["k_mac"])):
                self.db.execute(
                    "INSERT INTO node_key_archive(node_id, k_enc, k_mac, removed_at, reason, name, username, seen_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        nid,
                        a["k_enc"],
                        a["k_mac"],
                        int(a.get("removed_at") or time.time()),
                        a.get("reason") or "removed",
                        a.get("name") or "",
                        a.get("username") or "system",
                        a.get("seen_at"),
                    ),
                )
        if restored:
            log.warning("Restored %d paired sensor(s) from keys mirror %s", restored, path)

    def pairing_status(self) -> dict:
        now = time.time()
        self._expire_pending()
        with self._pair_lock:
            for k in [k for k, v in self.pair_targets.items() if v["expires"] < now]:
                self.pair_targets.pop(k, None)
            archived = {k for k, v in self._archive.items() if v} | {k for k, v in self._pending_keys.items() if v}
            reqs = [{"id": f"{k:04X}", "rssi": v["rssi"], "age_s": int(now - v["ts"]), "count": v["count"],
                     "pub_count": v.get("pub_count", 1),
                     "known": self.nodes.get(k) is not None, "node_paired": v.get("node_paired", False),
                     "target": k in self.pair_targets, "answered": k in self._pending_keys,
                     "archived": k in archived}
                    for k, v in sorted(self.pair_requests.items(), key=lambda kv: -kv[1]["ts"])
                    if now - v["ts"] < 120]
            targets = [{"id": f"{k:04X}", "name": v["name"], "expires_in": int(v["expires"] - now),
                        "answered": k in self._pending_keys,
                        "rssi": (self.pair_requests.get(k) or {}).get("rssi"),
                        "pub_count": max((self.pair_requests.get(k) or {}).get("pub_count", 0),
                                         len(self._pending_keys.get(k, ())))}
                       for k, v in self.pair_targets.items()]
            pending = [{"id": f"{k:04X}", "name": (v[-1]["name"] or ""),
                        "expires_in": int(max(e["ts"] for e in v) + PENDING_KEY_S - now),
                        "rssi": max((e.get("rssi") for e in v if e.get("rssi") is not None and e.get("rssi") > -120.0),
                                    default=(self.pair_requests.get(k) or {}).get("rssi")),
                        "pub_count": max((self.pair_requests.get(k) or {}).get("pub_count", 0), len(v))}
                       for k, v in self._pending_keys.items() if v and k not in self.pair_targets]
        return {"requests": reqs, "targets": targets, "pending": pending}

    # ------------------------------------------------------------ storage
    def _store(self, pkt: model.Packet, fw: int) -> None:
        now = int(time.time())
        node = self.nodes.get(pkt.node_id)
        nid = pkt.node_id
        last = self.last.get(nid)

        level_raw = level_mm = volume_l = None
        if pkt.probe_100uv is not None and not pkt.flags & model.FLAG_PROBE_CLIP:
            try:
                lv = model.compute_level(pkt.probe_100uv, node)
                level_raw = lv.level_cm * 10.0
                filt = self.filters.setdefault(nid, model.LevelFilter())
                fmm = filt.update(level_raw, model.deadband_mm(node), follow=nid in self.refills)
                flv = model.level_from_mm(fmm, node)
                level_mm, volume_l = int(round(flv.level_cm * 10)), int(round(flv.volume_l))
                level_raw = int(round(level_raw))
                self._bad_cal_logged.discard(nid)
            except (ArithmeticError, ValueError, TypeError, KeyError, IndexError):
                # broken calibration / geometry: keep the sample (raw probe value) without a level
                level_raw = level_mm = volume_l = None
                if nid not in self._bad_cal_logged:
                    self._bad_cal_logged.add(nid)
                    log.exception("level computation failed for node %04X (calibration?)", nid)

        refilling = 1 if nid in self.refills else 0
        synced = time_synced()
        row = dict(ts=now, node_id=nid, seq=pkt.seq, flags=pkt.flags,
                   vbat_mv=pkt.vbat_mv, temp_x100=pkt.temp_x100, hum_x100=pkt.hum_x100,
                   probe_100uv=pkt.probe_100uv, level_mm=level_mm, level_raw_mm=level_raw,
                   volume_l=volume_l,
                   rssi_x2=None if pkt.rssi_dbm is None else int(round(pkt.rssi_dbm * 2)),
                   lqi=pkt.lqi, refilling=refilling, time_ok=1 if synced else 0,
                   diag=pkt.diag, vbat_sag_mv=pkt.vbat_sag_mv)
        try:
            cur = self.db.execute(
                "INSERT INTO measurements(ts,node_id,seq,flags,vbat_mv,temp_x100,hum_x100,probe_100uv,"
                "level_mm,level_raw_mm,volume_l,rssi_x2,lqi,refilling,time_ok,diag,vbat_sag_mv) VALUES "
                "(:ts,:node_id,:seq,:flags,:vbat_mv,:temp_x100,:hum_x100,:probe_100uv,"
                ":level_mm,:level_raw_mm,:volume_l,:rssi_x2,:lqi,:refilling,:time_ok,:diag,:vbat_sag_mv)", row)
            if not synced and self._time_anchor is None:
                self._time_anchor = (time.time(), time.monotonic(), cur.lastrowid)
        except Exception:
            log.exception("measurement insert failed")
        row["fw"] = fw
        if last:                           # keep previous values when a sensor reported an error
            for k in ("level_mm", "level_raw_mm", "volume_l", "probe_100uv", "temp_x100", "hum_x100", "vbat_mv",
                      "vbat_sag_mv"):
                if row[k] is None:
                    row[f"{k}_stale"] = True
                    row[k] = last.get(k)
        self.last[nid] = row
        self.invalidate()
        if pkt.flags & model.FLAG_BOOT:
            why = (" (watchdog)" if pkt.flags & model.FLAG_WDT_RESET else
                   " (pokles napětí – zkontrolujte baterii)" if (pkt.diag or 0) & model.DIAG_BOD_RESET else "")
            self.audit("NODE_BOOT", f"Čidlo {node['name']} ({nid:04X}) hlásí restart" + why)
        self._spawn(self._refill_on_measurement(nid))

    # ============================================================ live state
    def level_now(self, nid: Optional[int]) -> Optional[model.Level]:
        node = self.nodes.get(nid)
        last = self.last.get(nid)
        if not node or not last or last.get("level_mm") is None:
            return None
        return model.level_from_mm(last["level_mm"], node)

    def age_s(self, nid: Optional[int]) -> Optional[int]:
        last = self.last.get(nid)
        return None if not last else max(0, int(time.time()) - last["ts"])

    def data_age(self, nid: Optional[int]) -> float:
        """Age of the last measurement in seconds; infinity without data (an age of
        0 – data arrived this second – must not read as "no data")."""
        age = self.age_s(nid)
        return math.inf if age is None else age

    def status(self, nid: Optional[int]) -> str:
        age = self.age_s(nid)
        if age is None:
            return "offline"
        if age <= 3 * model.NODE_INTERVAL_S + 60:
            return "online"
        if age <= int(self.settings.get("stale_timeout_s")):
            return "stale"
        return "offline"

    def battery(self, nid: Optional[int]) -> dict:
        last, node = self.last.get(nid), self.nodes.get(nid)
        if not last or not node or last.get("vbat_mv") is None:
            return {"voltage": None, "percent": None, "low": False, "days_left": None, "off": False}
        v = last["vbat_mv"] / 1000.0 * float(node["battery_trim"])
        kind = node.get("battery_type") or "lifes2"
        pct = model.battery_percent(v, kind=kind)
        cpd = self.cached(f"cpd:{nid}", 3600, lambda: self._cycles_per_day(nid))
        heats = self.cached(f"heat:{nid}", 3600, lambda: self._heats_per_day(nid))
        # energy model from the global settings, battery capacity of this tank (inheritable)
        energy = {**self.settings.all(), "battery_capacity_mah": node["battery_capacity_mah"],
                  "battery_capacity_alk_mah": node["battery_capacity_alk_mah"]}
        slots = model.quick_check_slots(last.get("fw"))
        days = model.battery_days_left(pct, cpd, energy, heats, kind, slots) if cpd else None
        low = pct < model.BATTERY_LOW_PCT or bool(last["flags"] & model.FLAG_BAT_LOW)
        sag = last.get("vbat_sag_mv")
        # the node switches itself off below the cut-off (at rest or under TX load)
        # after sending a last packet
        worst_mv = last["vbat_mv"] - (sag or 0)
        off = worst_mv < model.BATTERY_CUTOFF_MV and self.status(nid) != "online"
        return {"voltage": round(v, 3), "percent": round(pct, 1), "low": low or off,
                "days_left": 0 if off else (None if days is None else round(days)), "off": off,
                "sag_mv": sag, "heats_per_day": round(heats, 2), "type": kind}

    def _cycles_per_day(self, nid: int) -> float:
        since = int(time.time()) - 7 * 86400
        row = self.db.one("SELECT COUNT(*) n, MIN(ts) first FROM measurements WHERE node_id=? AND ts >= ?",
                          (nid, since))
        if not row or not row["n"] or not row["first"]:
            return 86400 / model.NODE_INTERVAL_S
        span_days = max(1 / 24, (time.time() - row["first"]) / 86400)
        return max(1.0, row["n"] / span_days)

    def _heats_per_day(self, nid: int) -> float:
        """SHT40 heater use over the last 7 days (from the diagnostic byte)."""
        since = int(time.time()) - 7 * 86400
        row = self.db.one("SELECT SUM((diag & ?) != 0) h, MIN(ts) first FROM measurements "
                          "WHERE node_id=? AND ts >= ? AND diag IS NOT NULL", (model.DIAG_HEATED, nid, since))
        if not row or not row["first"]:
            return 0.0
        return (row["h"] or 0) / max(1.0, (time.time() - row["first"]) / 86400)

    def link_quality(self, nid: int) -> dict:
        def calc():
            rows = self.db.query("SELECT seq, flags, diag FROM measurements WHERE node_id=? AND ts >= ? "
                                 "ORDER BY id", (nid, int(time.time()) - 86400))
            lost, prev, diag_n, busy, reinit = 0, None, 0, 0, 0
            for r in rows:
                if prev is not None and not r["flags"] & model.FLAG_BOOT:
                    gap = r["seq"] - prev - 1
                    if 0 <= gap < 1000:
                        lost += gap
                prev = r["seq"]
                if r["diag"] is not None:
                    diag_n += 1
                    busy += 1 if r["diag"] & model.DIAG_LBT_MASK else 0
                    reinit += 1 if r["diag"] & model.DIAG_RADIO_REINIT else 0
            got = len(rows)
            return {"received_24h": got, "lost_24h": lost,
                    "loss_pct": round(100.0 * lost / (got + lost), 1) if got + lost else None,
                    # share of transmissions that had to wait for a busy channel
                    "busy_pct": round(100.0 * busy / diag_n, 1) if diag_n else None,
                    "radio_reinit_24h": reinit}
        return self.cached(f"link:{nid}", 120, calc)

    def node_summaries(self) -> list[dict]:
        out = []
        for n in self.nodes.all():
            nid = n["node_id"]
            lv = self.level_now(nid)
            last = self.last.get(nid) or {}
            out.append({"id": f"{nid:04X}", "name": n["name"], "status": self.status(nid),
                        "level_pct": None if lv is None else round(lv.percent, 1),
                        "volume_l": None if lv is None else round(lv.volume_l),
                        "max_l": round(model.max_volume_l(n)),
                        "rssi": None if last.get("rssi_x2") is None else last["rssi_x2"] / 2.0,
                        "age_s": self.age_s(nid), "battery": self.battery(nid),
                        "refilling": nid in self.refills,
                        "refill_configured": self._provider(n) is not None,
                        "paired": bool(n.get("_secret")), "unpairing": bool(n.get("_unpair"))})
        return out

    async def state(self, nid: Optional[int]) -> dict:
        s = self.settings.all()
        node = self.nodes.get(nid)
        if node is None:
            return {"status": "offline", "node": None, "nodes": self.node_summaries(),
                    "radio": self._radio_info(), "refill": self.refill_info(None)}
        lv = self.level_now(nid)
        last = self.last.get(nid) or {}
        now = int(time.time())
        flows = self.cached(f"flows7:{nid}", 120, lambda: self._daily_flows(nid, now - 8 * 86400, now))
        today = datetime.now().strftime("%Y-%m-%d")
        week_start = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        rain = await self.weather.rain_next_2_days(s["latitude"], s["longitude"], wait=False)
        temp, hum = last.get("temp_x100"), last.get("hum_x100")
        return {
            "status": self.status(nid),
            "timestamp": last.get("ts"),
            "last_seen_seconds_ago": self.age_s(nid),
            "time_ok": bool(last.get("time_ok", 1)),
            "water_level_cm": None if lv is None else round(lv.level_cm, 1),
            "water_level_percent": None if lv is None else round(lv.percent, 1),
            "volume_liters": None if lv is None else round(lv.volume_l),
            "max_liters": round(model.max_volume_l(node)),
            "max_depth_cm": model.tank_height_cm(node),
            "column_cm": None if lv is None else round(lv.column_cm, 1),
            "raw_level_cm": None if last.get("level_raw_mm") is None else round(last["level_raw_mm"] / 10.0, 1),
            "probe_mv": None if last.get("probe_100uv") is None else last["probe_100uv"] / 10.0,
            "deadband_cm": round(model.deadband_mm(node) / 10.0, 2),
            "level_stale": bool(last.get("level_mm_stale")),
            "temperature": None if temp is None else round(temp / 100.0, 2),
            "humidity": None if hum is None else round(hum / 100.0, 1),
            "battery": self.battery(nid),
            "node": {
                "id": f"{nid:04X}",
                "name": node["name"],
                "seq": last.get("seq"),
                "rssi": None if last.get("rssi_x2") is None else last["rssi_x2"] / 2.0,
                "lqi": last.get("lqi"),
                "fast": bool(last.get("flags", 0) & model.FLAG_FAST),
                "fw": last.get("fw"),
                "warnings": model.flag_list(last.get("flags", 0) & ~(model.FLAG_BOOT | model.FLAG_BAT_LOW)),
                **self.link_quality(nid),
            },
            "nodes": self.node_summaries(),
            "radio": self._radio_info(),
            "refill": self.refill_info(nid),
            "expected_rain_mm": rain,
            "outlook": self.outlook(node, lv, flows),
            "consumption_today": round(sum(d["use"] for d in flows if d["day"] == today)),
            "consumption_7d": round(sum(d["use"] for d in flows if d["day"] >= week_start)),
            "catchment_m2": float(node["catchment_area_m2"] or 0),                          # schéma toků
            "rain_today": round(sum(d["rain"] for d in flows if d["day"] == today)),
            "rain_7d": round(sum(d["rain"] for d in flows if d["day"] >= week_start)),      # změřený přítok
            "refill_today": round(sum(d["refill"] for d in flows if d["day"] == today)),
            "refill_7d": round(sum(d["refill"] for d in flows if d["day"] >= week_start)),
            "flowing": self.cached(f"flowing:{nid}", 60, lambda: self.flowing_now(nid, now)),
        }

    def outlook(self, node: dict, lv: Optional[model.Level], flows: list[dict]) -> dict:
        """How long the water lasts at the measured consumption and, with automatic
        refilling, when it will start and roughly how much it adds. The weather forecast
        only enters this prediction; the past inflow and use always come from the sensor."""
        out = {"days_left": None, "use_per_day_l": None, "auto_refill": False,
               "refill_in_days": None, "refill_l": None}
        if lv is None:
            return out
        past = [d for d in flows if d["day"] < datetime.now().strftime("%Y-%m-%d")][-7:]
        if len(past) >= 3:
            use = sum(d["use"] for d in past) / len(past)
            out["use_per_day_l"] = round(use)
            if use >= 5:
                out["days_left"] = round(lv.volume_l / use, 1)
        if not (node.get("auto_refill_enabled") and self._provider(node) is not None and lv.max_volume_l):
            return out
        out["auto_refill"] = True
        if out["use_per_day_l"] is None:
            return out
        thr = float(node["refill_threshold_pct"]) / 100.0 * lv.max_volume_l
        target = float(node["auto_refill_target_pct"]) / 100.0 * lv.max_volume_l
        roof = float(node.get("catchment_area_m2") or 0) * float(node.get("runoff_coef") or 0.8)
        vol, before = lv.volume_l, 0.0
        for day in range(1, OUTLOOK_DAYS + 1):
            ahead = self.weather.rain_ahead(day * 24) if roof else None
            mm = ahead[0] if ahead else 0.0
            vol = min(lv.max_volume_l, vol - out["use_per_day_l"] + (mm - before) * roof)
            before = mm
            if vol < thr:
                out["refill_in_days"] = day
                out["refill_l"] = round(max(0.0, target - thr))
                break
        return out

    def _radio_info(self) -> dict:
        return {"ok": bool(self.radio and self.radio.hardware_ok), "mode": getattr(self.radio, "mode", None)}

    # ================================================================ refill
    # Each node controls its own tank. The valve is driven through a provider
    # (currently Hydrawise); the provider's own timer is always set as a fail-safe,
    # so the valve closes even if the hub dies during refilling.
    def _zone(self, node: Optional[dict]) -> Optional[tuple]:
        """Identity of the valve a node drives (provider, account, zone) or None."""
        if not node or node.get("refill_provider") != "hydrawise":
            return None
        key = str(node.get("hydrawise_api_key") or self.settings.get("hydrawise_api_key") or "").strip()
        relay = str(node.get("hydrawise_relay_id") or "").strip()
        return ("hydrawise", key, relay) if key and relay else None

    def zone_owner(self, node: Optional[dict]) -> Optional[int]:
        """Several sensors may be configured with the same valve (same account + zone).
        Only one of them – the first in the node list – may drive it, otherwise two
        controllers would start/stop the same valve and fill twice as much."""
        zone = self._zone(node)
        if zone is None:
            return None
        for n in self.nodes.all():
            if not n.get("_unpair") and self._zone(n) == zone:
                return n["node_id"]
        return None

    def _provider(self, node: Optional[dict], for_stop: bool = False):
        """(run, stop) callables of the node's valve, or None. Only the zone owner gets
        them; stopping always works so a running refill can be ended after a change."""
        zone = self._zone(node)
        if zone is None or (not for_stop and self.zone_owner(node) != node["node_id"]):
            return None
        _, key, relay = zone
        return (lambda secs: self.hydrawise.call(key, relay, "run", secs),
                lambda: self.hydrawise.call(key, relay, "stop"))

    def _refill_idle_note(self, node: dict) -> str:
        """What happens next when nothing runs: shown under "Nedopouští"."""
        if self._provider(node) is None:
            return "Ventil není nastaven"
        if not node["auto_refill_enabled"]:
            return "Automatika vypnutá – jen ruční dopouštění"
        return (f"Automaticky pod {float(node['refill_threshold_pct']):.0f} % "
                f"(doplní na {float(node['auto_refill_target_pct']):.0f} %)")

    def refill_info(self, nid: Optional[int]) -> dict:
        node = self.nodes.get(nid)
        if node is None:
            return {"configured": False, "active": False, "status": "–"}
        r = self.refills.get(nid)
        owner = self.zone_owner(node)
        shared = None
        if owner is not None and owner != nid:
            on = self.nodes.get(owner) or {}
            shared = {"id": f"{owner:04X}", "name": on.get("name", "")}
        note = self.refill_notes.get(nid) or ""
        info = {"configured": self._provider(node) is not None, "provider": node.get("refill_provider"),
                "zone_controlled_by": shared,
                "active": r is not None, "status": note or self._refill_idle_note(node),
                # poslední pokus skončil chybou ventilu / Hydrawise (červená cesta ve schématu toků)
                "fault": any(w in note for w in ("selhalo", "nepotvrdil", "nespuštěno")),
                "max_pct": node["max_refill_pct"], "auto_target_pct": node["auto_refill_target_pct"],
                "auto": node["auto_refill_enabled"],
                "waiting_for_rain": nid in self._rain_wait,
                "waiting_for_data": nid in self.refill_wait,
                "auto_paused_until": (int(self._auto_pause[nid])
                                      if self._auto_pause.get(nid, 0) > time.time() else None),
                "flow_lpm": node.get("real_flow_rate_lpm") or node["flow_rate_lpm"]}
        if r:
            info.update(mode=r["mode"], target_pct=r["target_pct"],
                        ends_at=r["start_ts"] + (r["planned_s"] or 0),
                        remaining_s=max(0, r["start_ts"] + (r["planned_s"] or 0) - int(time.time())))
        return info

    async def refill_start(self, nid: int, target_pct: float, mode: str, user: str, ip: str = "") -> tuple[bool, str]:
        async with self._refill_lock:
            return await self._refill_start(nid, target_pct, mode, user, ip)

    async def _refill_start(self, nid: int, target_pct: float, mode: str, user: str, ip: str) -> tuple[bool, str]:
        node = self.nodes.get(nid)
        if node is None:
            return False, "Čidlo neexistuje."
        prov = self._provider(node)
        if prov is None:
            owner = self.zone_owner(node)
            if owner is not None and owner != nid:
                name = (self.nodes.get(owner) or {}).get("name", f"{owner:04X}")
                return False, f"Stejný ventil (zónu) ovládá nádrž „{name}“ – spusťte dopouštění tam."
            return False, "Dopouštění není pro tuto nádrž nastaveno."
        if nid in self.refills:
            return False, "Dopouštění už běží."
        zone = self._zone(node)
        busy = [k for k in self.refills if k != nid and self._zone(self.nodes.get(k)) == zone]
        if busy:
            return False, "Tento ventil už dopouští pro jinou nádrž."
        lv = self.level_now(nid)
        if (lv is None or self.data_age(nid) > FRESH_S
                or (self.last.get(nid) or {}).get("level_mm_stale")):
            # no fresh valid level (old data, or the probe reported an error): the
            # valve must not run blind – a manual request waits for the next valid
            # measurement and starts then (_refill_on_measurement)
            if mode != "manual":
                return False, "Chybí čerstvá platná hladina – dopouštění nelze bezpečně spustit."
            self.refill_wait[nid] = {"target_pct": float(target_pct), "user": user, "ip": ip,
                                     "until": time.time() + REFILL_WAIT_S}
            self.refill_notes[nid] = "Čeká na nové měření z čidla, pak se dopouštění spustí"
            self.audit("MANUAL_REFILL", f"Požadavek ({node['name']}): čeká na nové měření z čidla", user, ip)
            self.invalidate()
            return True, (f"Čeká se na nové měření z čidla – dopouštění se pak spustí samo "
                          f"(nejdéle {REFILL_WAIT_S // 60} min).")
        self.refill_wait.pop(nid, None)
        target_pct = min(float(target_pct), float(node["max_refill_pct"]))
        need_l = target_pct / 100.0 * lv.max_volume_l - lv.volume_l
        if need_l <= max(50.0, model.deadband_l(node)):
            return False, "Hladina je už na cíli nebo těsně pod ním."
        flow = float(node.get("real_flow_rate_lpm") or node.get("flow_rate_lpm") or 20.0)
        seconds = int(min(need_l / flow * 60 * 1.10, int(node["max_refill_minutes"]) * 60))
        ok, msg = await prov[0](seconds)
        if not ok:
            if msg != Hydrawise.UNSURE:
                # the provider answered with an error: the valve surely did not open (no
                # stop either – it would end a run the controller's own schedule started)
                self.audit("REFILL_ERROR", f"{node['name']}: {msg}", user, ip)
                return False, msg
            # no answer (timeout, dropped connection): the start may have reached the
            # provider and opened the valve – send a preventive stop; if that fails too,
            # track the refill as unconfirmed (stop_retry) so refill_tick retries the stop
            stop_ok, _ = await prov[1]()
            self.audit("REFILL_ERROR", f"{node['name']}: {msg} – start nepotvrzen, preventivní zastavení "
                                       f"{'potvrzeno' if stop_ok else 'nepotvrzeno, opakuje se'}", user, ip)
            if not stop_ok:
                now = int(time.time())
                reason = f"Nejistý start ({msg}) – preventivní zastavení"
                cur = self.db.execute(
                    "INSERT INTO refills(start_ts, mode, target_pct, start_l, planned_s, username, node_id) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (now, mode, target_pct, int(lv.volume_l), seconds, user, nid),
                )
                self.refills[nid] = {
                    "id": cur.lastrowid,
                    "start_ts": now,
                    "mode": mode,
                    "target_pct": target_pct,
                    "start_l": int(lv.volume_l),
                    "planned_s": seconds,
                    "node_id": nid,
                    "stop_retry": True,
                    "stop_reason": reason,
                    "stop_user": (user, ip),
                }
                self.refill_notes[nid] = f"{reason} – ventil nepotvrdil zastavení, opakuje se"
                self.invalidate()
            return False, msg
        now = int(time.time())
        cur = self.db.execute(
            "INSERT INTO refills(start_ts, mode, target_pct, start_l, planned_s, username, node_id) "
            "VALUES(?,?,?,?,?,?,?)", (now, mode, target_pct, int(lv.volume_l), seconds, user, nid))
        self.refills[nid] = {"id": cur.lastrowid, "start_ts": now, "mode": mode, "target_pct": target_pct,
                             "start_l": int(lv.volume_l), "planned_s": seconds, "node_id": nid}
        self.refill_notes[nid] = (f"Dopouštění aktivní ({'automatické' if mode == 'auto' else 'manuální'}, "
                                  f"cíl {target_pct:.0f} %)")
        self.audit("AUTO_REFILL" if mode == "auto" else "MANUAL_REFILL",
                   f"Spuštěno ({node['name']}): cíl {target_pct:.0f} %, {need_l:.0f} L, max {seconds // 60} min, "
                   f"průtok {flow:.1f} L/min", user, ip)
        self.invalidate()
        return True, f"Dopouštění spuštěno (cíl {target_pct:.0f} %, nejdéle {seconds // 60} min)."

    # ---------------------------------------------- user cancel / resume
    def _load_auto_pause(self) -> dict[int, float]:
        try:
            raw = json.loads(self.db.scalar("SELECT value FROM settings WHERE key=?", (AUTO_PAUSE_KEY,), "{}"))
            now = time.time()
            return {int(k): float(v) for k, v in raw.items() if float(v) > now}
        except Exception:
            return {}

    def _save_auto_pause(self) -> None:
        try:
            self.db.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (AUTO_PAUSE_KEY, json.dumps({str(k): v for k, v in self._auto_pause.items()})))
        except Exception:
            log.exception("could not persist the automatic refill pause")

    def _pause_auto(self, nid: int, user: str, ip: str, what: str) -> str:
        until = time.time() + AUTO_CANCEL_S
        self._auto_pause[nid] = until
        self._save_auto_pause()
        name = (self.nodes.get(nid) or {}).get("name", f"{nid:04X}")
        hhmm = time.strftime("%d.%m. %H:%M", time.localtime(until))
        self.audit("AUTO_REFILL", f"{name}: {what} – automatika pozastavena do {hhmm}", user, ip)
        self.refill_notes[nid] = f"Automatické dopouštění zrušeno – pozastaveno do {hhmm}"
        self.invalidate()
        return f"Automatika je pozastavena do {hhmm} (lze obnovit)."

    async def refill_cancel(self, nid: int, reason: str, user: str, ip: str = "") -> tuple[bool, str]:
        """User's Stop button / Telegram 'stop': cancels whatever is pending or running.
        Waiting manual request -> dropped; running refill -> valve stopped; automatic
        refill (running or waiting for rain) -> also pauses the automation for this
        tank for AUTO_CANCEL_S, otherwise it would start again with the next measurement."""
        r = self.refills.get(nid)
        if r is not None:
            ok, msg = await self.refill_stop(nid, reason, user, ip)
            if r["mode"] == "auto":
                msg = f"{msg} {self._pause_auto(nid, user, ip, 'automatické dopouštění zrušeno')}"
            return ok, msg
        if nid in self.refill_wait:
            return await self.refill_stop(nid, reason, user, ip)
        if self._rain_wait.pop(nid, None) is not None:
            return True, ("Čekání na déšť zrušeno. "
                          + self._pause_auto(nid, user, ip, "čekající automatické dopouštění zrušeno"))
        return await self.refill_stop(nid, reason, user, ip)    # nothing pending: make sure the valve is closed

    def refill_resume_auto(self, nid: int, user: str, ip: str = "") -> tuple[bool, str]:
        if self._auto_pause.pop(nid, None) is None:
            return False, "Automatika není pozastavená uživatelem."
        self._save_auto_pause()
        name = (self.nodes.get(nid) or {}).get("name", f"{nid:04X}")
        self.audit("AUTO_REFILL", f"{name}: automatika znovu zapnuta", user, ip)
        self.refill_notes.pop(nid, None)        # back to the default description
        self.invalidate()
        return True, "Automatické dopouštění znovu zapnuto (rozhodne příští měření)."

    async def refill_stop(self, nid: int, reason: str, user: str = "system", ip: str = "",
                          call_api: bool = True, timed_out: bool = False) -> tuple[bool, str]:
        if nid not in self.refills and self.refill_wait.pop(nid, None) is not None:
            self.refill_notes[nid] = "Čekající dopouštění zrušeno"
            self.audit("MANUAL_REFILL", f"Čekající požadavek zrušen: {reason}", user, ip)
            self.invalidate()
            return True, "Čekající dopouštění zrušeno."
        async with self._refill_lock:
            return await self._refill_stop(nid, reason, user, ip, call_api, timed_out)

    async def _refill_stop(self, nid: int, reason: str, user: str, ip: str, call_api: bool,
                           timed_out: bool = False) -> tuple[bool, str]:
        """timed_out: the refill ran for its whole planned time (only then a level that
        did not rise means a fault – an early stop proves nothing)."""
        node = self.nodes.get(nid)
        ok, msg = True, "OK"
        prov = self._provider(node, for_stop=True) if node else None
        if call_api and prov:
            ok, msg = await prov[1]()
        name = node["name"] if node else f"{nid:04X}"
        r = self.refills.get(nid)
        if r and not ok:
            # the valve did not confirm: keep the refill open, refill_tick retries the
            # stop; the row is closed after a successful stop or when the provider's
            # own timer has surely closed the valve (planned end + margin)
            first = not r.get("stop_retry")
            r["stop_retry"] = True
            r.setdefault("stop_reason", reason)
            r.setdefault("stop_user", (user, ip))
            self.refill_notes[nid] = f"{r['stop_reason']} – ventil nepotvrdil zastavení, opakuje se"
            if first:
                self.audit("REFILL_ERROR", f"{name}: ventil nepotvrdil zastavení ({msg}) – opakuje se", user, ip)
            self.invalidate()
            return False, msg
        r = self.refills.pop(nid, None)
        if r:
            if r.get("stop_retry"):
                reason = r.get("stop_reason") or reason
                user, ip = r.get("stop_user") or (user, ip)
            lv = self.level_now(nid)
            end_l = int(lv.volume_l) if lv else None
            self.db.execute("UPDATE refills SET end_ts=?, end_l=?, result=? WHERE id=?",
                            (int(time.time()), end_l, reason, r["id"]))
            self.audit("AUTO_REFILL" if r["mode"] == "auto" else "MANUAL_REFILL",
                       f"Ukončeno ({name}): {reason}", user, ip)
            if r["mode"] == "auto":
                self._auto_block_until[nid] = time.time() + AUTO_COOLDOWN_S
            last = self.last.get(nid) or {}
            fresh = lv is not None and self.data_age(nid) <= FRESH_S and not last.get("level_mm_stale")
            if timed_out and not r.get("stop_retry") and fresh and r["start_l"] is not None \
                    and end_l - r["start_l"] < 20 and time.time() - r["start_ts"] > 600:
                self._auto_block_until[nid] = time.time() + NO_RISE_LOCK_S
                self.audit("REFILL_FAULT", f"{name}: hladina během dopouštění nestoupla – automatika "
                                           f"pozastavena na 24 h")
        elif call_api:
            self.audit("MANUAL_REFILL", f"Ventil zastaven ({name}): {reason}", user, ip)
        self.refill_notes[nid] = reason if ok else f"{reason} – ventil nepotvrdil zastavení!"
        self.invalidate()
        return ok, ("Dopouštění zastaveno." if ok else msg)

    async def _refill_on_measurement(self, nid: int) -> None:
        try:
            node = self.nodes.get(nid)
            if node is None or self._provider(node) is None:
                return
            s = self.settings.all()
            lv = self.level_now(nid)
            last = self.last.get(nid) or {}
            if lv is None or last.get("level_mm_stale"):
                return
            w = self.refill_wait.pop(nid, None)
            if w is not None and nid not in self.refills and time.time() <= w["until"]:
                # a manual request was waiting for exactly this: fresh, valid data
                ok, msg = await self.refill_start(nid, w["target_pct"], "manual", w["user"], w["ip"])
                if not ok:
                    self.refill_notes[nid] = f"Dopouštění nespuštěno: {msg}"
                    self.audit("REFILL_ERROR", f"{node['name']}: čekající dopouštění nespuštěno – {msg}",
                               w["user"], w["ip"])
                return
            r = self.refills.get(nid)
            if r and r.get("stop_retry"):
                return                               # stop already requested, refill_tick retries it
            if r:
                el = time.time() - r["start_ts"]
                rise = lv.volume_l - (r["start_l"] or lv.volume_l)
                if el >= 120 and rise >= 50:
                    measured = rise / el * 60.0
                    if 2.0 <= measured <= 1000.0:
                        old = node.get("real_flow_rate_lpm") or measured
                        self.nodes.update(nid, {"real_flow_rate_lpm": round(0.7 * old + 0.3 * measured, 2)},
                                          allow_internal=True)
                if lv.percent >= r["target_pct"]:
                    await self.refill_stop(nid, f"Dosaženo cíle ({lv.percent:.1f} %)")
                return
            thr = float(node["refill_threshold_pct"])
            if not node["auto_refill_enabled"] or lv.percent >= thr:
                if self._rain_wait.pop(nid, None):
                    self.audit("RAIN_WAIT", f"{node['name']}: déšť doplnil nádrž ({lv.percent:.0f} %), "
                                            f"dopouštění nebylo potřeba")
                self.refill_notes.pop(nid, None)        # back to the default description
                return
            paused = self._auto_pause.get(nid, 0)
            if paused and time.time() >= paused:
                self._auto_pause.pop(nid, None)
                self._save_auto_pause()
            elif paused:
                return                               # cancelled by the user (note already set)
            if time.time() < self._auto_block_until.get(nid, 0):
                self.refill_notes[nid] = "Automatické dopouštění dočasně pozastaveno"
                return
            target = float(node["auto_refill_target_pct"])
            gain = 0.0
            if node.get("rain_wait_enabled", True):
                await self.weather.forecast(s["latitude"], s["longitude"])
                gain, rain_mm, rain_until = self.rain_gain_pct(node, lv)
                decision = self._rain_wait_decision(nid, node, lv, gain, rain_mm, rain_until)
                if decision is None:
                    return                           # waiting for rain (note already set)
                if decision:
                    self.audit("RAIN_WAIT", f"{node['name']}: {decision}")
                if gain > 0:
                    # rain is still expected: top up only what the rain will not bring
                    target = max(min(target, thr + 2.0), target - gain)
            self._rain_wait.pop(nid, None)
            ok, msg = await self.refill_start(nid, target, "auto", "system")
            if not ok:
                self.refill_notes[nid] = f"Automatické dopouštění selhalo: {msg}"
                self._auto_block_until[nid] = time.time() + AUTO_COOLDOWN_S
        except Exception:
            log.exception("refill logic failed")

    def rain_gain_pct(self, node: dict, lv: model.Level) -> tuple[float, float, Optional[int]]:
        """Level gain (%) expected from the forecast rain within rain_wait_hours:
        rain [mm] × roof area [m²] × runoff coefficient = litres."""
        ahead = self.weather.rain_ahead(float(node.get("rain_wait_hours") or 24))
        if not ahead or not lv.max_volume_l:
            return 0.0, 0.0, None
        mm, until = ahead
        litres = mm * float(node["catchment_area_m2"]) * float(node.get("runoff_coef") or 0.8)
        return litres / lv.max_volume_l * 100.0, mm, until

    def _rain_wait_decision(self, nid: int, node: dict, lv: model.Level, gain: float, mm: float,
                            until: Optional[int]) -> Optional[str]:
        """None = keep waiting for rain; otherwise refill now ("" = no wait happened,
        text = why the wait was given up)."""
        thr = float(node["refill_threshold_pct"])
        now = time.time()
        wait = self._rain_wait.get(nid)
        floor = thr - float(node.get("rain_wait_max_below_pct", 10.0))
        if lv.percent < floor:
            return (f"hladina {lv.percent:.0f} % je víc než {thr - floor:.0f} % pod prahem – nečeká se na déšť"
                    if wait or gain > 0 else "")
        if wait:
            if lv.percent < wait["start_pct"] - float(node.get("rain_wait_max_drop_pct", 5.0)):
                return f"během čekání na déšť klesla hladina z {wait['start_pct']:.0f} % na {lv.percent:.0f} %"
            if now > wait["deadline"]:
                return "očekávaný déšť nepřišel"
        if gain <= 0 or lv.percent + gain < thr:
            return "" if not wait else "předpověď deště už nestačí"
        if not wait:
            hours = float(node.get("rain_wait_hours") or 24)
            wait = {"start_pct": lv.percent, "since": now,
                    "deadline": min(now + hours * 3600, (until or now) + 2 * 3600)}
            self._rain_wait[nid] = wait
            self.audit("RAIN_WAIT", f"{node['name']}: hladina {lv.percent:.0f} % pod prahem {thr:.0f} %, "
                                    f"předpověď {mm:.1f} mm ≈ +{gain:.0f} % – čeká se na déšť")
        self.refill_notes[nid] = (f"Čeká na déšť – předpověď {mm:.1f} mm ≈ +{gain:.0f} % "
                                  f"(nejdéle do {datetime.fromtimestamp(wait['deadline']):%H:%M})")
        return None

    async def refill_tick(self) -> None:
        """Every 30 s: timer expiry and stale-data safety stop, for every running refill."""
        for nid, w in list(self.refill_wait.items()):
            if time.time() > w["until"]:
                self.refill_wait.pop(nid, None)
                name = (self.nodes.get(nid) or {}).get("name", f"{nid:04X}")
                self.refill_notes[nid] = "Dopouštění nespuštěno – čidlo neposlalo platnou hladinu"
                self.audit("REFILL_ERROR", f"{name}: dopouštění nespuštěno – čidlo do {REFILL_WAIT_S // 60} min "
                           f"neposlalo platnou hladinu", w["user"], w["ip"])
                self.invalidate()
        for nid, r in list(self.refills.items()):
            try:
                if time.time() > r["start_ts"] + (r["planned_s"] or 0) + STOP_RETRY_MARGIN_S:
                    if r.get("stop_retry"):
                        # the provider's own timer has closed the valve by now
                        r["stop_reason"] = (f"{r.get('stop_reason') or 'Zastaveno'} – zastavení nepotvrzeno, "
                                            f"ventil zavřel jeho časovač")
                        await self.refill_stop(nid, r["stop_reason"], call_api=False)
                    else:
                        await self.refill_stop(nid, "Časovač vypršel (cíl nepotvrzen měřením)", call_api=False,
                                               timed_out=True)
                elif r.get("stop_retry"):
                    await self.refill_stop(nid, r.get("stop_reason") or "Opakované zastavení ventilu")
                elif self.data_age(nid) > FRESH_S:
                    await self.refill_stop(nid, "Bezpečnostní zastavení – výpadek dat z čidla")
            except Exception:
                log.exception("refill tick failed for node %04X", nid)

    # ======================================================== clock repair
    def time_tick(self) -> None:
        """Measurements stored before NTP synchronised carry the restored (stale) clock.
        Once synced, shift them by the measured jump (monotonic clock is the reference)."""
        a = self._time_anchor
        if a is None or not time_synced():
            return
        delta = int(round((time.time() - a[0]) - (time.monotonic() - a[1])))
        self._time_anchor = None
        if abs(delta) < 5:
            self.db.execute("UPDATE measurements SET time_ok=1 WHERE time_ok=0 AND id >= ?", (a[2],))
            return
        n = self.db.execute("UPDATE measurements SET ts = ts + ?, time_ok = 1 WHERE time_ok = 0 AND id >= ?",
                            (delta, a[2])).rowcount
        for row in self.last.values():
            if row.get("time_ok") == 0:
                row["ts"] += delta
                row["time_ok"] = 1
        self.aggregate_hours(since=int(time.time()) - abs(delta) - 7200)
        self.invalidate()
        self.audit("TIME_FIXED", f"Čas synchronizován – {n} měření posunuto o {delta} s")

    # =========================================================== data queries
    def history(self, nid: int, t_from: int, t_to: int) -> dict:
        node = self.nodes.get(nid)
        span = max(1, t_to - t_from)
        bucket = max(model.NODE_INTERVAL_S, int(math.ceil(span / 600 / 300.0)) * 300)
        if bucket >= 3600 and span > 45 * 86400:
            bucket = int(math.ceil(bucket / 3600.0)) * 3600
            rows = self.db.query(
                "SELECT (hour/?)*? AS t, AVG(level_mm) l, AVG(volume_l) v, AVG(temp) te, AVG(hum) hu, "
                "AVG(vbat) vb, MAX(refilling) rf, AVG(rssi) rs, NULL pr, NULL sg FROM hourly WHERE node_id=? AND hour >= ? AND hour < ? "
                "GROUP BY t ORDER BY t", (bucket, bucket, nid, t_from, t_to))
            scale_t = scale_h = scale_r = 1.0
        else:
            rows = self.db.query(
                "SELECT (ts/?)*? AS t, AVG(level_mm) l, AVG(volume_l) v, AVG(temp_x100) te, "
                "AVG(hum_x100) hu, AVG(vbat_mv) vb, MAX(refilling) rf, AVG(rssi_x2) rs, AVG(probe_100uv) pr, "
                "AVG(vbat_sag_mv) sg FROM measurements "
                "WHERE node_id=? AND ts >= ? AND ts < ? GROUP BY t ORDER BY t",
                (bucket, bucket, nid, t_from, t_to))
            scale_t = scale_h = 0.01
            scale_r = 0.5
        trim = float(node["battery_trim"])
        kind = node.get("battery_type") or "lifes2"
        out = {"bucket_s": bucket, "t": [], "level_pct": [], "level_cm": [], "volume_l": [], "probe_mv": [],
               "temp": [], "hum": [], "vbat": [], "bat_pct": [], "sag_mv": [], "rssi": [], "refill": []}

        def r1(x, k=1.0, nd=2):
            return None if x is None else round(x * k, nd)

        prev_t = None
        for r in rows:
            if prev_t is not None and r["t"] - prev_t > 3 * bucket + 600:
                for k in out:                      # gap -> null point breaks the line
                    if k == "t":
                        out[k].append((prev_t + bucket) * 1000)
                    elif k != "bucket_s":
                        out[k].append(None)
            prev_t = r["t"]
            out["t"].append(r["t"] * 1000)
            out["level_pct"].append(round(model.level_from_mm(r["l"], node).percent, 1) if r["l"] is not None else None)
            out["level_cm"].append(r1(r["l"], 0.1, 1))
            out["volume_l"].append(r1(r["v"], 1.0, 0))
            out["temp"].append(r1(r["te"], scale_t, 2))
            out["hum"].append(r1(r["hu"], scale_h, 1))
            out["probe_mv"].append(r1(r["pr"], 0.1, 1))
            out["vbat"].append(r1(r["vb"], 0.001 * trim, 3))
            out["bat_pct"].append(None if r["vb"] is None
                                  else round(model.battery_percent(r["vb"] * 0.001 * trim, kind=kind), 1))
            out["sag_mv"].append(r1(r["sg"], 1.0, 0))
            out["rssi"].append(r1(r["rs"], scale_r, 1))
            out["refill"].append(int(r["rf"] or 0))
        out["bands"] = self.bands(nid, t_from, t_to)
        out["boots"] = self.boots(nid, t_from, t_to)
        return out

    def bands(self, nid: int, t_from: int, t_to: int) -> list[dict]:
        """Intervals shaded in the level chart: refills (exact, from the refill log) and
        inflow from rain (detected from the level rising outside refilling)."""
        node = self.nodes.get(nid)
        out = []
        for r in self.db.query("SELECT start_ts, end_ts, start_l, end_l, planned_s FROM refills WHERE node_id=? "
                               "AND start_ts < ? AND IFNULL(end_ts, start_ts + IFNULL(planned_s, 0)) >= ? "
                               "ORDER BY start_ts", (nid, t_to, t_from)):
            end = r["end_ts"] or min(int(time.time()), r["start_ts"] + (r["planned_s"] or 0))
            litres = (r["end_l"] - r["start_l"]) if r["end_l"] is not None and r["start_l"] is not None else None
            out.append({"kind": "refill", "start": r["start_ts"] * 1000, "end": end * 1000, "litres": litres})
        deadband = max(5.0, 0.5 * model.deadband_l(node))
        if t_to - t_from > 62 * 86400:
            rows = self.db.query("SELECT hour AS ts, volume_l AS v, refilling AS r FROM hourly "
                                 "WHERE node_id=? AND hour >= ? AND hour < ? ORDER BY hour", (nid, t_from, t_to))
        else:
            rows = self.db.query("SELECT ts, volume_l AS v, refilling AS r FROM measurements WHERE node_id=? "
                                 "AND ts >= ? AND ts < ? AND volume_l IS NOT NULL ORDER BY ts", (nid, t_from, t_to))
        for a, b, litres in model.inflow_intervals([(r["ts"], r["v"], r["r"]) for r in rows], deadband):
            out.append({"kind": "rain", "start": a * 1000, "end": b * 1000, "litres": litres})
        out.sort(key=lambda x: x["start"])
        return out[-200:]

    def boots(self, nid: int, t_from: int, t_to: int) -> list[dict]:
        """Sensor power-ups (battery change) and watchdog restarts – battery chart."""
        return [{"t": r["ts"] * 1000, "wdt": bool(r["flags"] & model.FLAG_WDT_RESET)}
                for r in self.db.query("SELECT ts, flags FROM measurements WHERE node_id=? AND ts >= ? AND ts < ? "
                                       "AND (flags & ?) != 0 ORDER BY ts LIMIT 100",
                                       (nid, t_from, t_to, model.FLAG_BOOT))]

    def _daily_flows(self, nid: int, t_from: int, t_to: int) -> list[dict]:
        node = self.nodes.get(nid)
        if node is None:
            return []
        # the series is already filtered -> half of the probe dead band is enough
        deadband = max(5.0, 0.5 * model.deadband_l(node))
        max_vol = model.max_volume_l(node)
        if t_to - t_from > 62 * 86400:
            rows = self.db.query("SELECT hour AS ts, volume_l AS v, refilling AS r FROM hourly "
                                 "WHERE node_id=? AND hour >= ? AND hour < ? ORDER BY hour", (nid, t_from, t_to))
        else:
            rows = self.db.query("SELECT ts, volume_l AS v, refilling AS r FROM measurements "
                                 "WHERE node_id=? AND ts >= ? AND ts < ? AND volume_l IS NOT NULL ORDER BY ts",
                                 (nid, t_from, t_to))
        days: dict[str, dict] = {}
        d = datetime.fromtimestamp(t_from).date()
        end = datetime.fromtimestamp(max(t_from, t_to - 1)).date()
        while d <= end and len(days) < 800:
            key = d.isoformat()
            days[key] = {"day": key, "use": 0.0, "rain": 0.0, "refill": 0.0}
            d += timedelta(days=1)
        for ts, kind, liters in model.flows([(r["ts"], r["v"], r["r"]) for r in rows], deadband):
            if liters > 0.5 * max_vol:
                continue                           # implausible jump (recalibration, glitch)
            key = datetime.fromtimestamp(ts).date().isoformat()
            if key in days:
                days[key][kind] += liters
        return [{k: (round(v) if isinstance(v, float) else v) for k, v in x.items()} for x in days.values()]

    def flowing_now(self, nid: int, now: int) -> dict:
        """Does water flow in (rain) or out (consumption) right now? True when the level
        moved by more than the dead band within the last FLOWING_WINDOW_S – the flow
        diagram animates only then, not for any flow earlier today."""
        node = self.nodes.get(nid)
        out = {"rain": False, "use": False}
        if node is None:
            return out
        deadband = max(5.0, 0.5 * model.deadband_l(node))
        rows = self.db.query("SELECT ts, volume_l AS v, refilling AS r FROM measurements "
                             "WHERE node_id=? AND ts >= ? AND volume_l IS NOT NULL ORDER BY ts",
                             (nid, now - 3600))
        for ts, kind, _ in model.flows([(r["ts"], r["v"], r["r"]) for r in rows], deadband):
            if ts >= now - FLOWING_WINDOW_S and kind in out:
                out[kind] = True
        return out

    def daily_flows(self, nid: int, t_from: int, t_to: int) -> list[dict]:
        return self._daily_flows(nid, t_from, t_to)

    # ============================================================ maintenance
    def aggregate_hours(self, since: Optional[int] = None, until: Optional[int] = None) -> None:
        since = since if since is not None else int(time.time()) - 3 * 3600
        since -= since % 3600
        until = until if until is not None else 2 ** 62
        self.db.execute(
            "INSERT OR REPLACE INTO hourly(node_id, hour, n, level_mm, level_min, level_max, volume_l, temp, "
            "hum, vbat, refilling, rssi) "
            "SELECT node_id, (ts/3600)*3600 h, COUNT(*), AVG(level_mm), MIN(level_mm), MAX(level_mm), "
            "AVG(volume_l), AVG(temp_x100)/100.0, AVG(hum_x100)/100.0, AVG(vbat_mv), MAX(refilling), "
            "AVG(rssi_x2)/2.0 "
            "FROM measurements WHERE ts >= ? AND ts < ? GROUP BY node_id, h", (since, until))

    # ------------------------------------------------------- deleting data
    def _delete_where(self, nid: Optional[int], t_from: Optional[int], t_to: Optional[int]) -> dict:
        """WHERE clauses of the per-node time series for a node (None = all) and a range
        [t_from, t_to) (None = unbounded). A running refill is never deleted."""
        def clause(col: str, floor_hour: bool = False) -> tuple[str, list]:
            parts, params = [], []
            if nid is not None:
                parts.append("node_id = ?")
                params.append(nid)
            if t_from is not None:
                parts.append(f"{col} >= ?")
                params.append(t_from - t_from % 3600 if floor_hour else t_from)
            if t_to is not None:
                parts.append(f"{col} < ?")
                params.append(t_to)
            return " AND ".join(parts) or "1=1", params
        m, h, r = clause("ts"), clause("hour", True), clause("start_ts")
        return {"measurements": m, "hourly": h, "refills": (r[0] + " AND end_ts IS NOT NULL", r[1])}

    def count_data(self, nid: Optional[int], t_from: Optional[int], t_to: Optional[int]) -> dict:
        return {t: int(self.db.scalar(f"SELECT COUNT(*) FROM {t} WHERE {w}", p, 0))
                for t, (w, p) in self._delete_where(nid, t_from, t_to).items()}

    def delete_data(self, nid: Optional[int], t_from: Optional[int], t_to: Optional[int]) -> dict:
        """Delete measured data (raw measurements, hourly aggregates, finished refills) of
        one node or all nodes in a range. The audit log is never touched. Runs in a worker
        thread; call after_delete() on the loop afterwards."""
        counts = self.count_data(nid, t_from, t_to)
        with self.db.transaction() as c:
            for t, (w, p) in self._delete_where(nid, t_from, t_to).items():
                c.execute(f"DELETE FROM {t} WHERE {w}", p)
        if t_from is not None or t_to is not None:
            # the partly kept hours at the edges of the range: from the remaining raw data
            for edge in (t_from, t_to):
                if edge is not None:
                    self.aggregate_hours(since=edge - 3600, until=edge + 3600)
        return counts

    def after_delete(self, nid: Optional[int]) -> None:
        for i in ([nid] if nid is not None else list(self.nodes.ids())):
            self.after_recompute(i)

    def maintenance(self) -> None:
        """Daily: retention, low-disk guard, session purge, local backup."""
        now = int(time.time())
        keep_days = int(self.settings.get("retention_days"))
        if keep_days > 0:
            self.db.execute("DELETE FROM measurements WHERE ts < ?", (now - keep_days * 86400,))
        self._disk_guard()
        self.db.execute("DELETE FROM audit WHERE ts < ?", (now - 5 * 365 * 86400,))
        self.sessions.purge()
        self.db.execute("PRAGMA optimize")
        try:
            self.backup_local()
        except Exception:
            log.exception("local backup failed")

    def _free_mb(self) -> float:
        return shutil.disk_usage(self.data_dir).free / 1048576

    def _disk_guard(self) -> None:
        """Only if the card is nearly full: drop the oldest raw 5-min data (hourly
        aggregates stay, so charts of old periods keep working)."""
        if self._free_mb() >= LOW_DISK_MB:
            return
        oldest = self.db.scalar("SELECT MIN(ts) FROM measurements", (), None)
        half_year_ago = int(time.time()) - 180 * 86400
        if oldest is None or oldest > half_year_ago:
            return
        # the oldest month, but never anything from the last half year
        cutoff = min(oldest + 30 * 86400, half_year_ago)
        n = self.db.execute("DELETE FROM measurements WHERE ts < ?", (cutoff,)).rowcount
        self.audit("DISK_LOW", f"Málo místa na kartě – smazáno {n} nejstarších podrobných měření "
                               f"(hodinové průměry zůstávají)")

    # ================================================================ backup
    def backup_dir(self) -> str:
        d = os.path.join(self.data_dir, "backup")
        os.makedirs(d, exist_ok=True)
        return d

    def snapshot(self, dest: str) -> None:
        """Consistent copy of the live database (SQLite online backup, WAL-safe)."""
        import sqlite3
        if os.path.exists(dest):
            os.remove(dest)
        dst = sqlite3.connect(dest)
        try:
            self.db.conn().backup(dst, pages=256)
        finally:
            dst.close()

    def backup_local(self) -> str:
        """Nightly gzip copy on the card (protects against a corrupted DB file or a
        bad update); the last BACKUP_KEEP days are kept. Against SD card failure
        use the download in the web UI."""
        import gzip
        import shutil
        d = self.backup_dir()
        tmp = os.path.join(d, "snapshot.tmp")
        self.snapshot(tmp)
        out = os.path.join(d, f"retencka-{datetime.now():%Y-%m-%d}.db.gz")
        with open(tmp, "rb") as fi, gzip.open(out + ".part", "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 65536)
        os.replace(out + ".part", out)
        os.remove(tmp)
        files = sorted(f for f in os.listdir(d) if f.startswith("retencka-") and f.endswith(".db.gz"))
        for f in files[:-BACKUP_KEEP]:
            os.remove(os.path.join(d, f))
        return out

    def import_backup(self, path: str, full: bool = False) -> dict:
        """Merge a backup (.db or .db.gz from the download) into the live database.

        Data are ADDED, never overwritten: a measurement that already exists (same
        sensor + frame counter, ±1 day to survive clock corrections) is skipped, so importing the same file twice changes nothing.
        Sensors missing here are added together with their keys (paired sensors keep
        working without re-pairing). full=True also restores settings and adds
        missing user accounts. Runs in a worker thread, in batches, so the live
        receiver is never blocked for long."""
        import gzip
        import shutil
        import sqlite3
        import zlib
        work = os.path.join(self.backup_dir(), "import.tmp")
        with open(path, "rb") as f:
            gz = f.read(2) == b"\x1f\x8b"
        try:
            if gz:
                # bounded: a tiny "gzip bomb" must not fill the SD card
                limit = IMPORT_MAX_BYTES
                try:
                    limit = min(limit, shutil.disk_usage(self.backup_dir()).free - LOW_DISK_MB * 1048576)
                except OSError:
                    pass
                try:
                    with gzip.open(path, "rb") as fi, open(work, "wb") as fo:
                        size = 0
                        while chunk := fi.read(65536):
                            size += len(chunk)
                            if size > limit:
                                raise ValueError("Záloha je po rozbalení příliš velká (nebo na kartě není místo).")
                            fo.write(chunk)
                except (OSError, EOFError, zlib.error):
                    raise ValueError("Soubor není platná záloha (poškozený gzip).")
            else:
                shutil.copyfile(path, work)
            chk = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
            try:
                ok = chk.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                tables = {r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                ver = chk.execute("PRAGMA user_version").fetchone()[0]
            except sqlite3.DatabaseError:
                raise ValueError("Soubor není záloha Retenční nádrže.")
            finally:
                chk.close()
            if not ok or not {"measurements", "settings", "users"} <= tables:
                raise ValueError("Soubor není záloha Retenční nádrže (nebo je poškozený).")
            if ver > self.db.scalar("PRAGMA user_version", (), 0):
                raise ValueError("Záloha je z novější verze – nejdřív aktualizujte hub.")
            return self._merge(work, tables, full, ver)
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.remove(work + suffix)
                except OSError:
                    pass

    def _merge(self, work: str, tables: set, full: bool, src_ver: int = 999) -> dict:
        c = self.db.conn()
        c.execute("ATTACH DATABASE ? AS src", (work,))
        stats = {"measurements": 0, "skipped": 0, "nodes": 0, "refills": 0, "audit": 0, "users": 0,
                 "settings": False}
        try:
            def cols(table: str) -> list[str]:
                main = [r[1] for r in c.execute(f"PRAGMA main.table_info({table})")]
                src = {r[1] for r in c.execute(f"PRAGMA src.table_info({table})")}
                return [x for x in main if x in src and x != "id"]

            # sensors (+ keys) first
            if "nodes" in tables:
                nc = cols("nodes")
                for r in c.execute(f"SELECT {', '.join(nc)} FROM src.nodes").fetchall():
                    row = dict(zip(nc, r))
                    row.pop("unpair_at", None)
                    row.pop("unpair_sent_at", None)
                    if src_ver < 9 and row.get("config"):
                        # backup from before shared tank defaults: keep only real overrides
                        try:
                            row["config"] = json.dumps(strip_schema_defaults(json.loads(row["config"])))
                        except (ValueError, TypeError, AttributeError):
                            pass
                    if self.nodes.get(row["node_id"]) is None:
                        if len(self.nodes.ids()) >= 16:
                            continue
                        keys = [k for k in row if k != "sort"]
                        c.execute(f"INSERT INTO nodes({', '.join(keys)}, sort) VALUES({', '.join('?' * len(keys))}, ?)",
                                  [row[k] for k in keys] + [len(self.nodes.ids())])
                        stats["nodes"] += 1
                        self.nodes.reload()
                    elif row.get("secret") and not self.nodes.get(row["node_id"]).get("_secret"):
                        c.execute("UPDATE nodes SET secret=?, paired_at=? WHERE node_id=?",
                                  (row["secret"], row.get("paired_at"), row["node_id"]))
                        self.nodes.reload()

            # archived sensor keys (v10+): a removed sensor can still be released from
            # the restored hub. Pending keys of the old hub are never accepted here.
            if "node_key_archive" in tables:
                ac = cols("node_key_archive")
                current = {n["node_id"]: (n.get("_secret") or "")[32:64] for n in self.nodes.all()}
                for r in c.execute(f"SELECT {', '.join(ac)} FROM src.node_key_archive").fetchall():
                    row = dict(zip(ac, r))
                    if row.get("reason") == "pending":
                        row["reason"] = "expired"
                    if not row.get("k_mac") or current.get(row["node_id"]) == row["k_mac"] or c.execute(
                            "SELECT 1 FROM main.node_key_archive WHERE node_id=? AND k_mac=?",
                            (row["node_id"], row["k_mac"])).fetchone():
                        continue
                    c.execute(f"INSERT INTO main.node_key_archive({', '.join(row)}) VALUES({', '.join('?' * len(row))})",
                              list(row.values()))
                    stats["archived_keys"] = stats.get("archived_keys", 0) + 1
                self._reload_archive()

            # measurements, in 30-day batches (short write locks)
            mc = cols("measurements")
            lo, hi = c.execute("SELECT MIN(ts), MAX(ts) FROM src.measurements").fetchone()
            total = c.execute("SELECT COUNT(*) FROM src.measurements").fetchone()[0]
            if lo is not None:
                start = lo
                while start <= hi:
                    end = start + 30 * 86400
                    with self.db.transaction():
                        cur = c.execute(
                            f"INSERT INTO main.measurements({', '.join(mc)}) SELECT {', '.join('s.' + x for x in mc)} "
                            "FROM src.measurements s WHERE s.ts >= ? AND s.ts < ? AND NOT EXISTS ("
                            "SELECT 1 FROM main.measurements m WHERE m.node_id = s.node_id AND m.seq = s.seq "
                            "AND m.ts BETWEEN s.ts - 86400 AND s.ts + 86400)",
                            (start, end))
                        stats["measurements"] += max(0, cur.rowcount)
                    start = end
            stats["skipped"] = total - stats["measurements"]

            with self.db.transaction():
                if "hourly" in tables:
                    hc = cols("hourly")
                    c.execute(f"INSERT OR IGNORE INTO main.hourly({', '.join(hc)}) "
                              f"SELECT {', '.join(hc)} FROM src.hourly")
                if "refills" in tables:
                    rc = cols("refills")
                    stats["refills"] = c.execute(
                        f"INSERT INTO main.refills({', '.join(rc)}) SELECT {', '.join('s.' + x for x in rc)} "
                        "FROM src.refills s WHERE s.end_ts IS NOT NULL AND NOT EXISTS (SELECT 1 FROM main.refills m "
                        "WHERE m.start_ts = s.start_ts AND IFNULL(m.node_id, -1) = IFNULL(s.node_id, -1))").rowcount
                if "audit" in tables:
                    stats["audit"] = c.execute(
                        "INSERT INTO main.audit(ts, username, ip, action, detail) "
                        "SELECT s.ts, s.username, s.ip, s.action, s.detail FROM src.audit s WHERE NOT EXISTS ("
                        "SELECT 1 FROM main.audit m WHERE m.ts = s.ts AND m.action = s.action "
                        "AND IFNULL(m.detail, '') = IFNULL(s.detail, ''))").rowcount
                if full:
                    stats["users"] = c.execute(
                        "INSERT INTO main.users(username, pw_hash, role, created, pw_changed) "
                        "SELECT s.username, s.pw_hash, s.role, s.created, s.pw_changed FROM src.users s "
                        "WHERE NOT EXISTS (SELECT 1 FROM main.users m WHERE m.username = s.username)").rowcount
                    c.execute("INSERT OR REPLACE INTO main.settings(key, value) SELECT key, value FROM src.settings")
                    stats["settings"] = True
        finally:
            c.execute("DETACH DATABASE src")
        self.settings.reload()
        self.nodes.reload()
        if full and src_ver < 11:
            self._map_old_battery_capacity()
        self.aggregate_hours(since=0)
        for nid in self.nodes.ids():
            self._restore_node(nid)
            self._last_ctr[nid] = max(self._last_ctr.get(nid, -1), self._stored_ctr(nid))
        self._sync_keys_mirror()
        self.invalidate()
        return stats

    def _map_old_battery_capacity(self) -> None:
        """Backup from before v11: the global battery capacity (now copied into the
        settings rows) becomes the shared tank default, like DB migration v11."""
        changes = {}
        for key in ("battery_capacity_mah", "battery_capacity_alk_mah"):
            v = self.settings.get(key)
            if v is not None and v != NODE_SCHEMA[key][0]:
                changes[key] = v
        if changes:
            try:
                self.nodes.set_defaults(changes)
            except Exception:
                log.exception("mapping the battery capacity of an old backup failed")

    def backups(self) -> list[dict]:
        d = self.backup_dir()
        out = []
        for f in sorted(os.listdir(d), reverse=True):
            if f.endswith(".db.gz") and f.startswith("retencka-"):
                st = os.stat(os.path.join(d, f))
                out.append({"name": f, "size_mb": round(st.st_size / 1048576, 2), "ts": int(st.st_mtime)})
        return out

    def recompute_levels(self, nid: int) -> int:
        """Re-apply calibration + filter to the whole history of one node (worker thread)."""
        node = self.nodes.get(nid)
        if node is None:
            return 0
        db_mm = model.deadband_mm(node)
        filt = model.LevelFilter()
        n, last_id = 0, 0
        while True:
            rows = self.db.query("SELECT id, probe_100uv, flags, refilling FROM measurements WHERE node_id=? "
                                 "AND id > ? ORDER BY id LIMIT 2000", (nid, last_id))
            if not rows:
                break
            upd = []
            for r in rows:
                last_id = r["id"]
                if r["probe_100uv"] is None or r["flags"] & model.FLAG_PROBE_CLIP:
                    continue
                raw = model.compute_level(r["probe_100uv"], node).level_cm * 10.0
                flv = model.level_from_mm(filt.update(raw, db_mm, follow=bool(r["refilling"])), node)
                upd.append((int(round(flv.level_cm * 10)), int(round(raw)), int(round(flv.volume_l)), r["id"]))
            with self.db.transaction() as c:
                c.executemany("UPDATE measurements SET level_mm=?, level_raw_mm=?, volume_l=? WHERE id=?", upd)
            n += len(upd)
        self.aggregate_hours(since=0)
        return n

    def after_recompute(self, nid: int) -> None:
        """Reload the live state of a node after its history was recomputed (loop thread)."""
        self.filters.pop(nid, None)
        self.last.pop(nid, None)
        self._restore_node(nid)
        self.invalidate()

    def _restore_node(self, nid: int) -> None:
        rows = self.db.query("SELECT * FROM measurements WHERE node_id=? ORDER BY id DESC LIMIT 3", (nid,))
        if rows:
            self.last[nid] = dict(rows[0])
            f = model.LevelFilter()
            f.raw = [float(r["level_raw_mm"]) for r in reversed(rows) if r["level_raw_mm"] is not None]
            f.value = None if rows[0]["level_mm"] is None else float(rows[0]["level_mm"])
            self.filters[nid] = f

    # ================================================================== misc
    def hostnames(self) -> list[str]:
        names = [socket.gethostname() + ".local"]
        if os.path.exists("/run/retencka/alias-owned"):
            names.insert(0, "retencka.local")
        return names
