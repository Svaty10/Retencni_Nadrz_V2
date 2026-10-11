# REST API

## Home Assistant – port 8081 (jen LAN)
Hlavička `Authorization: Bearer <token>` (token: *Nastavení → Home Assistant*).

`GET /api/v1/water/state?node=3F2A` (bez `node` = první čidlo) · `GET /api/v1/nodes` (seznam čidel)
```json
{
  "node_id": "3F2A", "name": "Retenční nádrž",
  "water_level_cm": 54.4, "water_level_percent": 59.2, "volume_liters": 5933, "max_liters": 10028,
  "temperature": 12.0, "humidity": 93.0, "battery_voltage": 4.95, "battery_percent": 100.0,
  "battery_low": false, "rssi": -71.5, "last_seen_seconds_ago": 12, "status": "online",
  "is_refilling": false, "consumption_today": 182, "expected_rain_mm": 3.4
}
```
`status`: `online` (paket < 16 min), `stale` (< 30 min, nastavitelné), `offline`.

`GET /health` (bez tokenu) → `{"status":"ok","version":"3.0.0","radio":"gdo0"}`; 503 = rádio nepracuje.

## Webová aplikace – port 80
Data vybraného čidla: parametr `node=XXXX` (`state`, `history`, `flows`, `export.csv`). Vše pod `/api/` vyžaduje přihlášení (cookie session). Změnové požadavky navíc hlavičku
`X-CSRF-Token` (hodnota z `GET /api/me`). Role `user` = čtení, `admin` = vše.

| Metoda | Cesta | Role | Popis |
|---|---|---|---|
| GET | `/api/auth/mode` | – | `{"first_login": true}` dokud neexistuje uživatel |
| POST | `/api/auth/login` | – | `username`, `password` → cookie; při prvním přihlášení `admin` + heslo ze štítku → `{"setup": true}` |
| POST | `/api/auth/setup` | (setup) | vlastní admin účet (`username`, `password`), jen s lístkem z prvního přihlášení |
| POST | `/api/auth/logout` | user | |
| GET | `/api/me` | user | jméno, role, CSRF token, verze |
| POST | `/api/me/password` | user | `old_password`, `new_password` |
| GET | `/api/state` | user | kompletní živý stav pro dashboard |
| GET | `/api/history?range=24h\|7d\|30d\|365d` nebo `?from=&to=` (epoch s) | user | zprůměrovaná řada (max ~600 bodů) |
| GET | `/api/flows?…` | user | denní spotřeba / přítok ze srážek / dopouštění |
| GET | `/api/weather` | user | předpověď 7 dní (Open-Meteo, cache 1 h) |
| GET | `/api/export.csv?…` | user | CSV (středník, desetinná čárka) |
| GET/POST | `/api/settings` | admin | globální nastavení (validované; API klíč maskovaný) |
| POST | `/api/node?node=` | admin | nastavení čidla (název, rozměry, `cal_points`, přesnost/rozsah sondy, …) |
| POST | `/api/calibration/point?node=` | admin | `actual_cm` – kalibrační bod z aktuálního napětí sondy |
| POST | `/api/calibration/recompute?node=` | admin | přepočet historie (kalibrace + vyhlazení) |
| POST | `/api/calibration/battery?node=` | admin | `actual_v` – korekce měření baterie |
| POST | `/api/refill/start?node=` · `/api/refill/stop?node=` | admin | `target_pct` – dopouštění dané nádrže |
| GET | `/api/nodes` · POST `/api/nodes/remove` | admin | čidla, slyšená čidla, stav párování, statistiky rádia |
| GET | `/api/pairing` · POST `/api/pairing/start` · `/api/pairing/cancel` | admin | párování (`node_id` hex, `name`) |
| GET | `/api/users` · POST `/api/users/{add,role,delete,password}` | admin | správa uživatelů |
| GET | `/api/audit?page=&action=` | admin | audit log |
| GET | `/api/ha` · POST `/api/ha/rotate` | admin | token pro HA |
| GET | `/api/system` · POST `/api/system/update` | admin | stav systému, ruční kontrola aktualizací |

## Rádiový protokol v3
DATA 21 B (AES-128-CTR + CMAC), PAIR_REQ / PAIR_ACK 35 B (X25519) – podrobnosti v `docs/ZADANI.md` (1.5, 1.6).
