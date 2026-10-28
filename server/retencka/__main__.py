"""Entry point: python3 -m retencka

Environment (systemd EnvironmentFile=/etc/default/retencka):
  RETENCKA_DATA      data directory           (/var/lib/retencka)
  RETENCKA_STATIC    static files directory   (<package>/../static)
  RETENCKA_WEB_PORT  web UI port              (80)
  RETENCKA_BIND      bind address of the web (0.0.0.0)
  RETENCKA_SPI_BUS / RETENCKA_SPI_DEV / RETENCKA_SPI_HZ / RETENCKA_GDO0_GPIO
  RETENCKA_MOCK=1    synthetic radio for development
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import secrets
import shutil
import signal
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from . import __version__, reset, updates
from .core import Core
from .db import Database, SchemaTooNewError
from .radio import Receiver
from .settings import Settings
from .telegram import TelegramBot
from .httpserver import Server
from .mqtt import MqttBridge
from .web import build_web_app

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


def _try_restore_latest_backup(path: str) -> Optional[Database]:
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "backup")
    if not os.path.isdir(backup_dir):
        return None
    candidates = sorted(
        (f for f in os.listdir(backup_dir) if f.startswith("retencka-") and f.endswith(".db.gz")),
        reverse=True,
    )
    for name in candidates:
        gz_path = os.path.join(backup_dir, name)
        try:
            with gzip.open(gz_path, "rb") as fi, open(path, "wb") as fo:
                shutil.copyfileobj(fi, fo, 65536)
            db = Database(path)
            if db.integrity_ok():
                log.warning("Restored corrupted database from nightly backup %s", name)
                return db
            db.close_thread()
        except Exception as e:
            log.warning("Could not restore nightly backup %s: %s", name, e)
        for suffix in ("", "-wal", "-shm"):
            try:
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)
            except OSError:
                pass
    return None


def open_database(path: str) -> Database:
    """Open the DB; a corrupted file is moved aside and restored from the newest
    nightly backup (if available) so the device keeps working."""
    try:
        db = Database(path)
        if db.integrity_ok():
            return db
        db.close_thread()
        raise RuntimeError("integrity check failed")
    except SchemaTooNewError:
        raise
    except Exception as e:
        if not os.path.exists(path):
            raise
        bad = f"{path}.corrupt-{int(time.time())}"
        log.error("Database unusable (%s) – moving to %s", e, bad)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.replace(path + suffix, bad + suffix)
        restored = _try_restore_latest_backup(path)
        if restored is not None:
            return restored
        return Database(path)


def write_health(data_dir: str, radio_ok: bool) -> None:
    try:
        updates.write_health(data_dir, __version__, radio_ok)
    except OSError:
        log.exception("cannot write health.json")


async def main_async() -> None:
    data_dir = env("RETENCKA_DATA", "/var/lib/retencka")
    static_dir = env("RETENCKA_STATIC", str(Path(__file__).resolve().parent.parent / "static"))
    os.makedirs(data_dir, exist_ok=True)
    was_reset = reset.apply(data_dir)
    ensure_setup_code(data_dir)

    db = open_database(os.path.join(data_dir, "retencka.db"))
    settings = Settings(db)
    core = Core(db, settings, data_dir)
    if was_reset:
        core.audit("FACTORY_RESET", f"Obnoveno tovární nastavení (požádal {was_reset['user']})")
        try:
            core.control_request("servis")         # the root helper switches remote access off
        except OSError:
            pass
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

    mqtt = MqttBridge(core)
    core.mqtt = mqtt
    mqtt.start(core.loop)

    bind = env("RETENCKA_BIND", "0.0.0.0")
    web_port = int(env("RETENCKA_WEB_PORT", "80"))
    web = Server(build_web_app(core, static_dir))
    await web.start(bind, web_port)
    runners = [web]
    radio_ok = radio.alive()
    write_health(data_dir, radio_ok)
    log.info("Retencka %s: web :%d, data %s", __version__, web_port, data_dir)

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
            if radio.alive() != radio_ok:
                radio_ok = radio.alive()
                write_health(data_dir, radio_ok)
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
    await mqtt.stop()
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
