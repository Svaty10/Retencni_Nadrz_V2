# REST API Specification – Retention Tank Webserver

Base URL: `http://<raspberry-pi-ip>:8080`

---

## 1. Endpoints

### 1.1. `GET /api/v1/water/state`
Returns the current water level, telemetry metrics, and connection status. Optimized for Home Assistant REST integration.

**Response `200 OK` (JSON):**
```json
{
  "water_level_cm": 145.0,
  "water_level_percent": 72.5,
  "temperature": 21.5,
  "humidity": 65.4,
  "battery_voltage": 4.45,
  "battery_low": false,
  "current_liters": 14500,
  "last_seen_seconds_ago": 12,
  "status": "online",
  "timestamp": "2026-10-10T09:58:30.123456+00:00"
}
```

**Status States:**
- `"online"`: Last packet received within `stale_timeout_sec` (default 1800 s / 30 min).
- `"stale"`: More than 30 minutes since last reception.
- `"offline"`: No packets received since server boot.

---

### 1.2. `GET /api/v1/water/history?hours=24`
Returns time-series telemetry data from the local SQLite database (`history.db`) for graph visualization.

**Parameters:**
- `hours` *(optional, integer, default: 24, min: 1, max: 720)*: Timespan in hours.

**Response `200 OK` (JSON):**
```json
[
  {
    "timestamp": "2026-10-10T08:00:00+00:00",
    "water_level_cm": 142.5,
    "water_level_percent": 71.2,
    "temperature": 20.8,
    "humidity": 66.1,
    "battery_voltage": 4.46
  },
  {
    "timestamp": "2026-10-10T08:15:00+00:00",
    "water_level_cm": 143.0,
    "water_level_percent": 71.5,
    "temperature": 21.0,
    "humidity": 65.8,
    "battery_voltage": 4.45
  }
]
```

---

### 1.3. `GET /health`
Liveness probe for monitoring and systemd watchdog integration.

**Response `200 OK` (JSON):**
```json
{
  "status": "ok",
  "uptime_seconds": 3600,
  "packets_received": 14,
  "rf_hardware_ready": true
}
```

---

### 1.4. `GET /`
Serves the responsive single-page HTML/CSS/Vanilla JS dashboard with animated water tank visualizer and Chart.js telemetry charts.
