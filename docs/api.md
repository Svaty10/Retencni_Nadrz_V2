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

`GET /health` (bez tokenu) → `{"status":"ok","version":"1.0.0","radio":"gdo0"}`; 503 = rádio nepracuje.

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
| GET | `/api/history?range=24h\|7d\|30d\|365d` nebo `?from=&to=` (epoch s) | user | zprůměrovaná řada (max ~600 bodů): hladina, objem, klima, baterie, `rssi`, `refill`, `bands` (úseky dopouštění a přítoku z deště: `kind`, `start`, `end`, `litres`), `boots` (vložení baterie / restart čidla) |
| GET | `/api/flows?…` | user | denní spotřeba / přítok ze srážek / dopouštění |
| GET | `/api/weather` | user | předpověď 7 dní (Open-Meteo, cache 1 h) |
| GET | `/api/export.csv?…` | admin | CSV (středník, desetinná čárka) |
| GET/POST | `/api/settings` | admin | globální nastavení (validované; API klíč maskovaný) |
| POST | `/api/node?node=` | admin | nastavení čidla (název, rozměry, `cal_points`, přesnost/rozsah sondy, …) |
| POST | `/api/calibration/point?node=` | admin | `actual_cm` – kalibrační bod z aktuálního napětí sondy |
| POST | `/api/calibration/recompute?node=` | admin | přepočet historie (kalibrace + vyhlazení) |
| POST | `/api/calibration/battery?node=` | admin | `actual_v` – korekce měření baterie |
| POST | `/api/refill/start?node=` | admin | `target_pct` – spustí dopouštění; bez čerstvé platné hladiny se požadavek uloží a spustí s příštím měřením (`refill.waiting_for_data`, max. 15 min) |
| POST | `/api/refill/stop?node=` | admin | zastaví / zruší: běžící dopouštění, čekající ruční požadavek i čekání na déšť; zrušené automatické dopouštění pozastaví automatiku na 24 h (`refill.auto_paused_until`) |
| POST | `/api/refill/resume?node=` | admin | znovu zapne automatiku pozastavenou zrušením |
| GET | `/api/nodes` · POST `/api/nodes/remove` · `/api/nodes/unpair/cancel` | admin | čidla, slyšená čidla, stav párování, statistiky rádia; `remove` = odpárování s potvrzením čidlem, `{"force": true}` = smazat hned |
| GET | `/api/pairing` · POST `/api/pairing/start` · `/api/pairing/cancel` | admin | párování (`node_id` hex, `name`) |
| GET | `/api/users` · POST `/api/users/{add,role,delete,password}` | admin | správa uživatelů |
| GET | `/api/audit?page=&category=&who=&q=&from=&to=` | admin | log: `category` = login/users/settings/refill/sensors/system/problems, `who` = people/system/jméno, `q` = text |
| GET | `/api/ha` · POST `/api/ha/rotate` | admin | token pro HA |
| GET | `/api/system` · POST `/api/system/update` | admin | stav systému, ruční kontrola aktualizací |
| GET | `/api/backup` · `/api/backup/download` | admin | seznam nočních záloh; stažení aktuální zálohy (`.db.gz`) |
| POST | `/api/backup/import[?full=1]` | admin | tělo = soubor zálohy (`.db.gz` / `.db`, max 1 GB, streamuje se); sloučení bez duplicit, `full=1` i nastavení a uživatelé; vrací počty |

`/api/state` navíc obsahuje `outlook` (`days_left`, `use_per_day_l`, `rain_7d_mm`, `inflow_7d_l`, `pct_after_rain`)
a v `refill.zone_controlled_by` nádrž, která ovládá stejný ventil (pak je dopouštění u této nádrže vypnuté).

## Rádiový protokol v3
DATA 23 B (AES-128-CTR + CMAC, včetně diagnostického bajtu a poklesu baterie pod zátěží), PAIR_REQ 36 B / PAIR_ACK 35 B (X25519), UNPAIR 19 B (CMAC) – podrobnosti v `docs/ZADANI.md` (1.5, 1.6).
