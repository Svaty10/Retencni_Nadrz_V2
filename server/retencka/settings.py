"""Typed, validated settings stored in SQLite (atomic writes, survives power loss).

* Settings  – global hub settings (weather, Hydrawise, refill rules, system)
* Nodes     – one row per sensor node: name + its own tank geometry and calibration
"""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
from typing import Any, Optional

from .db import Database

# key: (default, type, min, max) – type in {"float", "int", "bool", "str", "points", "json"}
SCHEMA: dict[str, tuple[Any, str, Any, Any]] = {
    # weather
    "latitude":                  (50.0755, "float", -90.0, 90.0),
    "longitude":                 (14.4378, "float", -180.0, 180.0),
    # Hydrawise account key (shared by all nodes unless a node has its own)
    "hydrawise_api_key":         ("", "str", 0, 128),
    # battery / node energy model (same hardware for all nodes)
    "battery_capacity_mah":      (3000.0, "float", 100.0, 100000.0),
    "node_cycle_mAs":            (3.3, "float", 0.01, 100.0),
    "node_sleep_uA":             (1.5, "float", 0.0, 1000.0),
    "node_heat_mAs":             (66.0, "float", 0.0, 1000.0),   # one heating = 2 x 110 mW / 1 s
    # system
    "stale_timeout_s":           (1800, "int", 300, 86400),
    # raw 5-min measurements older than this are deleted (0 = keep forever);
    # hourly aggregates are always kept. ~10 MB per sensor and year.
    "retention_days":            (0, "int", 0, 36500),
}

# Per-node configuration (tank + probe of that node)
NODE_SCHEMA: dict[str, tuple[Any, str, Any, Any]] = {
    "tank_area_m2":       (10.9, "float", 0.01, 1000.0),
    "max_depth_cm":       (92.0, "float", 5.0, 2000.0),
    "sensor_offset_cm":   (1.0, "float", -100.0, 500.0),
    # [[probe_mV, water column above the sensor in cm], ...] – 2 points are enough
    "cal_points":         ([[120.0, 12.0], [1240.0, 92.0]], "points", None, None),
    # probe datasheet accuracy, e.g. 0.5 % of a 200 cm range = ±1 cm -> dead band
    "probe_accuracy_pct": (0.5, "float", 0.0, 10.0),
    "probe_range_cm":     (200.0, "float", 10.0, 10000.0),
    "catchment_area_m2":  (140.0, "float", 0.0, 100000.0),
    "runoff_coef":        (0.8, "float", 0.05, 1.0),       # share of roof rain reaching the tank
    "battery_trim":       (1.0, "float", 0.8, 1.2),
    # refilling of THIS tank – provider "none" | "hydrawise" (more can be added later)
    "refill_provider":        ("none", "choice:none,hydrawise", None, None),
    "hydrawise_relay_id":     ("", "str", 0, 32),
    "hydrawise_api_key":      ("", "str", 0, 128),     # optional, else the global account key
    "auto_refill_enabled":    (False, "bool", None, None),
    "refill_threshold_pct":   (20.0, "float", 0.0, 100.0),
    "auto_refill_target_pct": (30.0, "float", 0.0, 100.0),
    "max_refill_pct":         (90.0, "float", 0.0, 100.0),
    # waiting for forecast rain instead of refilling (see Core._refill_on_measurement)
    "rain_wait_enabled":       (True, "bool", None, None),
    "rain_wait_hours":         (24, "int", 1, 72),       # forecast horizon / longest wait
    "rain_wait_max_below_pct": (10.0, "float", 0.0, 100.0),  # never wait this far below the threshold
    "rain_wait_max_drop_pct":  (5.0, "float", 0.0, 100.0),   # stop waiting after this drop
    "flow_rate_lpm":          (41.6, "float", 0.5, 1000.0),
    "max_refill_minutes":     (120, "int", 1, 1440),
    "real_flow_rate_lpm":     (None, "json", None, None),   # measured, internal
}
NODE_SECRET_KEYS = {"hydrawise_api_key"}
NODE_INTERNAL_KEYS = {"real_flow_rate_lpm"}

SECRET_KEYS = {"hydrawise_api_key"}
INTERNAL_KEYS = {"ha_token"}
MAX_NODES = 16


class SettingsError(ValueError):
    pass


class Settings:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._cache: dict[str, Any] = {}
        self.reload()
        if not self.get("ha_token"):
            self._write({"ha_token": secrets.token_urlsafe(32)})

    def reload(self) -> None:
        data: dict[str, Any] = {k: v[0] for k, v in SCHEMA.items()}
        for r in self.db.query("SELECT key, value FROM settings"):
            try:
                data[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
        with self._lock:
            self._cache = data

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._cache.get(key, default)

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._cache)

    def public(self) -> dict[str, Any]:
        out = {}
        for k, v in self.all().items():
            if k not in SCHEMA:
                continue
            out[k] = mask(v) if k in SECRET_KEYS else v
        return out

    def update(self, changes: dict[str, Any], allow_internal: bool = False) -> list[str]:
        clean: dict[str, Any] = {}
        current = self.all()
        for key, raw in changes.items():
            if key in INTERNAL_KEYS:
                if not allow_internal:
                    raise SettingsError(f"Nastavení {key} nelze měnit")
                val = raw
            elif key in SCHEMA:
                if key in SECRET_KEYS and isinstance(raw, str) and raw.startswith("•"):
                    continue
                val = coerce(SCHEMA, key, raw)
            else:
                raise SettingsError(f"Neznámé nastavení: {key}")
            if current.get(key) != val:
                clean[key] = val
        if clean:
            self._write(clean)
        return sorted(clean)

    def _write(self, values: dict[str, Any]) -> None:
        with self.db.transaction() as c:
            for k, v in values.items():
                c.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
        with self._lock:
            self._cache.update(values)


class Nodes:
    """Registered sensor nodes with their per-node configuration (cached)."""

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._cache: dict[int, dict] = {}
        self.reload()

    def reload(self) -> None:
        nodes = {}
        for r in self.db.query("SELECT node_id, name, config, sort, secret, paired_at, unpair_at FROM nodes ORDER BY sort, node_id"):
            cfg = {k: v[0] for k, v in NODE_SCHEMA.items()}
            try:
                cfg.update(json.loads(r["config"]))
            except ValueError:
                pass
            cfg["name"] = r["name"]
            cfg["node_id"] = r["node_id"]
            cfg["sort"] = r["sort"]
            cfg["paired_at"] = r["paired_at"]
            cfg["_secret"] = r["secret"]
            cfg["_unpair"] = r["unpair_at"]
            nodes[r["node_id"]] = cfg
        with self._lock:
            self._cache = nodes

    def ids(self) -> list[int]:
        with self._lock:
            return [n["node_id"] for n in sorted(self._cache.values(), key=lambda x: (x["sort"], x["node_id"]))]

    def get(self, node_id: Optional[int]) -> Optional[dict]:
        with self._lock:
            n = self._cache.get(node_id) if node_id is not None else None
            return dict(n) if n else None

    def primary(self) -> Optional[int]:
        ids = self.ids()
        return ids[0] if ids else None

    def all(self) -> list[dict]:
        return [self.get(i) for i in self.ids()]

    def add(self, node_id: int, name: str, config: Optional[dict] = None) -> None:
        if not 0 <= node_id <= 0xFFFF:
            raise SettingsError("Neplatné ID čidla (4 hexadecimální znaky).")
        if self.get(node_id):
            raise SettingsError("Čidlo už je přidané.")
        if len(self.ids()) >= MAX_NODES:
            raise SettingsError(f"Maximálně {MAX_NODES} čidel.")
        name = _clean_name(name) or f"Nádrž {node_id:04X}"
        cfg = {k: v[0] for k, v in NODE_SCHEMA.items()}
        for k, v in (config or {}).items():
            if k in NODE_SCHEMA:
                cfg[k] = coerce(NODE_SCHEMA, k, v)
        sort = len(self.ids())
        self.db.execute("INSERT INTO nodes(node_id, name, config, created, sort) VALUES(?,?,?,?,?)",
                        (node_id, name, json.dumps(cfg), int(time.time()), sort))
        self.reload()

    def keys(self, node_id: int) -> Optional[tuple[bytes, bytes]]:
        n = self.get(node_id)
        if not n or not n.get("_secret"):
            return None
        raw = bytes.fromhex(n["_secret"])
        return raw[:16], raw[16:32]

    def set_keys(self, node_id: int, k_enc: bytes, k_mac: bytes) -> None:
        self.db.execute("UPDATE nodes SET secret=?, paired_at=? WHERE node_id=?",
                        ((k_enc + k_mac).hex(), int(time.time()), node_id))
        self.reload()

    def set_unpair(self, node_id: int, pending: bool) -> None:
        """Mark the node for unpairing (hub ignores its data until the sensor confirms)."""
        self.db.execute("UPDATE nodes SET unpair_at=? WHERE node_id=?",
                        (int(time.time()) if pending else None, node_id))
        self.reload()

    def public(self, node_id: int) -> Optional[dict]:
        n = self.get(node_id)
        if not n:
            return None
        out = {k: v for k, v in n.items() if not k.startswith("_") and k not in ("node_id", "sort")}
        for k in NODE_SECRET_KEYS:
            out[k] = mask(out.get(k))
        out["id"] = f"{node_id:04X}"
        out["paired"] = bool(n.get("_secret"))
        out["unpairing"] = bool(n.get("_unpair"))
        return out

    def update(self, node_id: int, changes: dict, allow_internal: bool = False) -> list[str]:
        cur = self.get(node_id)
        if not cur:
            raise SettingsError("Čidlo neexistuje.")
        changed = []
        name = cur["name"]
        cfg = {k: cur[k] for k in NODE_SCHEMA}
        for k, raw in changes.items():
            if k == "name":
                n = _clean_name(raw)
                if not n:
                    raise SettingsError("Zadejte název čidla.")
                if n != name:
                    name = n
                    changed.append("name")
            elif k in NODE_SCHEMA:
                if k in NODE_INTERNAL_KEYS and not allow_internal:
                    raise SettingsError(f"Nastavení {k} nelze měnit")
                if k in NODE_SECRET_KEYS and isinstance(raw, str) and raw.startswith("•"):
                    continue
                v = coerce(NODE_SCHEMA, k, raw)
                if cfg[k] != v:
                    cfg[k] = v
                    changed.append(k)
            else:
                raise SettingsError(f"Neznámé nastavení čidla: {k}")
        if cfg["auto_refill_target_pct"] <= cfg["refill_threshold_pct"]:
            raise SettingsError("Cíl automatického dopouštění musí být vyšší než spouštěcí hranice")
        if cfg["auto_refill_target_pct"] > cfg["max_refill_pct"]:
            raise SettingsError("Cíl automatického dopouštění nesmí překročit maximální limit")
        if changed:
            self.db.execute("UPDATE nodes SET name=?, config=? WHERE node_id=?",
                            (name, json.dumps(cfg), node_id))
            self.reload()
        return changed

    def remove(self, node_id: int) -> None:
        self.db.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))
        self.reload()


def mask(v: Any) -> str:
    return ("•" * 8 + str(v)[-4:]) if v else ""


def _clean_name(raw: Any) -> str:
    n = " ".join(str(raw or "").split())[:40]
    return "".join(ch for ch in n if ch.isprintable() and ch not in "<>\"'&")


def coerce(schema: dict, key: str, raw: Any) -> Any:
    default, typ, lo, hi = schema[key]
    try:
        if typ == "float":
            v = float(str(raw).replace(",", "."))
            if not math.isfinite(v):
                raise ValueError
            if not lo <= v <= hi:
                raise SettingsError(f"{key}: hodnota mimo rozsah {lo}–{hi}")
            return round(v, 6)
        if typ == "int":
            v = int(float(str(raw).replace(",", ".")))
            if not lo <= v <= hi:
                raise SettingsError(f"{key}: hodnota mimo rozsah {lo}–{hi}")
            return v
        if typ == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "ano", "on", "yes")
        if typ == "str":
            v = str(raw).strip()
            if len(v) > hi or any(ord(ch) < 32 for ch in v):
                raise SettingsError(f"{key}: neplatný text")
            return v
        if typ == "points":
            pts = [[float(p[0]), float(p[1])] for p in raw]
            if not 2 <= len(pts) <= 8:
                raise SettingsError("Kalibrace potřebuje 2 body (nejvýše 8).")
            for mv, cm in pts:
                if not (0 <= mv <= 6000 and -50 <= cm <= 2000):
                    raise SettingsError("Kalibrační bod mimo rozsah (0–6000 mV, −50–2000 cm)")
            pts.sort(key=lambda p: p[0])
            for a, b in zip(pts, pts[1:]):
                if b[0] - a[0] < 20.0:
                    raise SettingsError("Kalibrační body musí být od sebe aspoň 20 mV (ideálně prázdná a plná nádrž).")
            return pts
        if typ == "json":
            return raw
        if typ.startswith("choice:"):
            v = str(raw).strip()
            if v not in typ[7:].split(","):
                raise SettingsError(f"{key}: neplatná volba")
            return v
    except SettingsError:
        raise
    except (TypeError, ValueError, IndexError) as e:
        raise SettingsError(f"{key}: neplatná hodnota") from e
    return raw
