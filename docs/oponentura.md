# Oponentura architektury

Nezávislé posouzení architektury nad verzí 1.12.1 (commit `945056e`, 25. 10. 2026). Doplňuje
[architektura.md](architektura.md) (jak je systém postavený) a [bezpecnost.md](bezpecnost.md)
(bezpečnostní revize). Nálezy nejsou opravené – slouží jako podklad pro další vývoj.

Postup: ruční revize kódu `server/retencka/` (hlavně `core.py`, `external.py`, `db.py`, `__main__.py`),
updateru `system/bin/retencka-update.py`, webu `server/static/js/i18n.js` a dokumentace.
Firmware `uzel/` a root pomocníci byli posouzeni jen na úrovni dokumentace.

## Shrnutí

Architektura je pro daný účel dobře postavená: jasný princip, promyšlené pojistky ventilu, popsaný model
útočníka, podepsané aktualizace a slušné testy. Dva nálezy (1, 2) stojí za opravu hned, protože se týkají
ventilu a dat. Ostatní nálezy se týkají hlavně udržovatelnosti.

## Silné stránky

- **Čidlo posílá surové hodnoty, hub počítá.** Kalibrace a geometrie se mění bez flashování a historii
  jde zpětně přepočítat (`Core.recompute_levels`). Nejlepší rozhodnutí celého návrhu.
- **Pojistky dopouštění ve více vrstvách:** časovač přímo v Hydrawise (ventil se zavře i po pádu hubu),
  start jen s čerstvou platnou hladinou, zastavení při výpadku dat, opakování nepotvrzeného zastavení,
  jeden ventil = jedna nádrž.
- **Hranice oprávnění:** aplikace běží bez práv roota, akce s právy roota spouští přes soubory a systemd
  `.path` jednotky, ne přes sudo.
- **Úspornost a provoz:** jen standardní knihovna Pythonu, SQLite WAL s připojením pro každé vlákno,
  systemd watchdog vázaný i na rádiové vlákno, systémový disk jen pro čtení.
- **Dokumentace a testy** pokrývají kritické cesty (párování, dopouštění, migrace, zálohy, aktualizace).

## Nálezy

| # | Závažnost | Oblast | Kde | Stav |
|---|---|---|---|---|
| 1 | střední | nejistý výsledek startu ventilu | `server/retencka/core.py` `_refill_start`, `external.py` `Hydrawise.call` | **opraveno** (preventivní `stop` + `stop_retry`) |
| 2 | střední | rollback aktualizace nevrací databázi | `system/bin/retencka-update.py` `install()`, `server/retencka/db.py` `_migrate` | **opraveno** (snapshot DB + rollback, `SchemaTooNewError`) |
| 3 | střední | poškozená DB = ztráta všech párování | `server/retencka/__main__.py` `open_database` | **opraveno** (obnova noční zálohy + zrcadlení `<db>.keys`) |
| 4 | udržovatelnost | přetížená třída `Core`, implicitní stavy dopouštění | `server/retencka/core.py` | otevřeno |
| 5 | udržovatelnost | překlad rozhraní přepisem vykresleného textu | `server/static/js/i18n.js` | **opraveno** (aktualizován slovník, test pokrytí) |
| 6 | nízká | drobnosti (SQLite ve smyčce, vlastní kryptografie, Hydrawise API) | různé | otevřeno |

### 1. Nejistý výsledek startu ventilu

- **Stav:** opraveno ve verzi 1.13.1.
- **Řešení:** když Hydrawise na start (`run`) vůbec neodpoví (timeout, přerušené spojení – `Hydrawise.UNSURE`), hub okamžitě odešle preventivní příkaz `stop`. Pokud ani ten neprojde, vytvoří se záznam v tabulce `refills` s příznakem `stop_retry = True` a periodická smyčka `refill_tick` opakuje zastavení ventilu každých 30 s až do úspěchu nebo vypršení limitu. Odpoví-li Hydrawise chybou, ventil se neotevřel: nic se nezastavuje (neukončí se běh z rozvrhu Hydrawise) ani nezapisuje.

`Core._refill_start` po neúspěšném volání `run` na Hydrawise dříve jen zalogoval chybu a skončil.
`Hydrawise.call` zkouší start 3× s timeoutem 10 s; při timeoutu ale hub neví, jestli ventil opravdu neběží.

Scénář: požadavek dorazí na Hydrawise, odpověď se ztratí. Ventil běží celý naplánovaný čas
(až `max_refill_minutes`) a hub o tom neví:

- nezastaví ho při dosažení cíle (uzavřená smyčka nefunguje),
- nárůst hladiny zapíše jako „přítok z deště“,
- automatika se po `AUTO_COOLDOWN_S` (30 min) může pokusit o další start.

Horní mez dává časovač ventilu, ale bez sledování hubem.

**Návrh:** po neúspěšném startu poslat preventivně `stop` a záznam dopouštění vést jako nepotvrzený –
stejně jako kód už řeší nepotvrzené zastavení (`stop_retry` v `refill_tick`).

### 2. Rollback aktualizace nevrací databázi

- **Stav:** opraveno ve verzi 1.13.1.
- **Řešení:** `retencka-update.py` před přepnutím symlinku na novou verzi vytvoří snapshot databáze (`snapshot_db`) přes SQLite Backup API. Pokud kontrola zdraví selže, provede rollback symlinku, systemd jednotek i bezpečné obnovení původní databáze ze snapshotu (`restore_db`). Metoda `Database._migrate` navíc při `user_version > len(MIGRATIONS)` vyvolá výjimku `SchemaTooNewError` a odmítne běh starého kódu nad novějším schématem. Testováno v `test_updates.InstallAndRollback.test_unhealthy_update_restores_database`.

Nová verze při startu provede migrace DB. Když neprojde kontrolou zdraví (`/health`), updater vrátil kód
(symlink) a systemd jednotky, ale databáze zůstala migrovaná. Starý kód novější schéma nepoznal –
`Database._migrate` při `user_version` vyšší než počet známých migrací jen nic nemigroval a běžel dál.
Migrace přitom nejsou jen přidávání sloupců: v9 a v11 přepisují JSON v nastavení.

**Návrh:**

- před restartem na novou verzi udělat snapshot DB (`Core.snapshot` už existuje) a při rollbacku ho vrátit,
- v `_migrate` při `user_version > len(MIGRATIONS)` odmítnout start (nebo režim jen pro čtení),
- zavést pravidlo, že migrace mají být zpětně kompatibilní (jen přidávání), a výjimky výslovně označit.

### 3. Poškozená databáze znamená ztrátu všech párování

- **Stav:** opraveno ve verzi 1.13.1.
- **Řešení:** funkce `open_database` v `server/retencka/__main__.py` se při zjištění poškození databáze nejprve pokusí automaticky vyhledat a obnovit poslední platnou noční zálohu ze složky `backups/`. Zároveň `Core` atomicky zrcadlí všechny klíče a konfigurace čidel do pomocného souboru `<db>.keys` (`_sync_keys_mirror`), odkud lze párování ihned zrekonstruovat i při spuštění s novou prázdnou DB.

`open_database` při poškození DB soubor dříve odsunula stranou a začala s prázdnou. V DB jsou ale i klíče čidel –
všechna čidla jsou pak pro hub „cizí“ a obnova vede jen přes ruční import zálohy, jinak přes smazání
párování přes UPDI. Noční zálohy (`BACKUP_KEEP` = 7) leží na stejné SD kartě.

**Návrh:**

- při poškození automaticky zkusit obnovit poslední noční zálohu,
- klíče čidel držet navíc v malém samostatném souboru (atomický zápis),
- v dokumentaci doporučit pravidelnou zálohu mimo zařízení.

### 4. Přetížená třída `Core` a implicitní stavy dopouštění

`core.py` má přes 2 000 řádků a v jedné třídě míchá párování, ukládání měření, řízení dopouštění,
zálohy, NetBird, servisní přístup a stav aktualizací.

Řízení dopouštění je v [architektura.md](architektura.md#35-řízení-dopouštění) popsané stavovým diagramem,
v kódu je ale rozprostřené do sedmi slovníků (`refills`, `refill_wait`, `_rain_wait`, `_auto_pause`,
`_auto_block_until`, `refill_notes`, …). Stav nádrže je tak kombinace klíčů a nedovolené kombinace
nic nehlídá.

**Návrh:** vyčlenit `RefillController` s explicitním stavem pro každou nádrž (enum + povolené přechody)
a podobně modul párování. Současné testy (`test_refill`, `test_core_fixes`, `test_pairing_scenarios`)
umožňují refaktor provést bezpečně.

### 5. Překlad rozhraní přepisem vykresleného textu

- **Stav:** opraveno ve verzi 1.13.1.
- **Řešení:** slovník `DICT` a regulární výrazy `PATTERNS` v `i18n.js` byly synchronizovány se všemi změnami a sjednocenými pojmy rozhraní. Doplněn byl automatický test pokrytí překladů, který ověřuje přítomnost překladů pro všechny textové řetězce z HTML šablon a dynamických stavových hlášek.

`i18n.js` přes `MutationObserver` hledá v DOM české texty a nahrazuje je podle slovníku (`DICT`)
a regulárních výrazů (`PATTERNS`). Backend přitom posílá hotové české věty (`refill_notes`, záznamy
v logu, chybové hlášky). Každá změna textu kdekoli tiše rozbije anglickou verzi a žádný test to nezachytí.

**Návrh:** API vrací kód zprávy + parametry a překládá se podle kódu. Minimálně přidat test, který
najde české texty bez překladu.

### 6. Drobnosti

- **Synchronní SQLite v asyncio smyčce:** zápisy z `_store`, `_on_frame`, auditu a webových handlerů
  běží přímo ve smyčce. Když pomocné vlákno drží zápisovou transakci (import zálohy, mazání dat), smyčka
  může stát až 5 s (`busy_timeout`); při chybě „database is locked“ se měření jen zaloguje a zahodí.
  Při jednom paketu za minutu je dopad malý.
- **Vlastní kryptografie v čistém Pythonu** (`crypto.py`: AES, CMAC, X25519): důsledek pravidla
  „jen standardní knihovna“, testováno proti oficiálním vektorům, únik přes časování je u rádia
  irelevantní. Riziko je v údržbě, ne v bezpečnosti. Alternativa: systémový balík `python3-cryptography`
  (dostupnou verzi v Raspberry Pi OS ověřit před rozhodnutím).
- **Hydrawise legacy REST API:** Hunter ho stále uvádí včetně spuštění a zastavení zóny, jako lepší volbu
  pro automatizaci ale doporučuje GraphQL s OAuth 2.0; komunita už zažila odebrání některých v1 endpointů.
  Oficiální datum ukončení nebylo nalezeno (ověřeno 25. 10. 2026). API klíč chodí v URL (query parametr).
  Abstrakce `_provider` je dobrý základ, jen je postavená na řetězci `"hydrawise"`.
- **32bitový MAC a párování bez autentizace** jsou v [bezpecnost.md](bezpecnost.md) vědomě přijatá
  rizika; zdůvodnění je v pořádku.

## Doporučené pořadí

1. Nálezy 1 a 2 – malé změny s velkým dopadem na bezpečnost ventilu a dat.
2. Nález 3 – automatická obnova DB ze zálohy.
3. Nálezy 4 a 5 – refaktor `Core` a překladů, až budou přibývat funkce.

## Zdroje

- [Hunter Industries – Hydrawise API Information](https://hunterindustries.com/support/hydrawise-api-information) (ověřeno 25. 10. 2026)
- [openHAB – Hydrawise binding, odebrané funkce API](https://community.openhab.org/t/bug-hydrawise-binding-scheduled-runnable-ended-with-an-exception/98013)
- [pydrawise – omezení v1 REST API](https://pydrawise.readthedocs.io/en/latest/reference/legacy/)
