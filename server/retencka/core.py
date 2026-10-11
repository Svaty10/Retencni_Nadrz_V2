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


PAIR_TARGET_S = 15 * 60           # how long the hub waits for a node the user wants to pair


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
        """Radio thread: answer a PAIR_REQ immediately if the user wants this node paired.

        Without any registered node (first installation) the first request is accepted
        automatically. The new keys become valid only after the node proves them with
        its first authenticated DATA frame; until then the old keys keep working."""
        req = crypto.parse_pair_req(payload)
        if req is None:
            return None
        nid, node_pub = req
        with self._pair_lock:
            t = self.pair_targets.get(nid)
            node = self.nodes.get(nid)
            auto = (not self.nodes.ids() and not self.pair_targets) or (node is not None and not node.get("_secret"))
            if t is None and auto:
                # first installation, or a registered node that never had a key (migration)
                t = {"priv": None, "pub": None, "expires": time.time() + PAIR_TARGET_S,
                     "name": "Retenční nádrž", "user": "system (automaticky)"}
                self.pair_targets[nid] = t
            if t is None or t["expires"] < time.time():
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
            nid = req[0]
            self._heard(nid, rssi, "pair")
            with self._pair_lock:
                pr = self.pair_requests.get(nid, {"count": 0})
                pr.update(ts=int(time.time()), rssi=rssi, count=pr["count"] + 1)
                self.pair_requests[nid] = pr
            return

        hdr = crypto.data_header(payload)
        if hdr is None:
            if self.radio:
                self.radio.foreign_packets += 1
            return
        nid, ctr = hdr
        self._heard(nid, rssi, "data")
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
        if ctr <= self._last_ctr.get(nid, -1):
            if self.radio:
                self.radio.replays += 1
            return                                  # replay or duplicate
        self._last_ctr[nid] = ctr
        flags, vbat, temp, hum, probe, fw = fields
        pkt = model.Packet(
            flags=flags, node_id=nid, seq=ctr,
            vbat_mv=None if (flags & model.FLAG_BAT_ERR or vbat == model.U16_INVALID) else vbat,
            temp_x100=None if (flags & model.FLAG_SHT_ERR or temp == model.TEMP_INVALID) else temp,
            hum_x100=None if (flags & model.FLAG_SHT_ERR or hum == model.U16_INVALID) else hum,
            probe_100uv=None if (flags & model.FLAG_PROBE_ERR or probe == model.U16_INVALID) else probe,
            rssi_dbm=rssi, lqi=lqi)
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
        with self._pair_lock:
            self.pair_targets[nid] = {"priv": None, "pub": None, "expires": time.time() + PAIR_TARGET_S,
                                      "name": name, "user": user}
            self._pending_keys.pop(nid, None)

    def cancel_pairing(self, nid: int) -> None:
        with self._pair_lock:
            self.pair_targets.pop(nid, None)
            self._pending_keys.pop(nid, None)

    def pairing_status(self) -> dict:
        now = time.time()
        with self._pair_lock:
            for k in [k for k, v in self.pair_targets.items() if v["expires"] < now]:
                self.pair_targets.pop(k, None)
                self._pending_keys.pop(k, None)
            reqs = [{"id": f"{k:04X}", "rssi": v["rssi"], "age_s": int(now - v["ts"]), "count": v["count"],
                     "known": self.nodes.get(k) is not None,
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
            fmm = filt.update(level_raw, model.deadband_mm(node))
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
                   lqi=pkt.lqi, refilling=refilling, time_ok=1 if synced else 0)
        try:
            cur = self.db.execute(
                "INSERT INTO measurements(ts,node_id,seq,flags,vbat_mv,temp_x100,hum_x100,probe_100uv,"
                "level_mm,level_raw_mm,volume_l,rssi_x2,lqi,refilling,time_ok) VALUES "
                "(:ts,:node_id,:seq,:flags,:vbat_mv,:temp_x100,:hum_x100,:probe_100uv,"
                ":level_mm,:level_raw_mm,:volume_l,:rssi_x2,:lqi,:refilling,:time_ok)", row)
            if not synced and self._time_anchor is None:
                self._time_anchor = (time.time(), time.monotonic(), cur.lastrowid)
        except Exception:
            log.exception("measurement insert failed")
        row["fw"] = fw
        if last:                           # keep previous values when a sensor reported an error
            for k in ("level_mm", "level_raw_mm", "volume_l", "probe_100uv", "temp_x100", "hum_x100", "vbat_mv"):
                if row[k] is None:
                    row[f"{k}_stale"] = True
                    row[k] = last.get(k)
        self.last[nid] = row
        self.invalidate()
        if pkt.flags & model.FLAG_BOOT:
            self.audit("NODE_BOOT", f"Čidlo {node['name']} ({nid:04X}) hlásí restart" +
                       (" (watchdog)" if pkt.flags & model.FLAG_WDT_RESET else ""))
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
            return {"voltage": None, "percent": None, "low": False, "days_left": None}
        v = last["vbat_mv"] / 1000.0 * float(node["battery_trim"])
        pct = model.battery_percent(v)
        cpd = self.cached(f"cpd:{nid}", 3600, lambda: self._cycles_per_day(nid))
        days = model.battery_days_left(pct, cpd, self.settings.all()) if cpd else None
        low = pct < model.BATTERY_LOW_PCT or bool(last["flags"] & model.FLAG_BAT_LOW)
        return {"voltage": round(v, 3), "percent": round(pct, 1), "low": low,
                "days_left": None if days is None else round(days)}

    def _cycles_per_day(self, nid: int) -> float:
        since = int(time.time()) - 7 * 86400
        row = self.db.one("SELECT COUNT(*) n, MIN(ts) first FROM measurements WHERE node_id=? AND ts >= ?",
                          (nid, since))
        if not row or not row["n"] or not row["first"]:
            return 86400 / model.NODE_INTERVAL_S
        span_days = max(1 / 24, (time.time() - row["first"]) / 86400)
        return max(1.0, row["n"] / span_days)

    def link_quality(self, nid: int) -> dict:
        def calc():
            rows = self.db.query("SELECT seq, flags FROM measurements WHERE node_id=? AND ts >= ? ORDER BY id",
                                 (nid, int(time.time()) - 86400))
            lost, prev = 0, None
            for r in rows:
                if prev is not None and not r["flags"] & model.FLAG_BOOT:
                    gap = r["seq"] - prev - 1
                    if 0 <= gap < 1000:
                        lost += gap
                prev = r["seq"]
            got = len(rows)
            return {"received_24h": got, "lost_24h": lost,
                    "loss_pct": round(100.0 * lost / (got + lost), 1) if got + lost else None}
        return self.cached(f"link:{nid}", 120, calc)

    def node_summaries(self) -> list[dict]:
        out = []
        for n in self.nodes.all():
            nid = n["node_id"]
            lv = self.level_now(nid)
            last = self.last.get(nid) or {}
            out.append({"id": f"{nid:04X}", "name": n["name"], "status": self.status(nid),
                        "level_pct": None if lv is None else round(lv.percent, 1),
                        "rssi": None if last.get("rssi_x2") is None else last["rssi_x2"] / 2.0,
                        "age_s": self.age_s(nid), "battery": self.battery(nid),
                        "refilling": nid in self.refills,
                        "refill_configured": self._provider(n) is not None,
                        "paired": bool(n.get("_secret"))})
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
            "consumption_today": round(sum(d["use"] for d in flows if d["day"] == today)),
            "consumption_7d": round(sum(d["use"] for d in flows if d["day"] >= week_start)),
        }

    def _radio_info(self) -> dict:
        return {"ok": bool(self.radio and self.radio.hardware_ok), "mode": getattr(self.radio, "mode", None)}

    # ================================================================ refill
    # Each node controls its own tank. The valve is driven through a provider
    # (currently Hydrawise); the provider's own timer is always set as a fail-safe,
    # so the valve closes even if the hub dies during refilling.
    def _provider(self, node: dict):
        if node.get("refill_provider") == "hydrawise":
            key = node.get("hydrawise_api_key") or self.settings.get("hydrawise_api_key")
            relay = node.get("hydrawise_relay_id")
            if key and relay:
                return (lambda secs: self.hydrawise.call(key, relay, "run", secs),
                        lambda: self.hydrawise.call(key, relay, "stop"))
        return None

    def refill_info(self, nid: Optional[int]) -> dict:
        node = self.nodes.get(nid)
        if node is None:
            return {"configured": False, "active": False, "status": "–"}
        r = self.refills.get(nid)
        info = {"configured": self._provider(node) is not None, "provider": node.get("refill_provider"),
                "active": r is not None, "status": self.refill_notes.get(nid, "V normě"),
                "max_pct": node["max_refill_pct"], "auto": node["auto_refill_enabled"],
                "flow_lpm": node.get("real_flow_rate_lpm") or node["flow_rate_lpm"]}
        if r:
            info.update(mode=r["mode"], target_pct=r["target_pct"],
                        ends_at=r["start_ts"] + (r["planned_s"] or 0),
                        remaining_s=max(0, r["start_ts"] + (r["planned_s"] or 0) - int(time.time())))
        return info

    async def refill_start(self, nid: int, target_pct: float, mode: str, user: str, ip: str = "") -> tuple[bool, str]:
        node = self.nodes.get(nid)
        if node is None:
            return False, "Čidlo neexistuje."
        prov = self._provider(node)
        if prov is None:
            return False, "Dopouštění není pro tuto nádrž nastaveno."
        if nid in self.refills:
            return False, "Dopouštění už běží."
        lv = self.level_now(nid)
        if lv is None or (self.age_s(nid) or 1e9) > FRESH_S:
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
        node = self.nodes.get(nid)
        ok, msg = True, "OK"
        prov = self._provider(node) if node else None
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
            if not node["auto_refill_enabled"] or lv.percent >= node["refill_threshold_pct"]:
                self.refill_notes[nid] = "V normě"
                return
            if time.time() < self._auto_block_until.get(nid, 0):
                self.refill_notes[nid] = "Automatické dopouštění dočasně pozastaveno"
                return
            if s["rain_defer_enabled"] and lv.percent > node["critical_level_pct"]:
                rain = await self.weather.rain_next_2_days(s["latitude"], s["longitude"])
                if rain is not None and rain >= s["rain_defer_threshold_mm"]:
                    self.refill_notes[nid] = f"Dopouštění odloženo (předpověď {rain:.1f} mm srážek)"
                    return
            ok, msg = await self.refill_start(nid, node["auto_refill_target_pct"], "auto", "system")
            if not ok:
                self.refill_notes[nid] = f"Automatické dopouštění selhalo: {msg}"
                self._auto_block_until[nid] = time.time() + AUTO_COOLDOWN_S
        except Exception:
            log.exception("refill logic failed")

    async def refill_tick(self) -> None:
        """Every 30 s: timer expiry and stale-data safety stop, for every running refill."""
        for nid, r in list(self.refills.items()):
            if time.time() > r["start_ts"] + (r["planned_s"] or 0) + 60:
                await self.refill_stop(nid, "Časovač vypršel (cíl nepotvrzen měřením)", call_api=False)
            elif (self.age_s(nid) or 1e9) > FRESH_S:
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
                "AVG(vbat) vb, MAX(refilling) rf FROM hourly WHERE node_id=? AND hour >= ? AND hour < ? "
                "GROUP BY t ORDER BY t", (bucket, bucket, nid, t_from, t_to))
            scale_t = scale_h = 1.0
        else:
            rows = self.db.query(
                "SELECT (ts/?)*? AS t, AVG(level_mm) l, AVG(volume_l) v, AVG(temp_x100) te, "
                "AVG(hum_x100) hu, AVG(vbat_mv) vb, MAX(refilling) rf FROM measurements "
                "WHERE node_id=? AND ts >= ? AND ts < ? GROUP BY t ORDER BY t",
                (bucket, bucket, nid, t_from, t_to))
            scale_t = scale_h = 0.01
        max_mm = float(node["max_depth_cm"]) * 10.0
        trim = float(node["battery_trim"])
        out = {"bucket_s": bucket, "t": [], "level_pct": [], "level_cm": [], "volume_l": [],
               "temp": [], "hum": [], "vbat": [], "refill": []}

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
            out["refill"].append(int(r["rf"] or 0))
        return out

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
            "hum, vbat, refilling) "
            "SELECT node_id, (ts/3600)*3600 h, COUNT(*), AVG(level_mm), MIN(level_mm), MAX(level_mm), "
            "AVG(volume_l), AVG(temp_x100)/100.0, AVG(hum_x100)/100.0, AVG(vbat_mv), MAX(refilling) "
            "FROM measurements WHERE ts >= ? GROUP BY node_id, h", (since,))

    def maintenance(self) -> None:
        """Daily: retention, session purge, statistics."""
        keep = int(self.settings.get("retention_days")) * 86400
        now = int(time.time())
        self.db.execute("DELETE FROM measurements WHERE ts < ?", (now - keep,))
        self.db.execute("DELETE FROM audit WHERE ts < ?", (now - 3 * 365 * 86400,))
        self.sessions.purge()
        self.db.execute("PRAGMA optimize")

    def recompute_levels(self, nid: int) -> int:
        """Re-apply calibration + filter to the whole history of one node (worker thread)."""
        node = self.nodes.get(nid)
        if node is None:
            return 0
        db_mm = model.deadband_mm(node)
        filt = model.LevelFilter()
        n, last_id = 0, 0
        while True:
            rows = self.db.query("SELECT id, probe_100uv, flags FROM measurements WHERE node_id=? AND id > ? "
                                 "ORDER BY id LIMIT 2000", (nid, last_id))
            if not rows:
                break
            upd = []
            for r in rows:
                last_id = r["id"]
                if r["probe_100uv"] is None or r["flags"] & model.FLAG_PROBE_CLIP:
                    continue
                raw = model.compute_level(r["probe_100uv"], node).level_cm * 10.0
                flv = model.level_from_mm(filt.update(raw, db_mm), node)
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
