# Bezpečnostní revize

Revize celého projektu (hub `server/`, root pomocníci `system/`, firmware `uzel/`) nad verzí 1.7.0
(commit `d92f10f`), oprava ve verzi 1.7.1. Projekt neobsahuje žádné PHP – webový server je vlastní
asyncio HTTP server v Pythonu (jen standardní knihovna), takže „PHP exploity“ se ho netýkají.

Postup: ruční revize zaměřená na hranici oprávnění (aplikace `retencka` ↔ root), párování a kryptografii,
přihlášení / session / CSRF / role, zálohy, aktualizace, tovární reset, Telegram
a odchozí volání. Plný automatický sken pluginem *claude-security* v této relaci nešel spustit (potřebuje
nástroj Workflow); lze ho pustit později přes `/claude-security`.

Model útočníka: (a) kdokoli v dosahu rádia 433 MHz, (b) kdokoli v LAN / přes tunel, (c) přihlášený
uživatel bez role admin, (d) **kompromitovaná aplikace** (běží jako neprivilegovaný `retencka`) – root
pomocníci jí nesmí dát víc práv.

## Opravené nálezy

| # | Závažnost | Kde | Problém | Stav |
|---|---|---|---|---|
| 1 | **vysoká** | `system/bin/retencka-identity.sh` | Služba běží jako root při každém startu a do `data/setup_code` (adresář patří aplikaci) zapisovala `printf >`, pak `chown retencka` a `chmod`. Všechno následuje symlink: kompromitovaná aplikace mohla podstrčit `setup_code → /etc/passwd`, po restartu by root soubor přepsal a předal ho uživateli `retencka` → **eskalace na roota**. Adresář místo souboru navíc zablokoval službu (pád při `set -e`). | opraveno: soubor se připraví v `/var/lib/retencka` (patří rootovi, stejný oddíl) a `mv -T` ho přejmenuje na místo – rename symlink nahradí, nikdy jím neprojde; podstrčený adresář se smaže (když ho aplikace mezitím vytvoří znovu, služba to jen zaloguje a nespadne); čte se jen běžný soubor s časovým limitem. Testy `test_root_helpers.IdentitySetupCode` |
| 2 | střední | `system/install.sh` (NetBird) | Stejný vzor pro `data/servis.json` (`printf >` + `chown`) při přeinstalaci, kdy aplikace už běží. | opraveno stejně (temp v `$BASE` + `mv -fT`); test `InstallScript` (jen kontrola textu skriptu, ne běh) hlídá, aby install.sh nikdy nezapisoval / nechownoval přímo do `$DATA/` |
| 3 | nízká | `system/bin/retencka-control.py` | `wanted()` četlo `servis.json` přes `read_text()` (následuje symlink, na FIFO by root visel); adresář pojmenovaný `control-*` shodil celý pomocník (nevykonal servis ani kontrolu certifikátu a `.path` jednotka by ho spouštěla dokola). | opraveno: `read_small()` (bez symlinků, jen malý běžný soubor), podstrčený adresář se smaže `rmtree` (nesleduje odkazy), chyby jen do logu. Testy `ControlHelper` |
| 4 | nízká | `server/retencka/core.py` `import_backup` | Rozbalení gzipu bez limitu – malá „gzip bomba“ (jen admin, chráněno CSRF) mohla zaplnit SD kartu. Poškozený deflate proud (`zlib.error`) propadl jako chyba 500; na Windows po neplatném souboru zůstal `import.tmp`. | opraveno: limit 4 GiB a zároveň volné místo nad rezervou `LOW_DISK_MB`, `zlib.error` → srozumitelná chyba, kontrolní spojení se vždy zavře. Testy `test_backup` |
| 5 | střední | `server/retencka/core.py` `_on_frame` | Jeden náhodně trefený 32bitový MAC (1 : 4·10⁹) s vysokým čítačem (`ctr = 0xFFFFFFFF`) by přepsal `last_ctr` a všechny další legitimní rámce čidla by hub zahazoval jako replay. | opraveno: skok čítače o > `CTR_MAX_STEP` (4096 = 4× `CTR_RESERVE`) se uloží jako kandidát a potvrdí až druhým navazujícím rámcem (šance dvou náhodných zásahů = 1 : 2⁶⁴), bez časového omezení spárování. Test `test_huge_counter_jump_needs_confirmation` |
| 6 | nízká | `system/bin/retencka-portal.py`, `retencka-control.py`, `server/retencka/httpserver.py` | Captive portál povoloval SSID začínající `-` (předávané do `nmcli`), `control-reboot` nekontroloval visící symlink (`is_symlink()`), HTTP server neměl per-IP limit spojení a přepisoval vícenásobné hlavičky `Cookie:`. | opraveno: SSID s úvodní `-` odmítnuto, `is_symlink()` sjednoceno, přidán `MAX_PER_IP_CONNECTIONS = 24` a spojování `Cookie` hlaviček přes `; `. Testy `test_root_helpers`, `test_server` |

Testy se symlinky a FIFO se na Windows přeskočí (chybí oprávnění) – samotnou opravu č. 1 tak dokazují až
na Linuxu, kde běží v CI před každým vydáním.

## Ponechaná rizika (vědomě, bez změny)

| Závažnost | Oblast | Popis a zdůvodnění |
|---|---|---|
| nízká | NetBird setup key | `retencka-netbird-install.sh` předává klíč `netbird up --setup-key …` v příkazové řádce, takže je po pár sekund vidět v `ps` pro lokální uživatele. Na hubu jsou jen systémové účty a klíč je jednorázový / časově omezený. Klíč se neloguje ani neukládá. |
| nízká | MAC rádiových rámců 32 bitů | DATA rámec nese zkrácený CMAC (4 B). Padělání = 1 : 4·10⁹ na pokus a jeden rámec ~ desítky ms vysílání; úspěch by vložil jediné falešné měření, ne klíč. Délka rámce je dána energetickým rozpočtem čidla. |
| nízká | Párování bez autentizace a okno „znovu spárovat“ (X25519) | Útočník v dosahu rádia může během 15minutového okna párování odpovědět podvrženým `PAIR_REQ` dříve než skutečné čidlo a následně podvrhovat hladinu vody (ovládat ventil). Riziko a jeho zmírnění (RSSI, varování při více klíčích, preference nejsilnějšího signálu v `PENDING_MAX`) jsou popsány níže. |
| nízká | Entropie čidla | ATtiny1624 nemá TRNG; klíč vzniká z šumu ADC, jitteru hodin, sériového čísla a poolu z EEPROM přes AES-CMAC. Pro tuto hrozbu dostatečné. |
| info | Home Assistant (MQTT) | Hub je klient brokeru, žádný port neotevírá. Na portu 1883 jde heslo i data v LAN nešifrovaně (volitelně TLS). Kdo smí publikovat do `retencka/<uid>/+/refill/set`, může dopouštět – jde vypnout (*Dopouštění z Home Assistantu*), dopouštění má i tak své pojistky. Heslo se v UI nezobrazuje ani zčásti. `/health` naslouchá jen na 127.0.0.1. |
| info | První přihlášení po HTTP | Než existuje účet, funguje web i po http (heslo ze štítku = heslo hotspotu). Vydá jen 15minutový lístek na vytvoření vlastního admina. |
| info | Import zálohy | Záloha je SQLite soubor od admina; ověří se `quick_check`, tabulky a verze, sloupce se berou ze schématu hubu (žádné SQL ze souboru). Plný import obnoví i nastavení a účty – záměr. |

### Směrové riziko v okně „znovu spárovat“ a jeho zmírnění

Při otevření 15minutového okna pro párování nového čidla nebo po stisku „Znovu spárovat“ existuje směrové rádiové riziko: útočník v dosahu 433 MHz může odeslat falešný `PAIR_REQ` s očekávaným ID dříve než legitimní čidlo po vložení baterie. Pokud by byl klíč potvrzen, útočník získá možnost podvrhovat měření hladiny (např. hlásit prázdnou nádrž a vyvolat nežádoucí dopouštění z řadu).

**Zmírnění zavedená v systému (1.13.1):**
- **Zobrazení RSSI v reálném čase:** V tabulce čekajících i detekovaných požadavků na párování webové rozhraní zobrazuje sílu signálu (dBm i %). Při párování v těsné blízkosti hubu má legitimní čidlo silný signál; podezřele slabý signál varuje před vzdáleným útočníkem.
- **Detekce a varování při vícenásobných veřejných klíčích:** Pokud pro dané ID hub zachytí více různých veřejných klíčů (`pub_count > 1`), v UI se zobrazí výrazné červené varování `· pozor: X klíče!`. Správce vidí, že o registraci ID soupeří více vysílačů, a párování může zrušit.
- **Preference nejsilnějšího RSSI ve frontě (`PENDING_MAX`):** Backend při naplnění kapacity čekajících klíčů upřednostňuje kandidáta s nejsilnějším signálem, což brání DoS útoku vytlačením legitimního klíče slabými podvrženými pakety.
- **Časový limit 15 minut:** Okno se uzavře automaticky po 15 minutách a párování vyžaduje přihlášeného administrátora.

## Zkontrolováno bez nálezu

- **Přihlášení a session**: scrypt s omezením parametrů, konstantní čas i pro neznámé jméno, zámek (jméno, IP) + exponenciální zdržení IP, náhodný token (v DB jen SHA-256), obnova session při přihlášení (bez fixace), cookie HttpOnly + SameSite=Lax (web v LAN po http, bez Secure; za HTTPS tunelem – `X-Forwarded-Proto: https` z loopbacku – se Secure).
- **CSRF**: kontrola `Origin` výhradně proti `Host` (`X-Forwarded-Host` se ignoruje) + povinný `X-CSRF-Token` session pro všechny změny; ostatní proxy hlavičky jen z loopbacku.
- **Role**: všechny API cesty mají `@need` / `@need("admin")` kromě přihlášení, nastavení prvního účtu a statických souborů; poslední admin nejde smazat ani degradovat.
- **HTTP server**: limity hlaviček (16 KiB / 64), odmítnutý `Transfer-Encoding` a zdvojený `Content-Length` (žádný request smuggling), limit spojení, timeouty, bezpečnostní hlavičky (CSP, frame DENY, nosniff).
- **Aktualizace**: Ed25519 podpis přes `openssl pkeyutl`, kontrola cest v archivu + `filter="data"`, jen novější verze, verze v balíku = verze vydání, snímek databáze před přepnutím a rollback (symlink, systemd jednotky i databáze) při nezdravém startu – bez snímku se aktualizace nespustí, vadná verze se nezkouší znovu; požadavky aplikace čteny bez symlinků a striktně validovány. Nové testy `InstallAndRollback` se skutečným podpisem.
- **Tovární reset**: jen admin + heslo + slovo OBNOVIT, mazání až při dalším startu před otevřením DB, symlinky se mažou, ne procházejí; ponechá `setup_code`.
- **Telegram**: jen soukromé chaty, role se ověřuje z DB u každého příkazu, jednorázové 8znakové kódy s platností a limitem pokusů, vypnutí jen s potvrzením.
- **Hydrawise / geocoding / počasí**: pevné HTTPS adresy s ověřením certifikátu (žádné SSRF – uživatel URL nezadává), parametry kódované, API klíč maskovaný v logu, omezená velikost odpovědi.
- **Rádio a firmware**: CTR nonce = ID + 32bit čítač s rezervou v EEPROM (dvě kopie, nikdy znovu použit), replay a duplikát rozlišené, odmítnutí low-order bodu X25519, MAC v konstantním čase, UNPAIR vázaný na čerstvý veřejný klíč bootu (nelze přehrát).

## Pokrytí kritických cest testy

| Oblast | Testy |
|---|---|
| Párování (všechny scénáře z architektura.md 3.9 + replay po restartu) | `test_pairing_scenarios`, `test_core_fixes.PairingTest/UnpairTest` |
| Dopouštění a bezpečnostní stopky (cíl, strop %, strop minut, výpadek dat, časovač, opakované zastavení, jeden ventil = jedna nádrž) | `test_core_fixes.Refill*`, `test_refill` |
| Migrace DB v1 → nejnovější, z každé mezi-verze, přetočená verze, rollback chybné migrace | `test_migrations` |
| Záloha + obnova (stažení, noční kopie, nekomprimovaná DB, opakovaný import, data bez účtů, vadné soubory, novější verze, gzip bomba) | `test_backup`, `test_pairing_scenarios.S1` |
| Aktualizace + rollback (podpis, cesty, verze, zdravý / nezdravý start) | `test_updates` |
| Tovární reset (celý cyklus až po první přihlášení, poškozená značka, symlinky) | `test_admin.FactoryResetFiles`, `AdminServerTest.test_9_factory_reset` |
| Root pomocníci proti podstrčeným souborům | `test_root_helpers` |
