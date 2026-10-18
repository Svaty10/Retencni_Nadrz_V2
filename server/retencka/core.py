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
import socket
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Optional

from . import crypto, model
from .auth import IpLimiter, Sessions
from .db import Database
from .external import Http, Hydrawise, Weather
from .model import Packet
from .settings import NODE_SCHEMA, Nodes, Settings

log = logging.getLogger("retencka.core")

FRESH_S = 15 * 60                 # data younger than this may drive the valve
AUTO_COOLDOWN_S = 30 * 60
NO_RISE_LOCK_S = 24 * 3600


PAIR_TARGET_S = 15 * 60
LOW_DISK_MB = 300                 # below this, the oldest raw data are thinned out
BACKUP_KEEP = 7                   # nightly local backups kept
SERVIS_24H_S = 24 * 3600

# audit actions forwarded to Telegram: action -> admins only
NOTIFY_ACTIONS = {"AUTO_REFILL": False, "MANUAL_REFILL": False, "REFILL_ERROR": False, "REFILL_FAULT": False,
                  "RAIN_WAIT": False, "NODE_BOOT": True, "NODE_PAIRED": True, "NODE_UNPAIRED": True,
                  "DISK_LOW": True, "SERVICE_ACCESS": True}
NOTIFY_ICONS = {"AUTO_REFILL": "💧", "MANUAL_REFILL": "💧", "REFILL_ERROR": "⚠️", "REFILL_FAULT": "⚠️",
                "RAIN_WAIT": "🌧", "NODE_BOOT": "🔄", "NODE_PAIRED": "📡", "NODE_UNPAIRED": "📡",
                "DISK_LOW": "💾", "SERVICE_ACCESS": "🛠"}


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
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.radio = None                     # set by __main__
        self.started = time.time()

        self.last: dict[int, dict] = {}       # node_id -> last stored measurement
        self.filters: dict[int, model.LevelFilter] = {}
        self.heard: "OrderedDict[int, dict]" = OrderedDict()
        self._cache: dict[str, tuple[float, Any]] = {}

        # pairing (radio thread <-> loop thread)
        self._pair_lock = threading.Lock()
        self.pair_requests: dict[int, dict] = {}   # node_id -> {ts, rssi, count}
        self.pair_targets: dict[int, dict] = {}    # node_id -> {priv, pub, expires, name, user}
        self._pending_keys: dict[int, tuple] = {}  # node_id -> (k_enc, k_mac)
        self._last_ctr: dict[int, int] = {}

        # refill controller state, per node
        self.refills: dict[int, dict] = {}         # node_id -> open row of the refills table
        self.refill_notes: dict[int, str] = {}
        self._auto_block_until: dict[int, float] = {}
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
        self._restore()

    # ================================================================ helpers
    def audit(self, action: str, detail: str = "", user: str = "system", ip: str = "") -> None:
        try:
            self.db.execute("INSERT INTO audit(ts, username, ip, action, detail) VALUES(?,?,?,?,?)",
                            (int(time.time()), user[:64], ip[:64], action, detail[:500]))
        except Exception:
            log.exception("audit write failed")
        if action in NOTIFY_ACTIONS:
            self.notify(f"{NOTIFY_ICONS.get(action, '')} {detail}", NOTIFY_ACTIONS[action])

    def notify(self, text: str, admin_only: bool = False) -> None:
        if self.notifier is not None:
            try:
                self.notifier(text, admin_only)
            except Exception:
                log.exception("notification failed")

    def watch_tick(self) -> None:
        """Every minute: notify state changes that have no event of their own
        (low level, sensor silent / back, battery low / empty). The first pass only
        records the current state, so a restart does not repeat old warnings."""
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
        name = {"update": "update-request", "reboot": "control-reboot", "servis": "control-servis"}[kind]
        with open(os.path.join(self.data_dir, name), "w") as f:
            f.write(str(int(time.time())))

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
                "checked": st.get("checked")}

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
        """Move the single-node settings of the first v3 build into the nodes table."""
        if self.nodes.ids():
            return
        old = {r["key"]: json.loads(r["value"]) for r in self.db.query("SELECT key, value FROM settings")}
        paired = old.get("paired_node_id")
        if paired is None:
            return
        cfg = {k: old[k] for k in NODE_SCHEMA if k in old}
        try:
            self.nodes.add(int(paired), "Retenční nádrž", cfg)
        except Exception:
            log.exception("migration of the paired node failed")

    def _restore(self) -> None:
        for nid in self.nodes.ids():
            self._restore_node(nid)
            n = self.nodes.get(nid)
            self._last_ctr[nid] = self.db.scalar(
                "SELECT MAX(seq) FROM measurements WHERE node_id=? AND ts >= ?",
                (nid, n.get("paired_at") or 0), -1)
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

    # =========================================================== radio input
    def on_frame(self, payload: bytes, rssi: float, lqi: int) -> None:
        """Radio thread -> event loop."""
        if self.loop:
            self.loop.call_soon_threadsafe(self._on_frame, payload, rssi, lqi)

    def responder(self, payload: bytes) -> Optional[bytes]:
        """Radio thread: answer a PAIR_REQ immediately (the node listens only ~0.6 s).

        A node that holds a key (paired) accepts nothing but an UNPAIR signed with
        that key – a foreign hub can never take it over. The hub sends UNPAIR only
        when the user asked for it here (unpair, or re-pair = unpair + pair), and the
        node asks only after its battery was re-inserted, so unpairing needs both the
        hub login and physical access to the sensor.

        An unpaired node gets a PAIR_ACK when the user wants it paired. Without any
        registered node (first installation) the first request is accepted
        automatically. New keys become valid only after the node proves them with
        its first authenticated DATA frame."""
        req = crypto.parse_pair_req(payload)
        if req is None:
            return None
        nid, node_pub, node_paired = req
        with self._pair_lock:
            t = self.pair_targets.get(nid)
            if t is not None and t["expires"] < time.time():
                t = None
            node = self.nodes.get(nid)
            if node_paired:
                keys = self.nodes.keys(nid)
                if keys and (node.get("_unpair") or t is not None):
                    return crypto.build_unpair(keys[1], nid, node_pub)
                return None
            auto = (not self.nodes.ids() and not self.pair_targets) or (node is not None and not node.get("_secret"))
            if t is None and auto:
                # first installation, or a registered node that never had a key (migration)
                t = {"priv": None, "pub": None, "expires": time.time() + PAIR_TARGET_S,
                     "name": "Retenční nádrž", "user": "system (automaticky)"}
                self.pair_targets[nid] = t
            if t is None:
                return None
            if t["priv"] is None:
                t["priv"], t["pub"] = crypto.x25519_keypair()
            keys = crypto.derive_keys(crypto.x25519(t["priv"], node_pub))
            if keys is None:
                return None
            self._pending_keys[nid] = keys
            return crypto.build_pair_ack(nid, t["pub"])

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
            nid, _, node_paired = req
            self._heard(nid, rssi, "pair")
            with self._pair_lock:
                pr = self.pair_requests.get(nid, {"count": 0})
                pr.update(ts=int(time.time()), rssi=rssi, count=pr["count"] + 1, node_paired=node_paired)
                self.pair_requests[nid] = pr
            node = self.nodes.get(nid)
            if node and not node_paired and node.get("_unpair"):
                # the sensor confirmed it has forgotten its key -> unpairing complete
                self.forget_node(nid, "NODE_UNPAIRED",
                                 f"Čidlo {nid:04X} ({node['name']}) odpárováno – potvrzeno čidlem")
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
        with self._pair_lock:
            pending = self._pending_keys.get(nid)
        if pending:
            new_fields = crypto.open_data(pending[0], pending[1], payload)
            if new_fields is not None:
                self._commit_pairing(nid, pending)
                fields = new_fields
        if fields is None:
            if keys and self.radio:
                self.radio.auth_failures += 1
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
        self._last_ctr[nid] = ctr
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

    def _commit_pairing(self, nid: int, keys: tuple) -> None:
        with self._pair_lock:
            t = self.pair_targets.pop(nid, {})
            self._pending_keys.pop(nid, None)
            self.pair_requests.pop(nid, None)
        new = self.nodes.get(nid) is None
        if new:
            self.nodes.add(nid, t.get("name") or "")
        self.nodes.set_keys(nid, keys[0], keys[1])
        self._last_ctr[nid] = -1
        self.audit("NODE_PAIRED", f"Čidlo {nid:04X} {'spárováno' if new else 'znovu spárováno (nový klíč)'}",
                   t.get("user") or "system")
        log.info("Node %04X paired", nid)

    # ------------------------------------------------------------- pairing
    def start_pairing(self, nid: int, name: str, user: str) -> None:
        node = self.nodes.get(nid)
        if node and node.get("_unpair"):
            self.nodes.set_unpair(nid, False)       # re-pair instead of removing
        with self._pair_lock:
            self.pair_targets[nid] = {"priv": None, "pub": None, "expires": time.time() + PAIR_TARGET_S,
                                      "name": name, "user": user}
            self._pending_keys.pop(nid, None)

    def cancel_pairing(self, nid: int) -> None:
        with self._pair_lock:
            self.pair_targets.pop(nid, None)
            self._pending_keys.pop(nid, None)

    def request_unpair(self, nid: int, user: str, ip: str = "") -> None:
        """Step 1 of unpairing: stop accepting data, wait for the sensor to confirm."""
        self.cancel_pairing(nid)
        self.nodes.set_unpair(nid, True)
        self.last.pop(nid, None)
        self.invalidate()
        node = self.nodes.get(nid) or {}
        self.audit("NODE_UNPAIR_REQUESTED",
                   f"Odpárování čidla {nid:04X} ({node.get('name', '')}) – čeká na vložení baterie do čidla",
                   user, ip)

    def cancel_unpair(self, nid: int, user: str, ip: str = "") -> None:
        self.nodes.set_unpair(nid, False)
        self._restore_node(nid)
        self.invalidate()
        self.audit("NODE_UNPAIR_CANCELLED", f"Odpárování čidla {nid:04X} zrušeno", user, ip)

    def forget_node(self, nid: int, action: str, detail: str, user: str = "system", ip: str = "") -> None:
        """Delete the node and its key (measured data stay in the database)."""
        self.cancel_pairing(nid)
        self.nodes.remove(nid)
        self.last.pop(nid, None)
        self.filters.pop(nid, None)
        self._last_ctr.pop(nid, None)
        self.invalidate()
        self.audit(action, detail + "; naměřená data zůstávají", user, ip)
        log.info("Node %04X removed (%s)", nid, action)

    def pairing_status(self) -> dict:
        now = time.time()
        with self._pair_lock:
            for k in [k for k, v in self.pair_targets.items() if v["expires"] < now]:
                self.pair_targets.pop(k, None)
                self._pending_keys.pop(k, None)
            reqs = [{"id": f"{k:04X}", "rssi": v["rssi"], "age_s": int(now - v["ts"]), "count": v["count"],
                     "known": self.nodes.get(k) is not None, "node_paired": v.get("node_paired", False),
                     "target": k in self.pair_targets, "answered": k in self._pending_keys}
                    for k, v in sorted(self.pair_requests.items(), key=lambda kv: -kv[1]["ts"])
                    if now - v["ts"] < 120]
            targets = [{"id": f"{k:04X}", "name": v["name"], "expires_in": int(v["expires"] - now),
                        "answered": k in self._pending_keys} for k, v in self.pair_targets.items()]
        return {"requests": reqs, "targets": targets}

    # ------------------------------------------------------------ storage
    def _store(self, pkt: model.Packet, fw: int) -> None:
        now = int(time.time())
        node = self.nodes.get(pkt.node_id)
        nid = pkt.node_id
        last = self.last.get(nid)

        level_raw = level_mm = volume_l = None
        if pkt.probe_100uv is not None and not pkt.flags & model.FLAG_PROBE_CLIP:
            lv = model.compute_level(pkt.probe_100uv, node)
            level_raw = lv.level_cm * 10.0
            filt = self.filters.setdefault(nid, model.LevelFilter())
            fmm = filt.update(level_raw, model.deadband_mm(node), follow=nid in self.refills)
            flv = model.level_from_mm(fmm, node)
            level_mm, volume_l = int(round(flv.level_cm * 10)), int(round(flv.volume_l))
            level_raw = int(round(level_raw))

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
        asyncio.ensure_future(self._refill_on_measurement(nid))

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
        pct = model.battery_percent(v)
        cpd = self.cached(f"cpd:{nid}", 3600, lambda: self._cycles_per_day(nid))
        heats = self.cached(f"heat:{nid}", 3600, lambda: self._heats_per_day(nid))
        days = model.battery_days_left(pct, cpd, self.settings.all(), heats) if cpd else None
        low = pct < model.BATTERY_LOW_PCT or bool(last["flags"] & model.FLAG_BAT_LOW)
        sag = last.get("vbat_sag_mv")
        # the node switches itself off below the cut-off (at rest or under TX load)
        # after sending a last packet
        worst_mv = last["vbat_mv"] - (sag or 0)
        off = worst_mv < model.BATTERY_CUTOFF_MV and self.status(nid) != "online"
        return {"voltage": round(v, 3), "percent": round(pct, 1), "low": low or off,
                "days_left": 0 if off else (None if days is None else round(days)), "off": off,
                "sag_mv": sag, "heats_per_day": round(heats, 2)}

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
            "max_depth_cm": node["max_depth_cm"],
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
        }

    def outlook(self, node: dict, lv: Optional[model.Level], flows: list[dict]) -> dict:
        """Simple forecast: how long the water lasts at the recent consumption, and how
        much the roof should bring with the forecast rain (next 7 days)."""
        out = {"days_left": None, "use_per_day_l": None, "rain_7d_mm": None,
               "inflow_7d_l": None, "pct_after_rain": None}
        if lv is None:
            return out
        past = [d for d in flows if d["day"] < datetime.now().strftime("%Y-%m-%d")][-7:]
        if len(past) >= 3:
            use = sum(d["use"] for d in past) / len(past)
            out["use_per_day_l"] = round(use)
            if use >= 5:
                out["days_left"] = round(lv.volume_l / use, 1)
        rain = self.weather.rain_next_days(7)
        if rain is not None:
            inflow = rain * float(node["catchment_area_m2"]) * float(node.get("runoff_coef") or 0.8)
            out["rain_7d_mm"] = rain
            out["inflow_7d_l"] = round(inflow)
            if lv.max_volume_l:
                out["pct_after_rain"] = round(min(100.0, (lv.volume_l + inflow) / lv.max_volume_l * 100.0), 1)
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
        info = {"configured": self._provider(node) is not None, "provider": node.get("refill_provider"),
                "zone_controlled_by": shared,
                "active": r is not None, "status": self.refill_notes.get(nid, "V normě"),
                "max_pct": node["max_refill_pct"], "auto": node["auto_refill_enabled"],
                "waiting_for_rain": nid in self._rain_wait,
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
        if lv is None or self.data_age(nid) > FRESH_S:
            return False, "Chybí čerstvá data o hladině – dopouštění nelze bezpečně spustit."
        target_pct = min(float(target_pct), float(node["max_refill_pct"]))
        need_l = target_pct / 100.0 * lv.max_volume_l - lv.volume_l
        if need_l <= max(50.0, model.deadband_l(node)):
            return False, "Hladina je už na cíli nebo těsně pod ním."
        flow = float(node.get("real_flow_rate_lpm") or node["flow_rate_lpm"])
        seconds = int(min(need_l / flow * 60 * 1.10, int(node["max_refill_minutes"]) * 60))
        ok, msg = await prov[0](seconds)
        if not ok:
            self.audit("REFILL_ERROR", f"{node['name']}: {msg}", user, ip)
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

    async def refill_stop(self, nid: int, reason: str, user: str = "system", ip: str = "",
                          call_api: bool = True) -> tuple[bool, str]:
        async with self._refill_lock:
            return await self._refill_stop(nid, reason, user, ip, call_api)

    async def _refill_stop(self, nid: int, reason: str, user: str, ip: str, call_api: bool) -> tuple[bool, str]:
        node = self.nodes.get(nid)
        ok, msg = True, "OK"
        prov = self._provider(node, for_stop=True) if node else None
        if call_api and prov:
            ok, msg = await prov[1]()
        r = self.refills.pop(nid, None)
        name = node["name"] if node else f"{nid:04X}"
        if r:
            lv = self.level_now(nid)
            end_l = int(lv.volume_l) if lv else None
            self.db.execute("UPDATE refills SET end_ts=?, end_l=?, result=? WHERE id=?",
                            (int(time.time()), end_l, reason, r["id"]))
            self.audit("AUTO_REFILL" if r["mode"] == "auto" else "MANUAL_REFILL",
                       f"Ukončeno ({name}): {reason}" + ("" if ok else f" (ventil: {msg})"), user, ip)
            if r["mode"] == "auto":
                self._auto_block_until[nid] = time.time() + AUTO_COOLDOWN_S
            if end_l is not None and r["start_l"] is not None and end_l - r["start_l"] < 20 \
                    and time.time() - r["start_ts"] > 600:
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
            r = self.refills.get(nid)
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
                self.refill_notes[nid] = "V normě"
                return
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
        for nid, r in list(self.refills.items()):
            if time.time() > r["start_ts"] + (r["planned_s"] or 0) + 60:
                await self.refill_stop(nid, "Časovač vypršel (cíl nepotvrzen měřením)", call_api=False)
            elif self.data_age(nid) > FRESH_S:
                await self.refill_stop(nid, "Bezpečnostní zastavení – výpadek dat z čidla")

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
                "AVG(vbat) vb, MAX(refilling) rf, AVG(rssi) rs FROM hourly WHERE node_id=? AND hour >= ? AND hour < ? "
                "GROUP BY t ORDER BY t", (bucket, bucket, nid, t_from, t_to))
            scale_t = scale_h = scale_r = 1.0
        else:
            rows = self.db.query(
                "SELECT (ts/?)*? AS t, AVG(level_mm) l, AVG(volume_l) v, AVG(temp_x100) te, "
                "AVG(hum_x100) hu, AVG(vbat_mv) vb, MAX(refilling) rf, AVG(rssi_x2) rs FROM measurements "
                "WHERE node_id=? AND ts >= ? AND ts < ? GROUP BY t ORDER BY t",
                (bucket, bucket, nid, t_from, t_to))
            scale_t = scale_h = 0.01
            scale_r = 0.5
        max_mm = float(node["max_depth_cm"]) * 10.0
        trim = float(node["battery_trim"])
        out = {"bucket_s": bucket, "t": [], "level_pct": [], "level_cm": [], "volume_l": [],
               "temp": [], "hum": [], "vbat": [], "rssi": [], "refill": []}

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
            out["level_pct"].append(r1(r["l"], 100.0 / max_mm, 1) if max_mm else None)
            out["level_cm"].append(r1(r["l"], 0.1, 1))
            out["volume_l"].append(r1(r["v"], 1.0, 0))
            out["temp"].append(r1(r["te"], scale_t, 2))
            out["hum"].append(r1(r["hu"], scale_h, 1))
            out["vbat"].append(r1(r["vb"], 0.001 * trim, 3))
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

    def daily_flows(self, nid: int, t_from: int, t_to: int) -> list[dict]:
        return self._daily_flows(nid, t_from, t_to)

    # ============================================================ maintenance
    def aggregate_hours(self, since: Optional[int] = None) -> None:
        since = since if since is not None else int(time.time()) - 3 * 3600
        since -= since % 3600
        self.db.execute(
            "INSERT OR REPLACE INTO hourly(node_id, hour, n, level_mm, level_min, level_max, volume_l, temp, "
            "hum, vbat, refilling, rssi) "
            "SELECT node_id, (ts/3600)*3600 h, COUNT(*), AVG(level_mm), MIN(level_mm), MAX(level_mm), "
            "AVG(volume_l), AVG(temp_x100)/100.0, AVG(hum_x100)/100.0, AVG(vbat_mv), MAX(refilling), "
            "AVG(rssi_x2)/2.0 "
            "FROM measurements WHERE ts >= ? GROUP BY node_id, h", (since,))

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
        st = os.statvfs(self.data_dir)
        return st.f_bavail * st.f_frsize / 1048576

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
        work = os.path.join(self.backup_dir(), "import.tmp")
        with open(path, "rb") as f:
            gz = f.read(2) == b"\x1f\x8b"
        if gz:
            try:
                with gzip.open(path, "rb") as fi, open(work, "wb") as fo:
                    shutil.copyfileobj(fi, fo, 65536)
            except (OSError, EOFError):
                raise ValueError("Soubor není platná záloha (poškozený gzip).")
        else:
            shutil.copyfile(path, work)
        try:
            try:
                chk = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
                ok = chk.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                tables = {r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                ver = chk.execute("PRAGMA user_version").fetchone()[0]
                chk.close()
            except sqlite3.DatabaseError:
                raise ValueError("Soubor není záloha Retenční nádrže.")
            if not ok or not {"measurements", "settings", "users"} <= tables:
                raise ValueError("Soubor není záloha Retenční nádrže (nebo je poškozený).")
            if ver > self.db.scalar("PRAGMA user_version", (), 0):
                raise ValueError("Záloha je z novější verze – nejdřív aktualizujte hub.")
            return self._merge(work, tables, full)
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.remove(work + suffix)
                except OSError:
                    pass

    def _merge(self, work: str, tables: set, full: bool) -> dict:
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
        self.aggregate_hours(since=0)
        for nid in self.nodes.ids():
            n = self.nodes.get(nid)
            self._restore_node(nid)
            self._last_ctr[nid] = max(self._last_ctr.get(nid, -1), self.db.scalar(
                "SELECT MAX(seq) FROM measurements WHERE node_id=? AND ts >= ?", (nid, n.get("paired_at") or 0), -1))
        self.invalidate()
        return stats

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
