# Bezpečnostní kontrola 2026-10

Hloubková kontrola celého repozitáře nad verzí 1.12.1 (`main` @ `945056e`, 25. 10. 2026). Navazuje na
předchozí revizi v [bezpecnost.md](bezpecnost.md) a používá stejný model útočníka: (a) kdokoli v dosahu
rádia 433 MHz, (b) kdokoli v LAN / přes tunel, (c) přihlášený uživatel bez role admin,
(d) **kompromitovaná aplikace** (běží jako neprivilegovaný `retencka`) – root pomocníci jí nesmí dát
víc práv.

Rozsah: frontend (`server/static`), backend (`server/retencka`), root pomocníci a aktualizace
(`system/`), firmware čidla (`uzel/`), rádiový protokol, návrh a architektura.

Postup: čtyři nezávislé ruční prověrky (backend, frontend, systémová vrstva, rádio a kryptografie),
každý kandidát na nález pak samostatně ověřen proti kódu. Kód z repozitáře se nespouštěl.
Hlásí se jen nálezy s jistotou ≥ 8/10; DoS, chybějící zpevnění bez konkrétního zneužití a teoretické
závody se nehlásí.

## Nálezy

| # | Závažnost | Kde | Problém | Stav |
|---|---|---|---|---|
| 1 | **vysoká** | root pomocníci (`system/bin/`) | kompromitovaná aplikace může získat práva roota – detaily v soukromém advisory `GHSA-pc46-j66c-c3w2` | **neopraveno** |

### 1. Eskalace z aplikace na roota v root pomocnících

- **Kategorie:** eskalace oprávnění
- **Jistota:** 9/10
- **Stav:** neopraveno. Podrobnosti, postup útoku a oprava jsou v soukromém GitHub Security Advisory
  `GHSA-pc46-j66c-c3w2` (vidí ho jen správci repozitáře). Detaily se zveřejní po vydání opravené verze,
  v souladu s [SECURITY.md](../.github/SECURITY.md).
- **Předpoklad útoku:** kompromitovaná aplikace (útočník d) – sama o sobě nezpřístupňuje hub zvenčí.

## Pod prahem – ke zvážení jako zpevnění

| Oblast | Kde | Popis | Doporučení |
|---|---|---|---|
| `X-Forwarded-Host` z tunelu | `server/retencka/web.py:216` | kontrola `Origin` mu věří u spojení z loopbacku, tedy i u provozu přes Cloudflare tunel; povinný CSRF token ale zůstává | porovnávat jen s `Host` |
| Cookie bez `Secure` | `server/retencka/web.py:270` | `rt_sid` je bez `Secure` i za HTTPS tunelem (vědomá volba kvůli HTTP v LAN) | nastavit `Secure`, když požadavek přišel přes tunel |
| Regexy s `$` | `server/retencka/updates.py:26`, `server/retencka/httpserver.py:36` | `$` projde i s koncovým `\n`; dnes nezneužitelné | `re.fullmatch` nebo `\Z` |
| Převzetí čidla v okně „znovu spárovat“ | `server/retencka/core.py` (`responder`, `_commit_pairing`) | útočník v rádiovém dosahu může během 15 min odpovědět dřív než skutečné čidlo a pak dodávat hladinu (= ovládat ventil); spadá pod přijaté riziko „párování bez autentizace“, ale tento směr není v [bezpecnost.md](bezpecnost.md) popsán | doplnit do dokumentace; v UI ukázat RSSI a počet různých veřejných klíčů pro dané ID |
| `PENDING_MAX = 4` | `server/retencka/core.py:46` | podvržené žádosti o párování mohou vytlačit skutečný čekající klíč (jen DoS párování); komentář v kódu tvrdí opak | opravit komentář, případně preferovat klíč s nejsilnějším RSSI |

## Zkontrolováno bez nálezu

| Oblast | Výsledek |
|---|---|
| Frontend | žádná XSS: CSP `script-src 'self'` bez `unsafe-inline` / `unsafe-eval`; `esc()` u všech `innerHTML` s daty ze serveru nebo třetích stran (názvy čidel, uživatelé, audit, Telegram, zóny Hydrawise); `i18n.js` mění jen `nodeValue` a atributy; poznámky k vydání přes `textContent`, odkazy jen `https:` |
| Backend | všech 62 cest má správnou roli (`@need` / `@need("admin")`); CSRF = kontrola `Origin` + token v konstantním čase; session 256 bitů, v DB jen SHA-256, při přihlášení nová; SQL všude parametrizované, názvy sloupců z importu zálohy ze schématu hubu; HTTP parser odmítá smuggling; statické soubory chráněné proti `../`; ne-adminům neunikají tajné hodnoty (Hydrawise klíč, tokeny, hashe) |
| Rádio a kryptografie | nonce se neopakuje (monotónní čítač s rezervou v EEPROM); CMAC pokrývá typ, ID, čítač i šifrovaný text; přesné kontroly délek v Pythonu i C; X25519 se správným ořezáním a odmítnutím low-order bodů; anti-replay včetně potvrzení velkého skoku čítače (oprava č. 5 v [bezpecnost.md](bezpecnost.md) platí); UNPAIR vázaný na čerstvý veřejný klíč |
| Firmware | kontroly délek přijatých rámců (`cc1101_send`, `proto_check_unpair`, buffer `ack[40]`) |
| Telegram | jen soukromé chaty; role z DB u každého příkazu; propojovací kód 40 bitů, 10 min, 5 pokusů; dopouštění jen pro admina (shodně s webem) |
| OTA aktualizace | Ed25519 podpis nad stejnými bajty, které se rozbalují; downgrade zablokovaný; bezpečné rozbalení (`filter="data"`, kontrola cest); aplikace neovlivní zdroj ani URL |
| Captive portál | SSID a heslo do `nmcli` jako argumenty, SSID s úvodní `-` odmítnuto, výstup escapovaný |
| `bootstrap.sh`, GitHub Actions | bez nálezu; workflowy se nespouští z nedůvěryhodného vstupu (žádný `pull_request_target`) |
