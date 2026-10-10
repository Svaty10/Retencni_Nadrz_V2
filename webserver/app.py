"""
Lightweight FastAPI server for Raspberry Pi 3 B+ monitoring retention tank telemetry.
Serves Home Assistant REST sensor endpoints and a dynamic responsive dashboard.
Engineered for < 35 MB RAM footprint.
"""

import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cc1101_receiver import CC1101Receiver

# Base directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "history.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# Load configuration
def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "tank_area_m2": 10.0,
        "max_depth_cm": 150.0,
        "battery_critical_v": 3.00,
        "stale_timeout_sec": 1800,
        "max_db_records": 10000,
        "server": {"host": "0.0.0.0", "port": 8080},
        "hardware": {"spi_bus": 0, "spi_device": 0, "spi_speed_hz": 5000000, "gdo0_gpio": 25}
    }

config = load_config()

# SQLite storage helper
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                water_level_cm REAL NOT NULL,
                water_level_percent REAL NOT NULL,
                temperature REAL NOT NULL,
                humidity REAL NOT NULL,
                battery_voltage REAL NOT NULL,
                battery_low INTEGER NOT NULL,
                rssi REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry (timestamp)")
        conn.commit()

init_db()

# Thread-safe in-memory cache
class TelemetryCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_packet: Optional[dict] = None
        self.last_update_epoch: float = 0.0
        self.packets_total: int = 0
        self.start_epoch: float = time.time()

    def update(self, packet_data: dict, cfg: dict):
        with self.lock:
            now_epoch = time.time()
            iso_ts = datetime.now(timezone.utc).isoformat()

            level_mm = packet_data.get("level_mm", 0)
            vbat_mv = packet_data.get("vbat_raw_mv", 0)
            temp_x100 = packet_data.get("temp_x100", 0)
            hum_x100 = packet_data.get("hum_x100", 0)

            # Conversions
            water_level_cm = round(level_mm / 10.0, 1)
            max_depth_cm = float(cfg.get("max_depth_cm", 150.0))
            water_level_pct = round(max(0.0, min(100.0, (water_level_cm / max_depth_cm) * 100.0)), 1)
            temperature = round(temp_x100 / 100.0, 2)
            humidity = round(max(0.0, min(100.0, hum_x100 / 100.0)), 1)
            battery_voltage = round(vbat_mv / 1000.0, 3)

            crit_v = float(cfg.get("battery_critical_v", 3.00))
            battery_low = (battery_voltage < crit_v)

            # Liters in tank: Volume = Area (m2) * Height (m) * 1000 L/m3
            tank_area_m2 = float(cfg.get("tank_area_m2", 10.0))
            current_liters = round((tank_area_m2 * (water_level_cm / 100.0)) * 1000.0)

            self.last_packet = {
                "water_level_cm": water_level_cm,
                "water_level_percent": water_level_pct,
                "temperature": temperature,
                "humidity": humidity,
                "battery_voltage": battery_voltage,
                "battery_low": battery_low,
                "current_liters": current_liters,
                "rssi_dbm": packet_data.get("rssi_dbm", None),
                "timestamp": iso_ts
            }
            self.last_update_epoch = now_epoch
            self.packets_total += 1

            # Persist into SQLite
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("""
                        INSERT INTO telemetry (
                            timestamp, water_level_cm, water_level_percent,
                            temperature, humidity, battery_voltage, battery_low, rssi
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        iso_ts, water_level_cm, water_level_pct,
                        temperature, humidity, battery_voltage,
                        1 if battery_low else 0, packet_data.get("rssi_dbm")
                    ))
                    # Enforce max record limit (e.g. 10 000 rows)
                    max_records = int(cfg.get("max_db_records", 10000))
                    conn.execute(f"""
                        DELETE FROM telemetry WHERE id NOT IN (
                            SELECT id FROM telemetry ORDER BY id DESC LIMIT {max_records}
                        )
                    """)
                    conn.commit()
            except Exception as e:
                pass

    def get_state(self, stale_timeout_sec: int) -> dict:
        with self.lock:
            now = time.time()
            if self.last_packet is None:
                return {
                    "water_level_cm": 0.0,
                    "water_level_percent": 0.0,
                    "temperature": 0.0,
                    "humidity": 0.0,
                    "battery_voltage": 0.0,
                    "battery_low": False,
                    "current_liters": 0,
                    "last_seen_seconds_ago": 999999,
                    "status": "offline",
                    "timestamp": None
                }

            seconds_ago = int(now - self.last_update_epoch)
            status = "online" if seconds_ago <= stale_timeout_sec else "stale"

            return {
                "water_level_cm": self.last_packet["water_level_cm"],
                "water_level_percent": self.last_packet["water_level_percent"],
                "temperature": self.last_packet["temperature"],
                "humidity": self.last_packet["humidity"],
                "battery_voltage": self.last_packet["battery_voltage"],
                "battery_low": self.last_packet["battery_low"],
                "current_liters": self.last_packet["current_liters"],
                "last_seen_seconds_ago": seconds_ago,
                "status": status,
                "timestamp": self.last_packet["timestamp"]
            }

cache = TelemetryCache()

# Receiver callback
def on_rf_packet_received(pkt: dict):
    cache.update(pkt, config)

# Initialize hardware receiver
hw_cfg = config.get("hardware", {})
receiver = CC1101Receiver(
    spi_bus=hw_cfg.get("spi_bus", 0),
    spi_device=hw_cfg.get("spi_device", 0),
    spi_speed_hz=hw_cfg.get("spi_speed_hz", 5000000),
    gdo0_pin=hw_cfg.get("gdo0_gpio", 25),
    on_packet_callback=on_rf_packet_received
)

# FastAPI application
app = FastAPI(title="Retention Tank Telemetry Server", version="2.0.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

@app.on_event("startup")
def startup_event():
    receiver.start()

@app.on_event("shutdown")
def shutdown_event():
    receiver.stop()

# ------------------------------------------------------------------------------
# API Endpoints
# ------------------------------------------------------------------------------

@app.get("/api/v1/water/state")
def get_water_state():
    """
    Primary endpoint for Home Assistant REST sensor integration.
    Returns current tank levels, sensor data, and connection health status.
    """
    stale_sec = config.get("stale_timeout_sec", 1800)
    return cache.get_state(stale_sec)

@app.get("/api/v1/water/history")
def get_water_history(hours: int = Query(default=24, ge=1, le=720)):
    """
    Returns time-series history of telemetry for frontend charts.
    """
    limit_time = datetime.fromtimestamp(time.time() - (hours * 3600), timezone.utc).isoformat()

    results = []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT timestamp, water_level_cm, water_level_percent, temperature, humidity, battery_voltage
            FROM telemetry
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
        """, (limit_time,))
        for row in cur.fetchall():
            results.append({
                "timestamp": row["timestamp"],
                "water_level_cm": row["water_level_cm"],
                "water_level_percent": row["water_level_percent"],
                "temperature": row["temperature"],
                "humidity": row["humidity"],
                "battery_voltage": row["battery_voltage"]
            })

    return results

@app.get("/health")
def healthcheck():
    """
    Liveness probe for systemd watchdog and container health monitoring.
    """
    uptime = int(time.time() - cache.start_epoch)
    return {
        "status": "ok",
        "uptime_seconds": uptime,
        "packets_received": cache.packets_total,
        "rf_hardware_ready": receiver.is_hardware_available
    }

@app.post("/api/v1/water/simulate")
def simulate_packet(level_mm: int = 1450, vbat_mv: int = 4450, temp_x100: int = 2150, hum_x100: int = 6540):
    """
    Development endpoint to inject a simulated packet without physical CC1101 RF.
    """
    sim_pkt = {
        "level_mm": level_mm,
        "vbat_raw_mv": vbat_mv,
        "temp_x100": temp_x100,
        "hum_x100": hum_x100,
        "rssi_dbm": -68.5,
        "lqi": 30
    }
    cache.update(sim_pkt, config)
    return {"status": "injected", "data": sim_pkt}

@app.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request):
    """
    Clean, responsive, low-overhead single-page web dashboard.
    """
    stale_sec = config.get("stale_timeout_sec", 1800)
    current_state = cache.get_state(stale_sec)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "state": current_state,
        "config": config
    })

if __name__ == "__main__":
    import uvicorn
    server_cfg = config.get("server", {})
    uvicorn.run(
        "app:app",
        host=server_cfg.get("host", "0.0.0.0"),
        port=server_cfg.get("port", 8080),
        workers=1,
        access_log=False
    )
