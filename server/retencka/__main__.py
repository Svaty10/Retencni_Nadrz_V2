"""Entry point: python3 -m retencka

Environment (systemd EnvironmentFile=/etc/default/retencka):
  RETENCKA_DATA      data directory           (/var/lib/retencka)
  RETENCKA_STATIC    static files directory   (<package>/../static)
  RETENCKA_WEB_PORT  web UI port              (80)
  RETENCKA_HA_PORT   Home Assistant API port  (8081)
  RETENCKA_BIND      bind address             (0.0.0.0)
  RETENCKA_SPI_BUS / RETENCKA_SPI_DEV / RETENCKA_SPI_HZ / RETENCKA_GDO0_GPIO
  RETENCKA_MOCK=1    synthetic radio for development
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .core import Core
from .db import Database
from .radio import Receiver
from .settings import Settings
from .telegram import TelegramBot
from .httpserver import Server
from .web import build_ha_app, build_web_app

log = logging.getLogger("retencka")


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# ------------------------------------------------------------- systemd notify
def sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


def ensure_setup_code(data_dir: str) -> None:
    path = Path(data_dir, "setup_code")
    if not path.exists():
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        path.write_text(code[:4] + "-" + code[4:] + "\n")
        os.chmod(path, 0o600)
    log.info("Setup code file: %s", path)


def open_database(path: str) -> Database:
    """Open the DB; a corrupted file is moved aside so the device keeps working."""
    try:
        db = Database(path)
        if db.integrity_ok():
            return db
        db.close_thread()
        raise RuntimeError("integrity check failed")
    except Exception as e:
        if not os.path.exists(path):
            raise
        bad = f"{path}.corrupt-{int(time.time())}"
        log.error("Database unusable (%s) – moving to %s and starting fresh", e, bad)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.replace(path + suffix, bad + suffix)
        return Database(path)


async def main_async() -> None:
    data_dir = env("RETENCKA_DATA", "/var/lib/retencka")
    static_dir = env("RETENCKA_STATIC", str(Path(__file__).resolve().parent.parent / "static"))
    os.makedirs(data_dir, exist_ok=True)
    ensure_setup_code(data_dir)

    db = open_database(os.path.join(data_dir, "retencka.db"))
    settings = Settings(db)
    core = Core(db, settings, data_dir)
    core.loop = asyncio.get_running_loop()
    # few worker threads: each one costs stack + malloc arena on a 512 MB device
    core.loop.set_default_executor(ThreadPoolExecutor(max_workers=3, thread_name_prefix="worker"))

    radio = Receiver(core.on_frame, core.responder,
                     bus=int(env("RETENCKA_SPI_BUS", "0")), dev=int(env("RETENCKA_SPI_DEV", "0")),
                     speed_hz=int(env("RETENCKA_SPI_HZ", "5000000")),
                     gdo0_gpio=int(env("RETENCKA_GDO0_GPIO", "25")),
                     mock=env("RETENCKA_MOCK", "0") == "1")
    core.radio = radio
    radio.start()

    bot = TelegramBot(core)
    core.notifier = bot.notify
    core.telegram = bot
    bot.start(core.loop)

    bind = env("RETENCKA_BIND", "0.0.0.0")
    web_port, ha_port = int(env("RETENCKA_WEB_PORT", "80")), int(env("RETENCKA_HA_PORT", "8081"))
    web_app = build_web_app(core, static_dir)
    web_app["ha_port"] = ha_port
    ha_app = build_ha_app(core)

    runners = []
    for app, port in ((web_app, web_port), (ha_app, ha_port)):
        srv = Server(app)
        await srv.start(bind, port)
        runners.append(srv)
    log.info("Retencka %s: web :%d, Home Assistant :%d, data %s", __version__, web_port, ha_port, data_dir)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # Windows (development only): Ctrl+C ends asyncio.run
            pass

    sd_notify("READY=1\nSTATUS=running")
    wd_usec = int(os.environ.get("WATCHDOG_USEC", "0"))
    wd_period = max(1.0, wd_usec / 2e6) if wd_usec else 10.0

    async def periodic():
        next_hour = next_day = 0.0
        next_refill = next_watch = 0.0
        core.aggregate_hours(since=int(time.time()) - 48 * 3600)
        while not stop.is_set():
            now = time.time()
            # systemd watchdog: only while the loop AND the radio thread are alive
            if radio.alive():
                sd_notify("WATCHDOG=1")
            else:
                log.error("Radio thread stalled – withholding watchdog ping")
            try:
                if now >= next_refill:
                    next_refill = now + 30
                    await core.refill_tick()
                    core.time_tick()
                if now >= next_watch:
                    next_watch = now + 60
                    core.watch_tick()
                if now >= next_hour:
                    next_hour = now + 3600 - now % 3600 + 60
                    await asyncio.to_thread(core.aggregate_hours)
                if now >= next_day:
                    next_day = now + 86400
                    await asyncio.to_thread(core.maintenance)
            except Exception:
                log.exception("periodic task failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=wd_period)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(periodic())
    await stop.wait()
    log.info("Shutting down")
    sd_notify("STOPPING=1")
    task.cancel()
    bot.stop()
    radio.stop()
    for r in runners:
        await r.stop()
    await core.http.close()
    db.close_thread()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("RETENCKA_LOGLEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s" if os.environ.get("INVOCATION_ID")
        else "%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout)
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
