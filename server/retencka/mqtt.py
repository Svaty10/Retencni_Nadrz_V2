"""Home Assistant over MQTT (MQTT Discovery).

The hub connects as a client to an MQTT broker (usually the Mosquitto add-on of Home
Assistant). Every tank is announced as a device with its entities, so Home Assistant
creates them by itself – no YAML. States are published as soon as something changes
and at least once a minute; buttons start / stop refilling (can be switched off).

Topics (<base> = retencka/<uid>, uid random per hub, <node> = sensor ID in hex):
  <base>/status                 online / offline (last will)             retained
  <base>/<node>/state           JSON state of the tank                    retained
  <base>/<node>/availability    online / offline (sensor silent)          retained
  <base>/<node>/refill/set      START / STOP  (from Home Assistant)
  homeassistant/<component>/retencka_<uid>_<node>/<object>/config         retained
  homeassistant/status          Home Assistant (re)started -> publish everything again

Minimal MQTT 3.1.1 client on the event loop (QoS 0, standard library only). Retained
topics the hub published are remembered in mqtt-retained.json, so a removed tank (also
while the hub was off) or switching MQTT off deletes its entities in Home Assistant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import ssl
import struct
import time
from datetime import datetime, timezone
from typing import Optional

from . import __version__

log = logging.getLogger("retencka.mqtt")

DISCOVERY = "homeassistant"
KEEPALIVE_S = 60
PING_S = 30
REFRESH_S = 60
CONNECT_TIMEOUT_S = 10
MAX_PACKET = 64 * 1024
RETAINED_FILE = "mqtt-retained.json"
REPO_URL = "https://github.com/Svaty10/Retencni_Nadrz_V2"

CONNACK_ERRORS = {
    1: "Broker nepodporuje MQTT 3.1.1",
    2: "Broker odmítl ID klienta",
    3: "Broker teď nepřijímá spojení",
    4: "Špatné jméno nebo heslo",
    5: "Broker vyžaduje jméno a heslo",
}


class Refused(Exception):
    """CONNACK with an error code (text for the web)."""


# ------------------------------------------------------------- packets
def _varint(n: int) -> bytes:
    """MQTT remaining length.

    >>> _varint(0), _varint(127), _varint(128), _varint(16383)
    (b'\\x00', b'\\x7f', b'\\x80\\x01', b'\\xff\\x7f')
    """
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _packet(first: int, body: bytes) -> bytes:
    return bytes([first]) + _varint(len(body)) + body


def connect_packet(client_id: str, username: str, password: str, will_topic: str,
                   will_msg: bytes, keepalive: int = KEEPALIVE_S) -> bytes:
    flags = 0x02 | 0x04 | 0x20                      # clean session, will, will retained
    payload = _str(client_id) + _str(will_topic) + struct.pack(">H", len(will_msg)) + will_msg
    if username:
        flags |= 0x80
        payload += _str(username)
        if password:                                # 3.1.1: a password needs a user name
            flags |= 0x40
            payload += _str(password)
    return _packet(0x10, _str("MQTT") + bytes([4, flags]) + struct.pack(">H", keepalive) + payload)


def publish_packet(topic: str, payload: bytes, retain: bool = False) -> bytes:
    return _packet(0x30 | (1 if retain else 0), _str(topic) + payload)


def subscribe_packet(pid: int, topics: list[str]) -> bytes:
    return _packet(0x82, struct.pack(">H", pid) + b"".join(_str(t) + b"\x00" for t in topics))


PINGREQ = b"\xc0\x00"
DISCONNECT = b"\xe0\x00"


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first = (await reader.readexactly(1))[0]
    n, mult = 0, 1
    for _ in range(4):
        b = (await reader.readexactly(1))[0]
        n += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    else:
        raise ValueError("bad MQTT length")
    if n > MAX_PACKET:
        raise ValueError("MQTT packet too large")
    return first, (await reader.readexactly(n) if n else b"")


def parse_publish(first: int, body: bytes) -> tuple[str, bytes, int, Optional[int]]:
    tl = struct.unpack(">H", body[:2])[0]
    topic = body[2:2 + tl].decode("utf-8", "replace")
    rest = body[2 + tl:]
    qos, pid = (first >> 1) & 3, None
    if qos:
        pid = struct.unpack(">H", rest[:2])[0]
        rest = rest[2:]
    return topic, rest, qos, pid


def error_text(e: BaseException) -> str:
    if isinstance(e, Refused):
        return str(e)
    if isinstance(e, socket.gaierror):
        return "Adresa brokeru nenalezena"
    if isinstance(e, ssl.SSLError):
        return "Chyba zabezpečeného spojení (TLS, certifikát)"
    if isinstance(e, ConnectionRefusedError):
        return "Broker odmítl spojení – běží a je port správně?"
    if isinstance(e, asyncio.TimeoutError):
        return "Broker neodpovídá"
    if isinstance(e, (asyncio.IncompleteReadError, ConnectionError)):
        return "Spojení s brokerem přerušeno"
    return "Broker není dostupný"


def _iso(ts: Optional[int]) -> Optional[str]:
    return None if not ts else datetime.fromtimestamp(ts, timezone.utc).isoformat()


# ------------------------------------------------------------- entities
# object id: (component, name, extra config, uses sensor availability)
SENSORS = [
    ("hladina", "sensor", "Hladina", {"value_template": "{{ value_json.level_pct }}", "unit_of_measurement": "%",
                                      "state_class": "measurement", "icon": "mdi:water-percent"}, True),
    ("vyska", "sensor", "Výška hladiny", {"value_template": "{{ value_json.level_cm }}", "unit_of_measurement": "cm",
                                          "device_class": "distance", "state_class": "measurement"}, True),
    ("objem", "sensor", "Objem", {"value_template": "{{ value_json.volume_l }}", "unit_of_measurement": "L",
                                  "device_class": "volume_storage", "state_class": "measurement"}, True),
    ("teplota", "sensor", "Teplota", {"value_template": "{{ value_json.temperature }}", "unit_of_measurement": "°C",
                                      "device_class": "temperature", "state_class": "measurement"}, True),
    ("vlhkost", "sensor", "Vlhkost", {"value_template": "{{ value_json.humidity }}", "unit_of_measurement": "%",
                                      "device_class": "humidity", "state_class": "measurement"}, True),
    ("baterie", "sensor", "Baterie", {"value_template": "{{ value_json.battery_pct }}", "unit_of_measurement": "%",
                                      "device_class": "battery", "state_class": "measurement"}, True),
    ("baterie_slaba", "binary_sensor", "Slabá baterie", {
        "value_template": "{{ 'ON' if value_json.battery_low else 'OFF' }}", "device_class": "battery"}, False),
    ("spotreba_dnes", "sensor", "Spotřeba dnes", {
        "value_template": "{{ value_json.consumption_today }}", "unit_of_measurement": "L",
        "device_class": "water", "state_class": "total_increasing"}, False),
    ("dest", "sensor", "Očekávaný déšť (2 dny)", {
        "value_template": "{{ value_json.expected_rain_mm }}", "unit_of_measurement": "mm",
        "device_class": "precipitation", "icon": "mdi:weather-rainy"}, False),
    ("stav", "sensor", "Stav čidla", {"value_template": "{{ value_json.status }}", "device_class": "enum",
                                      "options": ["online", "stale", "offline"], "entity_category": "diagnostic",
                                      "icon": "mdi:access-point"}, False),
    ("naposledy", "sensor", "Poslední měření", {"value_template": "{{ value_json.last_seen }}",
                                                "device_class": "timestamp", "entity_category": "diagnostic"}, False),
    ("napeti", "sensor", "Napětí baterie", {"value_template": "{{ value_json.battery_v }}", "unit_of_measurement": "V",
                                            "device_class": "voltage", "state_class": "measurement",
                                            "entity_category": "diagnostic"}, True),
    ("signal", "sensor", "Signál", {"value_template": "{{ value_json.rssi }}", "unit_of_measurement": "dBm",
                                    "device_class": "signal_strength", "state_class": "measurement",
                                    "entity_category": "diagnostic"}, True),
]
REFILL_ENTITIES = [
    ("dopousti", "binary_sensor", "Dopouštění", {
        "value_template": "{{ 'ON' if value_json.refilling else 'OFF' }}", "device_class": "running"}, False),
]
REFILL_BUTTONS = [
    ("dopustit", "Dopustit", "START", "mdi:water-plus"),
    ("zastavit", "Zastavit dopouštění", "STOP", "mdi:water-off"),
]


class MqttBridge:
    def __init__(self, core):
        self.core = core
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.connected = False
        self.error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None       # something changed / command / HA birth
        self._reconf: Optional[asyncio.Event] = None     # MQTT settings changed
        self._dirty = False
        self._rediscover = False
        self._writer: Optional[asyncio.StreamWriter] = None
        self._wlock: Optional[asyncio.Lock] = None
        self._last_rx = 0.0
        self._sent: dict[str, bytes] = {}                # retained topic -> payload (this session)
        self._retained: set[str] = set(self._load_retained())

    # ------------------------------------------------------------ settings
    @property
    def base(self) -> str:
        return f"retencka/{self.core.settings.get('mqtt_uid') or 'hub'}"

    def config(self) -> dict:
        s = self.core.settings
        return {"host": (s.get("mqtt_host") or "").strip(), "port": int(s.get("mqtt_port") or 1883),
                "username": s.get("mqtt_username") or "", "password": s.get("mqtt_password") or "",
                "tls": bool(s.get("mqtt_tls")), "control": bool(s.get("mqtt_control"))}

    @staticmethod
    def _conn_key(c: dict) -> tuple:
        return c["host"], c["port"], c["username"], c["password"], c["tls"]

    def status(self) -> dict:
        c = self.config()
        on = bool(c["host"])
        return {"configured": on, "connected": self.connected and on, "error": self.error if on else None,
                "base_topic": self.base, "discovery_prefix": DISCOVERY}

    # ----------------------------------------------------------- lifecycle
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._wake, self._reconf, self._wlock = asyncio.Event(), asyncio.Event(), asyncio.Lock()
        self._task = loop.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._goodbye()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    def kick(self) -> None:
        """Something changed (any thread): publish the states again soon."""
        self._dirty = True
        self._set_threadsafe(self._wake)

    def reconfigure(self) -> None:
        """MQTT settings saved on the web."""
        self._set_threadsafe(self._reconf)
        self._set_threadsafe(self._wake)

    def _set_threadsafe(self, ev: Optional[asyncio.Event]) -> None:
        loop = self.loop
        if ev is None or loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(ev.set)
        except RuntimeError:
            pass

    async def _run(self) -> None:
        backoff = 5.0
        while True:
            self._reconf.clear()
            cfg = self.config()
            if not cfg["host"]:
                self.connected, self.error = False, None
                await self._wait(self._reconf, 3600)
                continue
            try:
                await self._session(cfg)
                backoff = 5.0                        # settings changed: connect again at once
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:                   # whatever happens, the bridge survives
                prev, self.error = self.error, error_text(e)
                if self.error != prev or self.connected:
                    log.warning("MQTT: %s (%s: %s)", self.error, type(e).__name__, e)
                self.connected = False
            finally:
                await self._close()
            await self._wait(self._reconf, backoff)
            backoff = min(backoff * 2, 300.0)

    @staticmethod
    async def _wait(ev: asyncio.Event, timeout: float) -> None:
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def _session(self, cfg: dict) -> None:
        ctx = ssl.create_default_context() if cfg["tls"] else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(cfg["host"], cfg["port"], ssl=ctx), CONNECT_TIMEOUT_S)
        self._writer = writer
        base = self.base
        await self._send(connect_packet(f"retencka-{self.core.settings.get('mqtt_uid') or 'hub'}",
                                        cfg["username"], cfg["password"], f"{base}/status", b"offline"))
        first, body = await asyncio.wait_for(read_packet(reader), CONNECT_TIMEOUT_S)
        if first >> 4 != 2 or len(body) < 2:
            raise ValueError("no CONNACK")
        if body[1]:
            raise Refused(CONNACK_ERRORS.get(body[1], f"Broker odmítl spojení (kód {body[1]})"))
        self.connected, self.error = True, None
        self._last_rx = time.monotonic()
        log.info("MQTT connected to %s:%d", cfg["host"], cfg["port"])
        await self._send(publish_packet(f"{base}/status", b"online", True))
        await self._send(subscribe_packet(1, [f"{base}/+/refill/set", f"{DISCOVERY}/status"]))
        self._sent.clear()
        reader_task = asyncio.create_task(self._reader(reader))
        try:
            await self._publish_all()
            last_ping = last_full = time.monotonic()
            while True:
                now = time.monotonic()
                timeout = min(PING_S - (now - last_ping), REFRESH_S - (now - last_full))
                await self._wait(self._wake, max(0.05, timeout))
                self._wake.clear()
                if reader_task.done():
                    reader_task.result()             # raises the reader's error
                    raise ConnectionError("closed by the broker")
                if self._reconf.is_set():
                    if self._conn_key(self.config()) != self._conn_key(cfg):
                        off = not self.config()["host"]
                        if off:
                            await self._clear_all()  # MQTT switched off: entities disappear
                        await self._goodbye(status=not off)
                        return
                    self._reconf.clear()
                    self._dirty = True               # e.g. refill buttons on / off
                now = time.monotonic()
                if self._rediscover:
                    self._rediscover = False
                    self._sent.clear()               # Home Assistant restarted: send everything
                if self._dirty or not self._sent or now - last_full >= REFRESH_S:
                    self._dirty = False
                    await self._publish_all()
                    last_full = now
                if now - last_ping >= PING_S:
                    if now - self._last_rx > KEEPALIVE_S + PING_S:
                        raise asyncio.TimeoutError()
                    await self._send(PINGREQ)
                    last_ping = now
        finally:
            reader_task.cancel()

    async def _reader(self, reader: asyncio.StreamReader) -> None:
        while True:
            first, body = await read_packet(reader)
            self._last_rx = time.monotonic()
            if first >> 4 == 3:                      # PUBLISH
                topic, payload, qos, pid = parse_publish(first, body)
                if qos == 1 and pid is not None:
                    await self._send(_packet(0x40, struct.pack(">H", pid)))
                self._on_message(topic, payload)

    async def _send(self, data: bytes) -> None:
        w = self._writer
        if w is None:
            raise ConnectionError("not connected")
        async with self._wlock:
            w.write(data)
            await asyncio.wait_for(w.drain(), CONNECT_TIMEOUT_S)

    async def _goodbye(self, status: bool = True) -> None:
        """Clean disconnect (the broker then does not send the last will)."""
        if self._writer is not None and self.connected:
            try:
                if status:
                    await self._send(publish_packet(f"{self.base}/status", b"offline", True))
                await self._send(DISCONNECT)
            except Exception:
                pass
        await self._close()

    async def _close(self) -> None:
        w, self._writer = self._writer, None
        self.connected = False
        if w is not None:
            try:
                w.close()
                await asyncio.wait_for(w.wait_closed(), 2)
            except Exception:
                pass

    # ------------------------------------------------------------ messages
    def _on_message(self, topic: str, payload: bytes) -> None:
        if topic == f"{DISCOVERY}/status":
            if payload.strip().lower() == b"online":
                self._rediscover = True
                self._wake.set()
            return
        parts = topic.split("/")
        if len(parts) != 5 or "/".join(parts[:2]) != self.base or parts[3:] != ["refill", "set"]:
            return
        try:
            nid = int(parts[2], 16)
        except ValueError:
            return
        cmd = payload[:16].decode("ascii", "replace").strip().upper()
        self.core._spawn(self.command(nid, cmd))

    async def command(self, nid: int, cmd: str) -> Optional[tuple[bool, str]]:
        node = self.core.nodes.get(nid)
        if node is None or cmd not in ("START", "STOP"):
            return None
        if not self.config()["control"]:
            log.warning("MQTT: refill command for %04X ignored (control from Home Assistant is off)", nid)
            return None
        if cmd == "START":
            res = await self.core.refill_start(nid, float(node["auto_refill_target_pct"]), "manual",
                                               "Home Assistant", "mqtt")
        else:
            res = await self.core.refill_cancel(nid, "Zastaveno z Home Assistantu", "Home Assistant", "mqtt")
        log.info("MQTT %s %04X: %s", cmd, nid, res[1])
        self.kick()
        return res

    # ---------------------------------------------------------- publishing
    def _device(self, nid: int, node: dict) -> dict:
        host = (self.core.hostnames() or ["retencka.local"])[0]
        last = self.core.last.get(nid) or {}
        dev = {"identifiers": [f"{self._uid(nid)}"], "name": node["name"], "manufacturer": "Retenční nádrž",
               "model": "Čidlo hladiny 433 MHz", "serial_number": f"{nid:04X}",
               "configuration_url": f"http://{host}/"}
        if last.get("fw") is not None:
            dev["sw_version"] = f"firmware {last['fw']}"
        return dev

    def _uid(self, nid: int) -> str:
        return f"retencka_{self.core.settings.get('mqtt_uid') or 'hub'}_{nid:04x}"

    def discovery(self, nid: int, node: dict, refill: bool, control: bool) -> dict[str, dict]:
        """Discovery config topic -> payload for one tank."""
        uid, base = self._uid(nid), self.base
        node_t = f"{base}/{nid:04x}"
        dev = self._device(nid, node)
        origin = {"name": "Retenční nádrž", "sw_version": __version__, "support_url": REPO_URL}
        hub_av = [{"topic": f"{base}/status"}]
        out: dict[str, dict] = {}
        ents = SENSORS + (REFILL_ENTITIES if refill else [])
        for obj, comp, name, extra, sensor_av in ents:
            cfg = {"name": name, "unique_id": f"{uid}_{obj}",
                   "state_topic": f"{node_t}/state", **extra, "device": dev, "origin": origin,
                   "availability": hub_av + ([{"topic": f"{node_t}/availability"}] if sensor_av else []),
                   "availability_mode": "all"}
            out[f"{DISCOVERY}/{comp}/{uid}/{obj}/config"] = cfg
        if refill and control:
            for obj, name, press, icon in REFILL_BUTTONS:
                out[f"{DISCOVERY}/button/{uid}/{obj}/config"] = {
                    "name": name, "unique_id": f"{uid}_{obj}",
                    "command_topic": f"{node_t}/refill/set", "payload_press": press, "icon": icon,
                    "device": dev, "origin": origin, "availability": hub_av}
        return out

    async def node_state(self, nid: int) -> dict:
        st = await self.core.state(nid)
        b = st["battery"]
        return {
            "status": st["status"],
            "level_pct": st["water_level_percent"],
            "level_cm": st["water_level_cm"],
            "volume_l": st["volume_liters"],
            "max_l": st["max_liters"],
            "temperature": st["temperature"],
            "humidity": st["humidity"],
            "battery_pct": b["percent"],
            "battery_v": b["voltage"],
            "battery_low": bool(b["low"]),
            "battery_days_left": b["days_left"],
            "rssi": st["node"]["rssi"],
            "last_seen": _iso(st.get("timestamp")),
            "refilling": bool(st["refill"]["active"]),
            "consumption_today": st["consumption_today"],
            "expected_rain_mm": st["expected_rain_mm"],
        }

    async def wanted(self) -> dict[str, bytes]:
        """Every retained topic the hub should have on the broker now."""
        control = self.config()["control"]
        out: dict[str, bytes] = {}
        for node in self.core.nodes.all():
            nid = node["node_id"]
            refill = self.core._provider(node) is not None
            for t, cfg in self.discovery(nid, node, refill, control).items():
                out[t] = json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode("utf-8")
            st = await self.node_state(nid)
            out[f"{self.base}/{nid:04x}/state"] = json.dumps(st, ensure_ascii=False, sort_keys=True).encode("utf-8")
            out[f"{self.base}/{nid:04x}/availability"] = b"offline" if st["status"] == "offline" else b"online"
        return out

    async def _publish_all(self) -> None:
        want = await self.wanted()
        # discovery before the states, so Home Assistant has the entity when its value arrives
        for t in sorted(want, key=lambda x: not x.startswith(DISCOVERY + "/")):
            if self._sent.get(t) != want[t]:
                await self._send(publish_packet(t, want[t], True))
                self._sent[t] = want[t]
        gone = (self._retained | set(self._sent)) - set(want)
        for t in sorted(gone):                       # removed tank / valve: delete in Home Assistant
            await self._send(publish_packet(t, b"", True))
            self._sent.pop(t, None)
        self._remember(set(want))

    async def _clear_all(self) -> None:
        for t in sorted(self._retained | set(self._sent)):
            await self._send(publish_packet(t, b"", True))
        await self._send(publish_packet(f"{self.base}/status", b"", True))
        self._sent.clear()
        self._remember(set())
        log.info("MQTT switched off: entities removed from Home Assistant")

    # ------------------------------------------------- remembered topics
    def _path(self) -> str:
        return os.path.join(self.core.data_dir, RETAINED_FILE)

    def _load_retained(self) -> list[str]:
        try:
            with open(self._path(), encoding="utf-8") as f:
                v = json.load(f)
            return [t for t in v if isinstance(t, str)] if isinstance(v, list) else []
        except (OSError, ValueError):
            return []

    def _remember(self, topics: set[str]) -> None:
        if topics == self._retained:
            return
        self._retained = set(topics)
        tmp = self._path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sorted(topics), f)
            os.replace(tmp, self._path())
        except OSError:
            log.warning("cannot save %s", RETAINED_FILE)

