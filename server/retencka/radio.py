"""CC1101 receiver on SPI0 with self-healing.

Register set MUST match uzel/src/cc1101.c (variable packet length, max 61 B).
The radio returns to IDLE after each packet (MCSM1.RXOFF_MODE = IDLE); the worker
reads the FIFO and re-enters RX. Frames are handed to the core undecoded (the core
owns the keys). PAIR_REQ frames are answered immediately from this thread – the
node listens for the PAIR_ACK only ~0.6 s after its request.

Packet detection:
  * GDO0 falling edge via libgpiod (v1 or v2 Python API) when available,
  * otherwise polling RXBYTES every 200 ms (≈ 5 SPI transfers/s, negligible CPU).

Self-healing:
  * every 30 s MARCSTATE + read-back of key registers; mismatch => full reset,
  * SPI exceptions => close and reopen spidev (after back-off),
  * no packet for 3 node intervals => preventive reset.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Optional

from . import crypto
from .model import NODE_INTERVAL_S

log = logging.getLogger("retencka.radio")

SRES, SCAL, STX, SRX, SIDLE, SFRX, SFTX = 0x30, 0x33, 0x35, 0x34, 0x36, 0x3A, 0x3B
VERSION, MARCSTATE, RXBYTES = 0xF1, 0xF5, 0xFB
PATABLE, FIFO = 0x3E, 0x3F
MARC_IDLE, MARC_RX, MARC_RXOVF = 0x01, 0x0D, 0x11

CONFIG: list[tuple[int, int]] = [
    (0x00, 0x2E),  # IOCFG2 hi-Z
    (0x02, 0x06),  # IOCFG0 sync / end of packet
    (0x03, 0x47),  # FIFOTHR
    (0x04, 0xD3),  # SYNC1
    (0x05, 0x91),  # SYNC0
    (0x06, 61),    # PKTLEN = max accepted length
    (0x07, 0x04),  # PKTCTRL1 append status
    (0x08, 0x45),  # PKTCTRL0 whitening, CRC, variable length
    (0x09, 0x00),  # ADDR
    (0x0A, 0x00),  # CHANNR
    (0x0B, 0x06),  # FSCTRL1
    (0x0C, 0x00),  # FSCTRL0
    (0x0D, 0x10),  # FREQ2  433.92 MHz
    (0x0E, 0xB0),  # FREQ1
    (0x0F, 0x71),  # FREQ0
    (0x10, 0xC7),  # MDMCFG4
    (0x11, 0x83),  # MDMCFG3
    (0x12, 0x03),  # MDMCFG2
    (0x13, 0x22),  # MDMCFG1
    (0x14, 0xF8),  # MDMCFG0
    (0x15, 0x40),  # DEVIATN
    (0x17, 0x30),  # MCSM1  RX -> IDLE, TX -> IDLE
    (0x18, 0x18),  # MCSM0  autocal IDLE->RX
    (0x19, 0x16),  # FOCCFG
    (0x1A, 0x6C),  # BSCFG
    (0x1B, 0x43),  # AGCCTRL2
    (0x1C, 0x40),  # AGCCTRL1
    (0x1D, 0x91),  # AGCCTRL0
    (0x21, 0x56),  # FREND1
    (0x22, 0x10),  # FREND0
    (0x23, 0xE9),  # FSCAL3
    (0x24, 0x2A),  # FSCAL2
    (0x25, 0x00),  # FSCAL1
    (0x26, 0x1F),  # FSCAL0
    (0x2C, 0x81),  # TEST2
    (0x2D, 0x35),  # TEST1
    (0x2E, 0x09),  # TEST0
]
VERIFY_REGS = [0x02, 0x06, 0x08, 0x0D, 0x0E, 0x0F, 0x10, 0x12, 0x17]
CHECK_PERIOD_S = 30.0
SILENCE_RESET_S = 3 * NODE_INTERVAL_S + 60


class RadioError(Exception):
    pass


class Receiver:
    def __init__(self, on_frame: Callable[[bytes, float, int], None],
                 responder: Optional[Callable[[bytes], Optional[bytes]]] = None,
                 bus: int = 0, dev: int = 0,
                 speed_hz: int = 5_000_000, gdo0_gpio: int = 25, mock: bool = False):
        self.on_frame = on_frame            # (payload, rssi_dbm, lqi) – called in this thread
        self.responder = responder          # payload -> reply frame or None (pairing)
        self.bus, self.dev, self.speed = bus, dev, speed_hz
        self.gdo0_gpio = gdo0_gpio
        self.mock = mock

        self.spi = None
        self._gpio = None          # (kind, handle)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # diagnostics (read from other threads – plain attribute reads are atomic)
        self.heartbeat = time.monotonic()
        self.hardware_ok = False
        self.chip_version: Optional[int] = None
        self.resets = 0
        self.crc_errors = 0
        self.foreign_packets = 0
        self.auth_failures = 0
        self.replays = 0
        self.duplicates = 0                 # second copies of important packets (dropped)
        self.last_rx = time.monotonic()
        self.mode = "mock" if mock else "init"

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="radio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._close()

    def alive(self, max_age: float = 90.0) -> bool:
        return (time.monotonic() - self.heartbeat) < max_age

    # ------------------------------------------------------------ SPI helpers
    def _w(self, reg: int, val: int) -> None:
        self.spi.xfer2([reg & 0x3F, val & 0xFF])

    def _r(self, reg: int) -> int:
        # status registers (0x30-0x3D) need the burst bit, config registers not
        addr = reg if reg >= 0xC0 else (reg & 0x3F) | 0x80
        return self.spi.xfer2([addr, 0])[1]

    def _strobe(self, s: int) -> int:
        return self.spi.xfer2([s])[0]

    def _read_fifo(self, n: int) -> list[int]:
        return self.spi.xfer2([FIFO | 0xC0] + [0] * n)[1:]

    # ---------------------------------------------------------------- setup
    def _open(self) -> None:
        import spidev  # python3-spidev
        self._close()
        spi = spidev.SpiDev()
        spi.open(self.bus, self.dev)
        spi.max_speed_hz = self.speed
        spi.mode = 0
        self.spi = spi
        self._open_gpio()

    def _open_gpio(self) -> None:
        self._gpio = None
        try:
            import gpiod  # python3-libgpiod
        except ImportError:
            log.info("gpiod not available – polling RXBYTES")
            return
        try:
            if hasattr(gpiod, "request_lines"):          # libgpiod v2
                from gpiod.line import Edge
                req = gpiod.request_lines(
                    "/dev/gpiochip0", consumer="retencka",
                    config={self.gdo0_gpio: gpiod.LineSettings(edge_detection=Edge.FALLING)})
                self._gpio = ("v2", req)
            else:                                        # libgpiod v1
                chip = gpiod.Chip("gpiochip0")
                line = chip.get_line(self.gdo0_gpio)
                line.request(consumer="retencka", type=gpiod.LINE_REQ_EV_FALLING_EDGE)
                self._gpio = ("v1", line)
            log.info("GDO0 edge detection on GPIO%d (%s)", self.gdo0_gpio, self._gpio[0])
        except Exception as e:  # pragma: no cover – hardware specific
            log.warning("GDO0 edge detection unavailable (%s) – polling", e)
            self._gpio = None

    def _close(self) -> None:
        if self._gpio:
            try:
                self._gpio[1].release()
            except Exception:
                pass
            self._gpio = None
        if self.spi:
            try:
                self.spi.close()
            except Exception:
                pass
            self.spi = None

    def _configure(self) -> None:
        self._strobe(SRES)
        time.sleep(0.005)
        ver = self._r(VERSION)
        if ver in (0x00, 0xFF):
            raise RadioError(f"CC1101 not detected (VERSION=0x{ver:02X})")
        self.chip_version = ver
        for reg, val in CONFIG:
            self._w(reg, val)
        self.spi.xfer2([PATABLE | 0x40, 0xC0])
        if not self._verify():
            raise RadioError("register read-back mismatch after configuration")
        self._enter_rx()
        self.resets += 1
        self.hardware_ok = True
        self.mode = "gdo0" if self._gpio else "polling"
        log.info("CC1101 v0x%02X configured, listening (%s)", ver, self.mode)

    def _verify(self) -> bool:
        cfg = dict(CONFIG)
        return all(self._r(r) == cfg[r] for r in VERIFY_REGS)

    def _enter_rx(self) -> None:
        self._strobe(SIDLE)
        self._strobe(SFRX)
        self._strobe(SRX)

    # ------------------------------------------------------------------ RX
    def _wait_event(self, timeout: float) -> None:
        if not self._gpio:
            self._stop.wait(min(timeout, 0.2))
            return
        kind, h = self._gpio
        try:
            if kind == "v2":
                if h.wait_edge_events(timeout):
                    h.read_edge_events()
            else:
                if h.event_wait(sec=int(timeout), nsec=int((timeout % 1) * 1e9)):
                    h.event_read()
        except Exception as e:  # pragma: no cover
            log.warning("GPIO wait failed (%s) – switching to polling", e)
            self._gpio = None
            self.mode = "polling"

    def _poll_fifo(self) -> None:
        # MCSM1.RXOFF_MODE = IDLE: a finished packet leaves the chip in IDLE. In RX
        # (or a transient state) a packet may be arriving right now – reading or
        # flushing the FIFO then would destroy it, so wait for the end of packet.
        state = self._r(MARCSTATE) & 0x1F
        if state == MARC_RXOVF:
            log.warning("RX FIFO overflow – flushing")
            self._enter_rx()
            return
        if state != MARC_IDLE:
            return
        n = self._r(RXBYTES)
        overflow = bool(n & 0x80)
        n &= 0x7F
        if overflow:
            log.warning("RX FIFO overflow – flushing")
            self._enter_rx()
            return
        if n < 3:
            self._enter_rx()                      # IDLE without a packet: back to listening
            return
        length = self._read_fifo(1)[0]
        if length == 0 or length > 61 or n < length + 3:
            self._enter_rx()                      # garbage / incomplete
            return
        raw = self._read_fifo(length + 2)
        payload, rssi_raw, lqi_raw = bytes(raw[:length]), raw[length], raw[length + 1]
        if not lqi_raw & 0x80:
            self.crc_errors += 1
            self._enter_rx()
            return
        rssi = round((rssi_raw - 256 if rssi_raw >= 128 else rssi_raw) / 2.0 - 74.0, 1)
        # answer a pairing request right away (node listens only briefly)
        reply = None
        if self.responder and payload[:1] == bytes([crypto.PKT_PAIR_REQ]):
            try:
                reply = self.responder(payload)
            except Exception:
                log.exception("pairing responder failed")
        if reply:
            self._transmit(reply)
        self._enter_rx()
        self.last_rx = time.monotonic()
        try:
            self.on_frame(payload, rssi, lqi_raw & 0x7F)
        except Exception:
            log.exception("frame handler failed")

    def _transmit(self, frame: bytes) -> None:
        self._strobe(SIDLE)
        self._strobe(SFTX)
        self.spi.xfer2([0x3F | 0x40, len(frame)] + list(frame))   # burst: length + payload
        self._strobe(STX)
        end = time.monotonic() + 0.4
        while time.monotonic() < end:                             # wait until back in IDLE
            if (self._r(MARCSTATE) & 0x1F) == MARC_IDLE:
                return
            time.sleep(0.002)
        log.warning("TX did not finish in time")

    def _health_check(self) -> None:
        state = self._r(MARCSTATE) & 0x1F
        if not self._verify():
            raise RadioError("register mismatch (chip reset or brown-out?)")
        if state == MARC_RXOVF:
            self._enter_rx()
        elif state == MARC_IDLE and (self._r(RXBYTES) & 0x7F) == 0:
            self._strobe(SRX)
        if time.monotonic() - self.last_rx > SILENCE_RESET_S:
            self.last_rx = time.monotonic()
            raise RadioError("no packet for a long time – preventive reset")

    def _run(self) -> None:
        if self.mock:
            self._run_mock()
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._open()
                self._configure()
                backoff = 1.0
                next_check = time.monotonic() + CHECK_PERIOD_S
                while not self._stop.is_set():
                    self.heartbeat = time.monotonic()
                    self._wait_event(1.0)
                    self._poll_fifo()
                    if time.monotonic() >= next_check:
                        self._health_check()
                        next_check = time.monotonic() + CHECK_PERIOD_S
            except RadioError as e:
                log.warning("Radio: %s – resetting", e)
                self.hardware_ok = False
                self._sleep_heartbeat(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception as e:
                log.error("Radio failure: %s – reopening in %.0f s", e, backoff)
                self.hardware_ok = False
                self.mode = "error"
                self._close()
                self._sleep_heartbeat(backoff)
                backoff = min(backoff * 2, 60.0)

    def _sleep_heartbeat(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < end:
            self.heartbeat = time.monotonic()
            self._stop.wait(1.0)

    # ----------------------------------------------------------------- mock
    def _run_mock(self, period: float = 20.0) -> None:
        """Synthetic encrypted node for development without hardware (RETENCKA_MOCK=1).

        Behaves like real firmware: offers pairing (PAIR_REQ) until the hub answers,
        then sends encrypted DATA frames."""
        log.warning("Radio in MOCK mode – synthetic node 0xBEEF")
        self.hardware_ok = True
        priv = bytes(range(1, 33))
        pub = crypto.x25519_base(priv)
        keys = None
        ctr, t0 = 0, time.time()
        while not self._stop.is_set():
            self.heartbeat = time.monotonic()
            self.last_rx = time.monotonic()
            req = crypto.build_pair_req(0xBEEF, pub, False)
            if keys is None:
                reply = self.responder(req) if self.responder else None
                self.on_frame(req, -70.0, 20)
                if reply and len(reply) == crypto.PAIR_LEN and reply[0] == crypto.PKT_PAIR_ACK:
                    keys = crypto.derive_keys(crypto.x25519(priv, reply[3:35]))
                    log.info("MOCK node paired")
                    continue
                self._sleep_heartbeat(5)
                continue
            ph = (time.time() - t0) / 3600.0
            tel = crypto.build_telemetry(0x20 if ctr == 0 else 0, 4950, int(1200 + 300 * math.sin(ph / 3)),
                                         int(8500 + 800 * math.cos(ph / 2)), int((700 + 400 * math.sin(ph)) * 10),
                                         diag=0x10 if ctr % 12 == 0 else 0, sag=9)
            frame = crypto.build_data(keys[0], keys[1], 0xBEEF, ctr, tel)
            ctr += 1
            try:
                self.on_frame(frame, -71.5, 20)
            except Exception:
                log.exception("frame handler failed")
            self._sleep_heartbeat(period)
