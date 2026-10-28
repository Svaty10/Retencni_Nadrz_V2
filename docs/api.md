# REST API

## Home Assistant
Přes MQTT (Discovery), ne přes HTTP – viz [home_assistant.md](home_assistant.md). REST API na portu 8081
(do verze 1.21) bylo odstraněno; hub kromě webu (80) neotevírá žádný port.

## Kontrola, že aplikace běží (updater, `install.sh`)
Bez sítě: aplikace po startu zapíše `/var/lib/retencka/data/health.json`
`{"version": "1.22.0", "pid": 812, "radio": true, "ts": 1761640000}` (znovu jen při změně stavu rádia).
Updater bere novou verzi za funkční, když soubor po restartu zapsal hlavní proces služby (`MainPID`)
s očekávanou verzí a ten proces běží ještě 10 s; jinak do 120 s rollback.

## Webová aplikace – port 80
Data vybraného čidla: parametr `node=XXXX` (`state`, `history`, `flows`, `export.csv`). Vše pod `/api/` vyžaduje přihlášení (cookie session). Změnové požadavky navíc hlavičku
`X-CSRF-Token` (hodnota z `GET /api/me`). Role `user` = čtení, `admin` = vše.

| Metoda | Cesta | Role | Popis |
|---|---|---|---|
| GET | `/api/auth/mode` | – | `{"first_login": true}` dokud neexistuje uživatel |
| POST | `/api/auth/login` | – | `username`, `password` → cookie; při prvním přihlášení `admin` + heslo ze štítku → `{"setup": true}` |
| POST | `/api/auth/setup` | (setup) | vlastní admin účet (`username`, `password`), jen s lístkem z prvního přihlášení |
| POST | `/api/auth/logout` | user | |
| GET | `/api/me` | user | jméno, role, CSRF token, verze (session = cookie `rt_sid`, HttpOnly, SameSite=Lax) |
| POST | `/api/me/password` | user | `old_password`, `new_password` |
| POST | `/api/me/theme` | user | `theme`: `auto` / `light` / `dark` – motiv uložený u účtu (`GET /api/me` ho vrací v `theme`) |
| POST | `/api/me/lang` | user | `lang`: `cs` / `en` – jazyk rozhraní uložený u účtu (`GET /api/me` ho vrací v `lang`) |
| GET | `/api/state` | user | kompletní živý stav pro dashboard; `disk_low` (na kartě méně než 300 MB), `disk_free_mb` |
| GET | `/api/history?range=24h\|7d\|30d\|365d` nebo `?from=&to=` (epoch s) | user | zprůměrovaná řada (max ~600 bodů): hladina, objem, `probe_mv` (napětí sondy), klima, baterie (`vbat` V, `bat_pct` % podle křivky typu baterie, `sag_mv` pokles při vysílání; `probe_mv`/`sag_mv` jen z 5min dat, v hodinových agregátech `null`), `rssi`, `refill`, `bands` (úseky dopouštění a přítoku z deště: `kind`, `start`, `end`, `litres`), `boots` (vložení baterie / restart čidla) |
| GET | `/api/flows?…` | user | denní spotřeba / přítok ze srážek / dopouštění |
| GET | `/api/weather` | user | předpověď 7 dní (Open-Meteo, cache 1 h) |
| GET | `/api/export.csv?…` | admin | CSV (středník, desetinná čárka) |
| GET/POST | `/api/settings` | admin | globální nastavení (validované; API klíč maskovaný): poloha `latitude`, `longitude`, `location_name`, `hydrawise_api_key`, `telegram_token`, `retention_days`, … (kapacita baterií je od verze s migrací v11 u nádrží, viz `/api/node-defaults`) |
| GET | `/api/geocode?q=obec` | admin | hledání místa (Open-Meteo geocoding přes hub; `q` 2–80 znaků): `{"results": [{name, admin1, country, lat, lon}]}` (max 8); uložení = `POST /api/settings` s `latitude`, `longitude`, `location_name` |
| POST | `/api/hydrawise/test` | admin | test klíče Hydrawise (uložený klíč účtu, nebo `{"api_key": "…"}` zadaný a neuložený): `{"ok": true, "controllers": ["název ovladače"]}`; klíč se nikdy nevrací |
| GET | `/api/hydrawise/zones?node=` | admin | zóny z Hydrawise (`statusschedule.php`, klíč nádrže nebo účtu): `{"zones": [{relay_id, name, number}]}`; `relay_id` se ukládá do `hydrawise_relay_id` nádrže (jen číslice, vložený API klíč se odmítne s vysvětlením) |
| POST | `/api/node?node=` | admin | nastavení čidla (název, tvar nádrže `tank_shape`: `area` (kvádr: plocha dna × hloubka) / `cyl_v` / `cyl_h`; starší `rect` (délka × šířka) se dál počítá, web ho ukáže jako `area` s plochou délka × šířka a při uložení převede, rozměry `max_depth_cm`, `tank_area_m2`, `tank_diameter_cm`, `tank_length_cm`, `tank_width_cm`, `cal_points`, přesnost/rozsah sondy, výška sondy nad dnem, baterie, dopouštění…). Zděditelné klíče (viz `/api/node-defaults`) se uloží jako vlastní hodnota nádrže jen když se liší od zděděné; `{"reset": ["klíč", …]}` vlastní hodnoty zruší (zase platí výchozí). Odpověď i `GET /api/nodes` (`nodes[]`) obsahují efektivní hodnoty + `inherited` / `overrides` (seznamy klíčů) |
| GET/POST | `/api/node-defaults` | admin | výchozí nastavení nádrží (od 1.13.0 bez vlastní karty ve webu – nová nádrž je dostane a mění se u každé nádrže zvlášť): `{"defaults": {…}, "keys": [zděditelné klíče]}`; POST `{klíč: hodnota}` změní výchozí hodnotu u nádrží bez vlastní hodnoty. Výchozí hodnoty obsahují předvyplněné parametry baterie (`battery_type`: `alkaline`, `battery_capacity_mah`, `battery_capacity_alk_mah`), sondy (`probe_accuracy_pct`, `probe_range_cm`, `sensor_offset_cm`), odtokového součinitele a pravidel dopouštění; individuální rozměry nádrže, plocha střechy a průtok mají výchozí hodnotu `0.0` (nevyplněno) |
| POST | `/api/calibration/point?node=` | admin | `actual_cm` – kalibrační bod z aktuálního napětí sondy |
| POST | `/api/calibration/recompute?node=` | admin | přepočet historie (kalibrace + vyhlazení); po `POST /api/node` se změnou parametru nádrže (odpověď `recalc: true`) a po kalibračním bodu běží sám |
| POST | `/api/calibration/battery?node=` | admin | `actual_v` – korekce měření baterie |
| POST | `/api/refill/start?node=` | admin | `target_pct` – spustí dopouštění; bez čerstvé platné hladiny se požadavek uloží a spustí s příštím měřením (`refill.waiting_for_data`, max. 15 min) |
| POST | `/api/refill/stop?node=` | admin | zastaví / zruší: běžící dopouštění, čekající ruční požadavek i čekání na déšť; zrušené automatické dopouštění pozastaví automatiku na 24 h (`refill.auto_paused_until`) |
| POST | `/api/refill/resume?node=` | admin | znovu zapne automatiku pozastavenou zrušením |
| GET | `/api/nodes` · POST `/api/nodes/remove` · `/api/nodes/unpair/cancel` | admin | čidla, slyšená čidla (`archived` = hub má uschovaný klíč, `key_match` = čidlo s ním vysílá → „Spárovat znovu“), stav párování, statistiky rádia; `remove` = odpárování s potvrzením čidlem, `{"force": true}` = odebrat hned (klíč do archivu) |
| GET | `/api/pairing` · POST `/api/pairing/start` · `/api/pairing/cancel` | admin | párování (`node_id` hex, `name`); stav: `requests` (+ `archived`), `targets`, `pending` (klíč odeslán, čeká na první data ≤ 24 h); `cancel` zruší i čekající klíč |
| GET | `/api/users` · POST `/api/users/{add,role,delete,password}` | admin | správa uživatelů: `role` `{username, role: user\|admin}`, `password` `{username, password}` (nové heslo jinému uživateli, odhlásí ho), `delete` `{username}`; nejde na vlastním účtu a nikdy nezůstane žádný administrátor („Musí zůstat alespoň jeden administrátor.“) |
| GET | `/api/audit?page=&category=&who=&q=&from=&to=` | admin | log: `category` = login/users/settings/refill/sensors/system/problems, `who` = people/system/jméno, `q` = text. Výsledky root updateru zapisuje aplikace podle `update-status.json`: `UPDATE_AVAILABLE`, `UPDATE_INSTALLED`, `UPDATE_ROLLED_BACK`, `UPDATE_ERROR` (stejná chyba jen jednou), `UPDATE_UP_TO_DATE` (jen po ruční kontrole); dále `TLS_CERT_ISSUED`, `FACTORY_RESET`, `NODE_PAIRING_CANCELLED` |
| GET | `/api/mqtt` | admin | Home Assistant (MQTT): `configured`, `connected`, `error`, `base_topic`; nastavení `mqtt_*` přes `/api/settings` |
| GET | `/api/system` | admin | stav systému; `hostnames`, `ips` (IPv4 hubu: výchozí cesta + adresa, na kterou se připojil prohlížeč), `cpu_pct`, `ram_used_mb`, `ram_total_mb`, `ram_pct`, `rss_kb`, `db_mb`, `disk_free_mb`, `disk_low`, `low_disk_mb` (300), `update` = poslední `update-status.json`, `update_prefs` |
| POST | `/api/system/factory-reset` | admin | `{"password": "heslo přihlášeného admina", "confirm": "OBNOVIT"}` – obnova továrního nastavení: zapíše `data/factory-reset` a restartuje aplikaci, ta před otevřením databáze smaže obsah datového adresáře kromě `setup_code` (heslo ze štítku); 403 při špatném hesle |
| POST | `/api/system/restart` · `/api/system/reboot` · `/api/system/poweroff` | admin | restart aplikace (SIGTERM, systemd ji spustí znovu) / restart zařízení / vypnutí zařízení (root helper `retencka-control`: `control-reboot`, `control-poweroff`); zapíše se do auditu |
| POST | `/api/data/delete` | admin | `{"node": "XXXX" \| null (všechny), "from": epoch \| null, "to": epoch \| null, "dry_run": true}` – smaže měření, hodinové souhrny a dokončená dopouštění v rozsahu `[from, to)`; `dry_run` (výchozí) jen spočítá: `{"counts": {measurements, hourly, refills}, "total"}`. Audit se nemaže, smazání se do něj zapíše |
| POST | `/api/update/check` | admin | požádá root updater o kontrolu (nikdy neinstaluje); 409 když právě probíhá instalace. Stará cesta `POST /api/system/update` dělá totéž |
| POST | `/api/update/install` | admin | `{"version": "X"}` – instalace přesně té verze, kterou nabídla poslední kontrola (`state` available/error/rolled_back a `latest` = X), jinak 409 |
| GET | `/api/update/status` | admin | `{"status": {...}, "running": "verze webu", "prefs": {"auto", "channel"}, "pending": žádost čeká, "busy": updater pracuje}`; `status`: `state` (checking, available, up_to_date, error, downloading, verifying, installing, restarting, done, rolled_back), `current`, `latest`, `target`, `published_at`, `message`, `started_at`, `updated_at`, `notes` = `[{version, date, markdown}]` |
| POST | `/api/update/prefs` | admin | `{"auto": true\|false, "channel": "stable"\|"beta"}` – automatická instalace v noci a kanál (přísná validace) |
| GET | `/api/servis` · POST `/api/servis` | admin | vzdálený servis: stav (`installed`, `connected`, `ip`, `mode`, `until`, `install` = průběh instalace z webu `{state: queued/installing/done/error, message}`); POST `{"mode": "off"\|"24h"\|"on"}` |
| POST | `/api/servis/install` | admin | `{"setup_key": "…", "management_url": "https://…" (nepovinné)}` – instalace NetBird přes root helper; klíč se nevrací ani nezapisuje do záznamů |
| GET | `/api/backup` · `/api/backup/download` | admin | `{"backups": [{name, size_mb, ts}], "keep": 7, "dir": "…/backup"}` – automatické denní zálohy na kartě; stažení aktuální zálohy (`.db.gz`) |
| POST | `/api/backup/import[?full=1]` | admin | tělo = soubor zálohy (`.db.gz` / `.db`, max 1 GB, streamuje se); sloučení bez duplicit, `full=1` i nastavení a uživatelé; vrací počty |

`/api/state` navíc obsahuje `outlook` (`days_left`, `use_per_day_l` ze změřené spotřeby; `auto_refill`, `refill_in_days`, `refill_l`: kdy a kolik automatika dopustí podle změřené spotřeby a předpovědi deště, `null` = v příštích 7 dnech ne), změřené objemy `rain_today`, `rain_7d`, `refill_today`, `refill_7d` (litry dnes / za 7 dní, z rozdílů hladiny), pro schéma toků `catchment_m2`, `flowing` (`rain`, `use`: hladina se v posledních 15 min zvedla / klesla o víc než pásmo necitlivosti) a v `refill` příznak `fault` (poslední pokus o dopouštění skončil chybou ventilu)
a v `refill.zone_controlled_by` nádrž, která ovládá stejný ventil (pak je dopouštění u této nádrže vypnuté).

## Rádiový protokol v3
DATA 23 B (AES-128-CTR + CMAC, včetně diagnostického bajtu a poklesu baterie pod zátěží), PAIR_REQ 36 B / PAIR_ACK 35 B (X25519), UNPAIR 19 B (CMAC) – podrobnosti v `docs/ZADANI.md` (1.5, 1.6).
