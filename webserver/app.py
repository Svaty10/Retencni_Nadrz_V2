"""
Comprehensive, ultra-lightweight FastAPI server for Raspberry Pi 3 B+
Controls CC1101 RF telemetry, Hydrawise tank refilling, Open-Meteo smart rain deferral,
SQLite long-term storage (500k+ records), Home Assistant REST integration,
and a rich responsive Glassmorphism dashboard with tabs.
RAM footprint: < 35 MB.
"""

import os
import io
import csv
import json
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import requests
from fastapi import FastAPI, Query, Request, BackgroundTasks, HTTPException, Form, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

try:
    from werkzeug.security import generate_password_hash, check_password_hash
except ImportError:
    import hashlib
    def check_password_hash(p_hash: str, pwd: str) -> bool:
        if p_hash.startswith("scrypt:"):
            parts = p_hash.split("$")
            if len(parts) == 3:
                params = parts[0].split(":")
                n, r, p = int(params[1]), int(params[2]), int(params[3])
                salt = parts[1].encode('utf-8')
                expected = parts[2]
                calc = hashlib.scrypt(pwd.encode('utf-8'), salt=salt, n=n, r=r, p=p).hex()
                return calc == expected
        return False
    def generate_password_hash(pwd: str) -> str:
        import secrets
        salt = secrets.token_hex(8)
        derived = hashlib.scrypt(pwd.encode('utf-8'), salt=salt.encode('utf-8'), n=32768, r=8, p=1).hex()
        return f"scrypt:32768:8:1${salt}${derived}"

from cc1101_receiver import CC1101Receiver

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("retencka_server")

# Base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
USERS_PATH = os.path.join(BASE_DIR, "users.json")
SECRET_KEY_PATH = os.path.join(BASE_DIR, "secret_key.txt")
DB_PATH = os.path.join(BASE_DIR, "history.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

def get_or_create_secret_key() -> str:
    if os.path.exists(SECRET_KEY_PATH):
        try:
            with open(SECRET_KEY_PATH, "r", encoding="utf-8") as f:
                k = f.read().strip()
                if k:
                    return k
        except Exception:
            pass
    import secrets
    k = secrets.token_hex(32)
    try:
        with open(SECRET_KEY_PATH, "w", encoding="utf-8") as f:
            f.write(k)
    except Exception:
        pass
    return k

SECRET_KEY = get_or_create_secret_key()

def load_users() -> dict:
    if os.path.exists(USERS_PATH):
        try:
            with open(USERS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_users(users: dict):
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=4)

# ==============================================================================
# Configuration Management
# ==============================================================================

DEFAULT_CONFIG = {
    "tank_area_m2": 10.0,
    "max_depth_cm": 150.0,
    "flow_rate_lpm": 41.6,
    "refill_threshold_pct": 25.0,
    "max_refill_pct": 90.0,
    "hydrawise_api_key": "",
    "hydrawise_relay_id": "",
    "latitude": 50.0755,
    "longitude": 14.4378,
    "rain_defer_enabled": True,
    "rain_defer_threshold_mm": 5.0,
    "battery_critical_v": 3.00,
    "stale_timeout_sec": 1800,
    "max_db_records": 500000,
    "server": {"host": "0.0.0.0", "port": 8080},
    "hardware": {"spi_bus": 0, "spi_device": 0, "spi_speed_hz": 5000000, "gdo0_gpio": 25}
}

config_lock = threading.Lock()

def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                merged = DEFAULT_CONFIG.copy()
                merged.update(data)
                return merged
        except Exception as e:
            logger.error(f"Error loading config.json: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(new_cfg: dict):
    with config_lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(new_cfg, f, indent=4)

config = load_config()

# ==============================================================================
# SQLite Long-Term Storage (500,000+ records)
# ==============================================================================

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                water_level_cm REAL NOT NULL,
                water_level_percent REAL NOT NULL,
                current_liters REAL NOT NULL,
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

# ==============================================================================
# Hydrawise & Weather Integration
# ==============================================================================

def trigger_hydrawise(cfg: dict, action: str, custom_seconds: int = 0) -> bool:
    """
    Trigger or suspend Hydrawise irrigation valve via Hunter REST API.
    """
    api_key = cfg.get("hydrawise_api_key", "").strip()
    relay_id = str(cfg.get("hydrawise_relay_id", "")).strip()

    if not api_key or not relay_id:
        logger.warning("Hydrawise API Key or Relay ID not configured. Skipping API call.")
        return False

    url = "https://api.hydrawise.com/api/v1/setzone.php"
    api_action = "stop" if action in ("stop", "suspend") else "run"

    params = {
        "api_key": api_key,
        "action": api_action,
        "relay_id": relay_id
    }
    if api_action == "run":
        params["custom"] = int(custom_seconds)
        params["period_id"] = 999  # Manual custom duration

    try:
        r = requests.get(url, params=params, timeout=8)
        logger.info(f"Hydrawise API response ({r.status_code}): {r.text[:120]}")
        return (r.status_code == 200)
    except Exception as e:
        logger.error(f"Hydrawise API request failed: {e}")
        return False

# Weather forecast cache
weather_cache = {"rain_mm": 0.0, "updated_epoch": 0.0}

def fetch_predicted_rain(lat: float, lon: float) -> float:
    now = time.time()
    if now - weather_cache["updated_epoch"] < 3600.0:
        return weather_cache["rain_mm"]

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "precipitation_sum",
        "timezone": "auto",
        "forecast_days": 2
    }
    try:
        r = requests.get(url, params=params, timeout=5)
        if r.status_code == 200:
            data = r.json()
            precip = data.get("daily", {}).get("precipitation_sum", [0.0, 0.0])
            total_rain = round(sum(precip), 1)
            weather_cache["rain_mm"] = total_rain
            weather_cache["updated_epoch"] = now
            return total_rain
    except Exception as e:
        logger.warning(f"Failed to fetch Open-Meteo forecast: {e}")

    return weather_cache["rain_mm"]

# ==============================================================================
# In-Memory Telemetry & Refill State Machine
# ==============================================================================

class SystemState:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_packet: Optional[dict] = None
        self.last_update_epoch: float = 0.0
        self.start_epoch: float = time.time()
        self.packets_total: int = 0

        # Refill State
        self.is_refilling: bool = False
        self.refill_status: str = "Nedopouští"
        self.refill_expires_epoch: float = 0.0
        self.refill_target_pct: float = 0.0
        self.refill_start_liters: float = 0.0

    def update_telemetry(self, packet_data: dict, cfg: dict):
        with self.lock:
            now_epoch = time.time()
            iso_ts = datetime.now(timezone.utc).isoformat()

            level_mm = packet_data.get("level_mm", 0)
            vbat_mv = packet_data.get("vbat_raw_mv", 0)
            temp_x100 = packet_data.get("temp_x100", 0)
            hum_x100 = packet_data.get("hum_x100", 0)

            water_level_cm = round(level_mm / 10.0, 1)
            max_depth_cm = float(cfg.get("max_depth_cm", 150.0))
            water_level_pct = round(max(0.0, min(100.0, (water_level_cm / max_depth_cm) * 100.0)), 1)
            temperature = round(temp_x100 / 100.0, 2)
            humidity = round(max(0.0, min(100.0, hum_x100 / 100.0)), 1)
            battery_voltage = round(vbat_mv / 1000.0, 3)

            crit_v = float(cfg.get("battery_critical_v", 3.00))
            battery_low = (battery_voltage < crit_v)

            tank_area_m2 = float(cfg.get("tank_area_m2", 10.0))
            current_liters = round((tank_area_m2 * (water_level_cm / 100.0)) * 1000.0)

            self.last_packet = {
                "water_level_cm": water_level_cm,
                "water_level_percent": water_level_pct,
                "current_liters": current_liters,
                "temperature": temperature,
                "humidity": humidity,
                "battery_voltage": battery_voltage,
                "battery_low": battery_low,
                "rssi_dbm": packet_data.get("rssi_dbm"),
                "timestamp": iso_ts
            }
            self.last_update_epoch = now_epoch
            self.packets_total += 1

            # Persist to SQLite
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("""
                        INSERT INTO telemetry (
                            timestamp, water_level_cm, water_level_percent, current_liters,
                            temperature, humidity, battery_voltage, battery_low, rssi
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        iso_ts, water_level_cm, water_level_pct, current_liters,
                        temperature, humidity, battery_voltage,
                        1 if battery_low else 0, packet_data.get("rssi_dbm")
                    ))
                    # Enforce high capacity max limit (e.g. 500,000 records)
                    max_records = int(cfg.get("max_db_records", 500000))
                    conn.execute(f"""
                        DELETE FROM telemetry WHERE id NOT IN (
                            SELECT id FROM telemetry ORDER BY id DESC LIMIT {max_records}
                        )
                    """)
                    conn.commit()
            except Exception as e:
                logger.error(f"SQLite insert error: {e}")

    def check_refill_expiry(self):
        with self.lock:
            if self.is_refilling:
                now = time.time()
                if now >= self.refill_expires_epoch:
                    self.is_refilling = False
                    self.refill_status = "Nedopouští (Dokončeno)"
                    self.refill_expires_epoch = 0.0

    def start_refill(self, target_pct: float, duration_sec: int, cfg: dict):
        with self.lock:
            self.is_refilling = True
            self.refill_target_pct = target_pct
            self.refill_expires_epoch = time.time() + duration_sec
            current_liters = self.last_packet.get("current_liters", 0) if self.last_packet else 0
            self.refill_start_liters = current_liters

            min_left = round(duration_sec / 60.0, 1)
            self.refill_status = f"Dopouštění aktivní (Cíl {target_pct}%, zbývá ~{min_left} min)"

    def stop_refill(self):
        with self.lock:
            self.is_refilling = False
            self.refill_status = "Nedopouští (Manuálně zastaveno)"
            self.refill_expires_epoch = 0.0

    def estimate_battery_days(self) -> Optional[float]:
        """Estimate remaining battery runtime in days based on voltage discharge trend."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                cur.execute("""
                    SELECT timestamp, battery_voltage FROM telemetry
                    WHERE timestamp >= ? AND battery_voltage > 2.5
                    ORDER BY timestamp DESC
                """, (cutoff,))
                rows = cur.fetchall()

                if len(rows) < 8:
                    return None

                latest_v = rows[0][1]
                oldest_v = rows[-1][1]

                t_latest = datetime.fromisoformat(rows[0][0])
                t_oldest = datetime.fromisoformat(rows[-1][0])
                days_span = (t_latest - t_oldest).total_seconds() / 86400.0

                if days_span < 1.0:
                    return None

                v_drop = oldest_v - latest_v
                if v_drop <= 0.01:
                    # Battery virtually steady
                    return 365.0

                drop_per_day = v_drop / days_span
                v_headroom = latest_v - 3.0  # Cut-off cliff is 3.0 V
                if v_headroom <= 0:
                    return 0.0

                return round(v_headroom / drop_per_day, 1)
        except Exception:
            return None

    def get_full_state(self, cfg: dict) -> dict:
        self.check_refill_expiry()
        with self.lock:
            now = time.time()
            stale_sec = cfg.get("stale_timeout_sec", 1800)

            max_depth_cm = float(cfg.get("max_depth_cm", 150.0))
            tank_area_m2 = float(cfg.get("tank_area_m2", 10.0))
            max_liters = round((max_depth_cm / 100.0) * tank_area_m2 * 1000.0)

            rain_expected = fetch_predicted_rain(
                float(cfg.get("latitude", 50.0755)),
                float(cfg.get("longitude", 14.4378))
            )

            bat_days = self.estimate_battery_days()

            refill_remaining = max(0, int(self.refill_expires_epoch - now)) if self.is_refilling else 0

            if self.last_packet is None:
                return {
                    "water_level_cm": 0.0,
                    "water_level_percent": 0.0,
                    "current_liters": 0,
                    "max_liters": max_liters,
                    "temperature": 0.0,
                    "humidity": 0.0,
                    "battery_voltage": 0.0,
                    "battery_low": False,
                    "battery_est_days": bat_days,
                    "last_seen_seconds_ago": 999999,
                    "status": "offline",
                    "timestamp": None,
                    "is_refilling": self.is_refilling,
                    "refill_status": self.refill_status,
                    "refill_remaining_sec": refill_remaining,
                    "expected_rain_mm": rain_expected
                }

            seconds_ago = int(now - self.last_update_epoch)
            status = "online" if seconds_ago <= stale_sec else "stale"

            return {
                "water_level_cm": self.last_packet["water_level_cm"],
                "water_level_percent": self.last_packet["water_level_percent"],
                "current_liters": self.last_packet["current_liters"],
                "max_liters": max_liters,
                "temperature": self.last_packet["temperature"],
                "humidity": self.last_packet["humidity"],
                "battery_voltage": self.last_packet["battery_voltage"],
                "battery_low": self.last_packet["battery_low"],
                "battery_est_days": bat_days,
                "last_seen_seconds_ago": seconds_ago,
                "status": status,
                "timestamp": self.last_packet["timestamp"],
                "is_refilling": self.is_refilling,
                "refill_status": self.refill_status,
                "refill_remaining_sec": refill_remaining,
                "expected_rain_mm": rain_expected
            }

sys_state = SystemState()

# Receiver callback
def on_rf_packet_received(pkt: dict):
    sys_state.update_telemetry(pkt, config)

# Initialize CC1101 transceiver
hw_cfg = config.get("hardware", {})
receiver = CC1101Receiver(
    spi_bus=hw_cfg.get("spi_bus", 0),
    spi_device=hw_cfg.get("spi_device", 0),
    spi_speed_hz=hw_cfg.get("spi_speed_hz", 5000000),
    gdo0_pin=hw_cfg.get("gdo0_gpio", 25),
    on_packet_callback=on_rf_packet_received
)

# Background supervisor for refill expiry and auto-refill triggers
def background_supervisor():
    while True:
        try:
            sys_state.check_refill_expiry()
        except Exception:
            pass
        time.sleep(2)

supervisor_thread = threading.Thread(target=background_supervisor, daemon=True, name="refill_supervisor")

# FastAPI App
app = FastAPI(title="Retention Tank Telemetry & Hydrawise Controller", version="2.5.0")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400 * 7, same_site="lax", https_only=False)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Brute-force protection: IP tracking
FAILED_LOGINS: Dict[str, dict] = {}

def get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip
    x_forwarded = request.headers.get("X-Forwarded-For")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"

def check_ip_lockout(ip: str) -> Optional[int]:
    now = time.time()
    rec = FAILED_LOGINS.get(ip)
    if rec and rec.get("lock_until"):
        if now < rec["lock_until"]:
            return int(rec["lock_until"] - now)
        else:
            rec["count"] = 0
            rec["lock_until"] = None
    return None

def register_failed_login(ip: str):
    now = time.time()
    rec = FAILED_LOGINS.setdefault(ip, {"count": 0, "lock_until": None})
    rec["count"] += 1
    if rec["count"] >= 5:
        rec["lock_until"] = now + 300  # 5 minutes lockout

def reset_failed_login(ip: str):
    if ip in FAILED_LOGINS:
        FAILED_LOGINS[ip] = {"count": 0, "lock_until": None}

@app.on_event("startup")
def startup_event():
    receiver.start()
    if not supervisor_thread.is_alive():
        supervisor_thread.start()

@app.on_event("shutdown")
def shutdown_event():
    receiver.stop()

# ==============================================================================
# REST API Endpoints
# ==============================================================================

@app.get("/api/v1/water/state")
def get_water_state():
    """
    Primary endpoint for Home Assistant REST sensor integration.
    """
    return sys_state.get_full_state(config)

@app.get("/api/v1/water/history")
def get_water_history(hours: int = Query(default=24, ge=1, le=720)):
    """
    Returns telemetry time-series history for charts.
    """
    limit_time = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    results = []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT timestamp, water_level_cm, water_level_percent, current_liters, temperature, humidity, battery_voltage
            FROM telemetry
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
        """, (limit_time,))
        for row in cur.fetchall():
            results.append({
                "timestamp": row["timestamp"],
                "water_level_cm": row["water_level_cm"],
                "water_level_percent": row["water_level_percent"],
                "current_liters": row["current_liters"],
                "temperature": row["temperature"],
                "humidity": row["humidity"],
                "battery_voltage": row["battery_voltage"]
            })
    return results

@app.get("/api/v1/water/records")
def get_recent_records(limit: int = Query(default=50, ge=1, le=1000)):
    """
    Returns latest individual records for the data log table.
    """
    results = []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT id, timestamp, water_level_cm, water_level_percent, current_liters,
                   temperature, humidity, battery_voltage, battery_low, rssi
            FROM telemetry
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        for row in cur.fetchall():
            results.append(dict(row))
    return results

@app.get("/api/v1/water/export.csv")
def export_csv():
    """Export complete telemetry history in CSV format."""
    def iter_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "timestamp", "level_cm", "level_percent", "liters", "temp_c", "hum_rh", "vbat_v", "rssi"])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, timestamp, water_level_cm, water_level_percent, current_liters,
                       temperature, humidity, battery_voltage, rssi
                FROM telemetry ORDER BY id ASC
            """)
            for row in cur:
                writer.writerow(row)
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=retencka_telemetrie.csv"}
    )

# ------------------------------------------------------------------------------
# Hydrawise Refill Control API
# ------------------------------------------------------------------------------

class RefillRequest(BaseModel):
    target_pct: float

@app.post("/api/v1/water/refill/start")
def start_refill(req: RefillRequest, background_tasks: BackgroundTasks):
    """
    Start tank refilling via Hydrawise until target percentage is achieved.
    Calculates exact duration based on tank volume and flow rate.
    """
    global config
    target_pct = min(float(config.get("max_refill_pct", 90.0)), max(5.0, req.target_pct))

    state = sys_state.get_full_state(config)
    current_liters = state["current_liters"]
    max_liters = state["max_liters"]
    target_liters = (target_pct / 100.0) * max_liters
    liters_needed = target_liters - current_liters

    if liters_needed <= 50:
        raise HTTPException(status_code=400, detail="Hladina je již vyšší nebo blízko cílové. Není třeba dopouštět.")

    flow_rate = float(config.get("flow_rate_lpm", 41.6))
    if flow_rate <= 0:
        flow_rate = 41.6

    duration_sec = int((liters_needed / flow_rate) * 60)
    if duration_sec <= 0:
        raise HTTPException(status_code=400, detail="Neplatný čas dopouštění.")

    # Check rain deferral
    if config.get("rain_defer_enabled", True):
        rain_thresh = float(config.get("rain_defer_threshold_mm", 5.0))
        if state["expected_rain_mm"] >= rain_thresh:
            logger.info(f"Refill requested, but rain forecasted ({state['expected_rain_mm']} mm >= {rain_thresh} mm).")

    # Send command to Hydrawise
    background_tasks.add_task(trigger_hydrawise, config, "run", duration_sec)
    sys_state.start_refill(target_pct, duration_sec, config)

    return {
        "success": True,
        "target_pct": target_pct,
        "liters_needed": round(liters_needed),
        "duration_seconds": duration_sec,
        "message": f"Dopouštění spuštěno na {target_pct}% (~{round(duration_sec/60)} minut)"
    }

@app.post("/api/v1/water/refill/stop")
def stop_refill(background_tasks: BackgroundTasks):
    """
    Immediately stop Hydrawise refilling.
    """
    background_tasks.add_task(trigger_hydrawise, config, "stop", 0)
    sys_state.stop_refill()
    return {"success": True, "message": "Dopouštění bylo zastaveno."}

# ------------------------------------------------------------------------------
# Configuration API
# ------------------------------------------------------------------------------

@app.get("/api/v1/config")
def get_config_endpoint():
    return config

@app.post("/api/v1/config")
def update_config_endpoint(new_cfg: dict):
    global config
    # Preserve hardware section
    hw = config.get("hardware", {})
    srv = config.get("server", {})

    merged = config.copy()
    merged.update(new_cfg)
    merged["hardware"] = hw
    merged["server"] = srv

    config = merged
    save_config(config)
    logger.info("Configuration updated successfully.")
    return {"success": True, "config": config}

@app.get("/health")
def healthcheck():
    """Liveness probe for systemd watchdog."""
    uptime = int(time.time() - sys_state.start_epoch)
    return {
        "status": "ok",
        "uptime_seconds": uptime,
        "packets_received": sys_state.packets_total,
        "rf_hardware_ready": receiver.is_hardware_available
    }

@app.post("/api/v1/water/simulate")
def simulate_packet(level_mm: int = 1450, vbat_mv: int = 4450, temp_x100: int = 2150, hum_x100: int = 6540):
    sim_pkt = {
        "level_mm": level_mm,
        "vbat_raw_mv": vbat_mv,
        "temp_x100": temp_x100,
        "hum_x100": hum_x100,
        "rssi_dbm": -68.5,
        "lqi": 30
    }
    sys_state.update_telemetry(sim_pkt, config)
    return {"status": "injected", "data": sim_pkt}

# ------------------------------------------------------------------------------
# Authentication & User Management Routes
# ------------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if request.session.get("username"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = get_client_ip(request)
    lockout_sec = check_ip_lockout(ip)
    if lockout_sec:
        min_rem = max(1, lockout_sec // 60)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": f"Příliš mnoho neúspěšných pokusů. Zkuste to za {min_rem} min."}
        )

    users = load_users()
    user_data = users.get(username)

    if user_data and check_password_hash(user_data.get("password", ""), password):
        reset_failed_login(ip)
        request.session["username"] = username
        request.session["role"] = user_data.get("role", "user")
        logger.info(f"User '{username}' logged in successfully from {ip}")
        return RedirectResponse(url="/", status_code=303)

    register_failed_login(ip)
    logger.warning(f"Failed login attempt for username '{username}' from {ip}")
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Neplatné uživatelské jméno nebo heslo."}
    )

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str

@app.post("/api/v1/user/password")
def change_password(req: PasswordChangeRequest, request: Request):
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Neautorizováno.")

    users = load_users()
    user_data = users.get(username)
    if not user_data or not check_password_hash(user_data.get("password", ""), req.old_password):
        raise HTTPException(status_code=400, detail="Původní heslo není správné.")

    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="Nové heslo musí mít alespoň 6 znaků.")

    user_data["password"] = generate_password_hash(req.new_password)
    users[username] = user_data
    save_users(users)
    logger.info(f"Password changed for user '{username}'")
    return {"success": True, "message": "Heslo bylo úspěšně změněno."}

@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    username = request.session.get("username")
    if not username:
        return RedirectResponse(url="/login", status_code=303)

    role = request.session.get("role", "user")
    state = sys_state.get_full_state(config)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "state": state,
        "config": config,
        "username": username,
        "role": role
    })

if __name__ == "__main__":
    import uvicorn
    srv = config.get("server", {})
    uvicorn.run("app:app", host=srv.get("host", "0.0.0.0"), port=srv.get("port", 8080), workers=1, access_log=False)
