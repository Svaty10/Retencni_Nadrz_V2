# REST API Specification – Retention Tank Webserver

Base URL: `http://<raspberry-pi-ip>:8080`

---

## 1. Telemetrie a Stav

### 1.1. `GET /api/v1/water/state`
Vrací aktuální stav hladiny vody, senzorů SHT40, baterie a stav dopouštění Hydrawise. Primární endpoint pro Home Assistant REST senzor.

**Response `200 OK` (JSON):**
```json
{
  "water_level_cm": 145.0,
  "water_level_percent": 72.5,
  "current_liters": 14500,
  "max_liters": 15000,
  "temperature": 21.5,
  "humidity": 65.4,
  "battery_voltage": 4.45,
  "battery_low": false,
  "battery_est_days": 320.5,
  "last_seen_seconds_ago": 12,
  "status": "online",
  "timestamp": "2026-10-10T10:12:30.123456+00:00",
  "is_refilling": false,
  "refill_status": "Nedopouští",
  "refill_remaining_sec": 0,
  "expected_rain_mm": 1.2
}
```

---

### 1.2. `GET /api/v1/water/history?hours=24`
Vrací časovou řadu naměřených dat pro vykreslení grafů na webu.
- `hours` *(integer, default 24)*: Časový rozsah v hodinách.

---

### 1.3. `GET /api/v1/water/records?limit=50`
Vrací posledních `limit` jednotlivých záznamů pro zobrazení v logovací tabulce.

---

### 1.4. `GET /api/v1/water/export.csv`
Stáhne kompletní historii všech telemetrických záznamů ze SQLite databáze ve formátu CSV.

---

## 2. Řízení Dopouštění (Hydrawise)

### 2.1. `POST /api/v1/water/refill/start`
Spustí dopouštění z vodovodního řadu přes Hunter Hydrawise API na požadované cílové procento.

**Request Body (JSON):**
```json
{
  "target_pct": 80.0
}
```

**Response `200 OK` (JSON):**
```json
{
  "success": true,
  "target_pct": 80.0,
  "liters_needed": 1125,
  "duration_seconds": 1622,
  "message": "Dopouštění spuštěno na 80% (~27 minut)"
}
```

---

### 2.2. `POST /api/v1/water/refill/stop`
Okamžitě zastaví probíhající dopouštění přes Hydrawise.

**Response `200 OK` (JSON):**
```json
{
  "success": true,
  "message": "Dopouštění bylo zastaveno."
}
```

---

## 3. Konfigurace & Systém

### 3.1. `GET /api/v1/config`
Vrací aktuální konfigurační parametry serveru.

### 3.2. `POST /api/v1/config`
Uloží nové konfigurační parametry do `config.json` a ihned aplikuje změny na běžící server.

### 3.3. `GET /health`
Liveness probe pro systemd watchdog a kontrolu stavu rádiového přijímače.
