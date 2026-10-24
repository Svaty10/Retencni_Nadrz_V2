# Changelog

Formát dle [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), verze dle [SemVer](https://semver.org/).
Postup vydání nové verze: [docs/vydani.md](docs/vydani.md).

## [1.9.0] - 2026-10-24

### Přidáno
- **Firmware čidla 11 – rychlá kontrola hladiny každou minutu:** mezi vysíláními (1× za 5 min) se čidlo každých 60 s krátce probudí a změří jen sondu (bez teploty, baterie a rádia). Když se hladina proti poslední odeslané hodnotě změní aspoň o 10 mV (≈ 6 mm) ve **dvou kontrolách po sobě**, hned odešle měření a přejde na minutový režim – začátek deště nebo odběru je v aplikaci do 2 minut místo až 5. Jedna náhodná výchylka nic nespustí. Průměrný odběr ~12 → ~31 µA, odhad výdrže 3× AA Li-FeS2 ~8 let, alkalických ~5 let (tabulka v README). **Firmware se neaktualizuje rádiem – čidlo je potřeba přeflashovat** (`uzel/3-aktualizace-firmwaru.cmd`), spárování zůstane.
- Odhad výdrže baterie počítá u čidel s firmwarem ≥ 11 s rychlými kontrolami (1440 − počet vysílání za den, nové nastavení `node_check_mAs` = 1,4 mAs).
- Popisek grafu hladiny ukazuje i napětí sondy v mV.

### Změněno
- Graf **Baterie a signál** má osu v **procentech** podle křivky typu baterie (Li-FeS2 / alkalické); popisek ukazuje „XX % · 3,84 V (při vysílání −55 mV)“. Historie (`/api/history`) má nové řady `bat_pct`, `sag_mv` a `probe_mv`.

## [1.8.1] - 2026-10-24

### Opraveno
- **Jméno uživatele nerozlišuje velikost písmen ani u diakritiky:** „MATĚJ“, „Matěj“ i „matěj“ jsou jeden účet (dříve se u Č, Ě, Š … přihlášení nezdařilo a šlo vytvořit druhý účet lišící se jen velikostí písmen). Přihlášení, session i Logování vždy používají jméno tak, jak bylo založeno. Zámek po chybných pokusech platí pro jméno v jakékoli podobě.

## [1.8.0] - 2026-10-24

### Změněno
- **HTTPS se už nevynucuje:** `http://retencka.local` funguje dál jako dřív, `https://` je k dispozici vedle. Přesměrování z http na https zapne administrátor přepínačem **Vždy používat HTTPS** v kartě *Zabezpečené připojení (HTTPS)* – až mají certifikát hubu nainstalovaný jeho zařízení (zapnout jde jen ze stránky otevřené přes https://). Huby s 1.7.x tak po aktualizaci nikoho nepřekvapí varováním prohlížeče.

## [1.7.1] - 2026-10-24

### Opraveno (bezpečnost)
- **Služba identity zařízení** (běží jako root) už nezapisuje heslo ze štítku do datového adresáře aplikace přes případný podstrčený symlink: soubor se připraví mimo něj a přejmenuje na místo. Dříve mohla kompromitovaná aplikace touto cestou získat práva roota. Stejně opraven zápis `servis.json` v `install.sh`.
- Root pomocník `retencka-control` čte požadovaný servisní režim bez symlinků a s omezenou velikostí; podstrčený adresář `control-*` ho už nezastaví.
- Import zálohy: rozbalení gzipu je omezené (4 GiB a volné místo na kartě), poškozený soubor vrátí srozumitelnou chybu místo chyby serveru a nezůstanou po něm dočasné soubory.

### Přidáno
- Bezpečnostní revize a přehled zbylých rizik: [docs/bezpecnost.md](docs/bezpecnost.md).
- Testy kritických cest: migrace databáze z každé verze, záloha a obnova, instalace aktualizace se skutečným podpisem a rollback, bezpečnostní limity dopouštění, tovární reset, přehrání zaznamenaného rádiového rámce po restartu.

## [1.7.0] - 2026-10-24

### Přidáno
- **HTTPS bez domény a internetu:** hub si vytvoří vlastní certifikační autoritu (10 let, smí podepsat jen názvy `.local` a místní IP adresy) a certifikát webu pro `retencka.local`, `retencka-xxxx.local` a své IP; ten se obnovuje sám (30 dní před koncem a při změně IP/názvu). Web běží na `https://` (port 443), port 80 přesměruje – kromě prvního nastavení, stažení certifikátu a hotspotu. Certifikát hubu jde stáhnout v *Nastavení → Obecné → Zabezpečené připojení (HTTPS)* i na přihlašovací stránce, s návodem pro Android, iOS, Windows a macOS. Po instalaci certifikátu funguje v prohlížeči i zjištění polohy. API pro Home Assistant zůstává http na :8081.
- **Obnova továrního nastavení** (*Obecné → Systém*, admin): nabídne zálohu, chce napsat OBNOVIT a heslo; smaže data, nastavení, uživatele, klíče čidel a zálohy, ponechá identitu zařízení, heslo ze štítku a certifikát HTTPS.
- Správa uživatelů: změna role a nové heslo jinému uživateli v dialogu s potvrzením; vždy zůstane aspoň jeden administrátor.
- Krátké popisy bloků na přehledu, nápověda „?“ u méně zřejmých polí nastavení (funguje i dotykem).
- Ukazatel hladiny se stupnicí a jemnou animací hladiny (vypnutá při „omezit pohyb“).
- Akce v dialozích se zadáním a potvrzením: ruční dopouštění, zastavení, přidání čidla, mazání uživatele.
- Varování „málo místa na kartě“ na přehledu a v kartě Systém; v kartě Záloha a data vysvětlení automatických denních záloh (posledních 7 na kartě) a proč stahovat zálohu mimo zařízení.
- Logování: výsledky aktualizací (dostupná verze, nainstalováno, vráceno, chyba – stejná chyba jen jednou), vydání certifikátu HTTPS, obnova továrního nastavení, zrušené párování.

### Změněno
- Záložka nastavení a volba v liště se jmenují **Všechna čidla** (dříve Všechny nádrže).
- Logování je kompaktnější (jeden řádek na záznam).
- Session cookie se po přechodu na HTTPS přesune do `__Host-` cookie s příznakem Secure; HSTS se s vlastní CA neposílá (web zůstane dostupný i ze zařízení bez certifikátu).

## [1.6.0] - 2026-10-24

### Přidáno
- **Volba motivu** v menu účtu: *Auto* (podle zařízení) / *Světlý* / *Tmavý*. Uloží se u účtu, takže platí na všech zařízeních po přihlášení; grafy se přebarví hned.

### Opraveno
- Migrace databáze přeskočí přidání sloupce, který už existuje (ručně obnovená databáze).

## [1.5.0] - 2026-10-24

### Přidáno
- **Restart a vypnutí** v menu účtu (admin): restart aplikace, restart zařízení, vypnutí zařízení – s potvrzením a automatickým připojením po restartu. Telegram: `vypnout`.
- **Mazání naměřených dat** (*Obecné → Záloha a data*): pro jednu nebo všechny nádrže, v rozsahu od–do nebo vše; nejdřív ukáže, kolik záznamů smaže. Záznamy (audit log) zůstávají.
- **Grafy si pamatují zvolený rozsah** (24 h / 7 dní / … / vlastní) – i po obnovení stránky a přepnutí nádrže.

### Opraveno
- **Nastavení na mobilu skákalo nahoru a nešlo do něj psát** (při posouvání a otevření klávesnice se karty přeskládávaly).
- Po přepnutí záložek v Nastavení se karty občas nahrnuly do jednoho sloupce a bylo nutné stránku obnovit.
- Karta *Dopouštění* u nádrže: ventil a zóna v jedné rovině, zóna s tlačítkem pro načtení názvů, pravidla v pravidelné mřížce, ruční nastavení zvlášť.

## [1.4.0] - 2026-10-23

### Změněno
- **Nastavení ve třech záložkách:** *Obecné* (poloha, Hunter Hydrawise, Telegram a servis, Home Assistant, záloha a data, systém), *Všechny nádrže* (čidla, párování, výchozí nastavení nádrží) a záložka pro každou nádrž (nádrž, kalibrace, sonda, baterie, upozornění, dopouštění). Každá skupina je samostatná karta s nadpisem a popisem, nahoře rychlá navigace.
- **Kapacita baterie** se nastavuje u nádrže (výchozí hodnota ve *Výchozí nastavení nádrží*); dosavadní hodnota se převede.

### Přidáno
- **Poloha bez souřadnic:** vyhledání obce / adresy (Open-Meteo), případně poloha zařízení (jen přes HTTPS). Souřadnice zůstávají v pokročilém nastavení.
- **Výběr zóny Hydrawise:** *Načíst zóny z Hydrawise* u nádrže – zóna se vybere podle jména, relay_id netřeba hledat. Klíč účtu je v *Obecné* s tlačítkem *Otestovat připojení*.
- Pole Zóna přijme jen číslo a vložený API klíč odmítne s vysvětlením, kam patří.

## [1.3.0] - 2026-10-23

### Přidáno
- **Archiv klíčů odebraných čidel:** čidlo odebrané z hubu (*Odebrat bez čidla*) jde znovu spárovat bez programátoru – v *Čidla v dosahu* tlačítko *Spárovat znovu*, hub čidlo uvolní podepsaným UNPAIR starým klíčem (stačí vložit baterii). Smazání EEPROM (`4-smazat-parovani.cmd`) je potřeba jen u hubu bez zálohy. Archiv je součástí zálohy.
- Popis všech stavů párování a odpárování s koncovými situacemi v `docs/architektura.md` (3.9).

### Opraveno
- Ztracená první data po spárování: hub nový klíč drží 24 h (i přes restart) a čidlo se nestane „cizím“.
- Restart hubu během odpárování: odpárování se dokončí (čas odeslání UNPAIR se ukládá); pozdní potvrzení z čidla se už nezahodí. Zrušení párování nezruší probíhající odpárování.
- Čidlo, které ztratilo klíč (smazaná EEPROM), je v *Čidla v dosahu* označené „čidlo ztratilo klíč“ s *Znovu spárovat* – historie i nastavení zůstanou.
- Každá situace párování / odpárování má test se simulovaným čidlem (`server/tests/test_pairing_scenarios.py`).

## [1.2.3] - 2026-10-23

### Opraveno
- **Smazané čidlo se po restartu nebo aktualizaci vracelo** jako „Retenční nádrž“: migrace z první jednonádržové verze se spouštěla při každém startu s prázdným seznamem čidel. Proběhne teď jen jednou a starý údaj `paired_node_id` se smaže. (Nešlo o automatické párování – nové čidlo se bez potvrzení nespáruje.)

## [1.2.2] - 2026-10-23

### Opraveno
- Varování „Čidlo neposílá data“ se už neukazuje, když v hubu žádné čidlo není.

### Přidáno
- `uzel/4-smazat-parovani.cmd`: smaže párování v čidle (EEPROM) pro čidlo, které hub ukazuje jako „cizí čidlo“ (odebrané z hubu nebo z jiného hubu bez zálohy); firmware zůstane.

## [1.2.1] - 2026-10-23

### Opraveno
- **Hub nepřijímal data z čidel** („Přijímač: nedostupný“): služba neměla přístup k SPI rádia – povolení zařízení `DeviceAllow` uvádělo třídu `spidev`, ovladač se ale hlásí jako `spi` (chyba od 1.0.0).

## [1.2.0] - 2026-10-23

### Přidáno
- **Aktualizace v okně:** *Zkontrolovat aktualizace* jen zjistí novou verzi a ukáže seznam změn; instaluje se až po potvrzení, s průběhem (stahuji → ověřuji podpis → instaluji → restartuji) a výsledkem. Přepínač „Instalovat automaticky (v noci)“ a kanál Stabilní / Beta přímo na webu; dostupná verze je označená u ikony účtu. Telegram `aktualizovat` nejdřív zkontroluje a nabídne instalaci.
- **Nastavení podle nádrží:** přepínač *Všechny nádrže* / jednotlivé nádrže. Společné věci a nová karta *Výchozí nastavení nádrží* (baterie, upozornění, sonda, dopouštění, čekání na déšť, průtok); u nádrže lze hodnotu změnit na vlastní („vlastní“ / „↺ výchozí“) – změna výchozích ji nepřepíše. Stávající hodnoty se při aktualizaci zachovají.
- **Telegram z webu:** průvodce založením bota (BotFather → token → Ověřit a uložit) v kartě Telegram.
- **Vzdálený servis z webu:** instalace a připojení NetBirdu zadáním setup key (klíč se nikde nezobrazí ani nezaloguje).

### Změněno
- Karty v Nastavení se skládají do sloupců podle výšky a končí v jedné rovině.
- Návod k programování čidla: odpor 4,7 kΩ, čidlo při programování napájené z Arduina Una (3,3 V) nebo z baterie – napájení připojit před drátem UPDI.

## [1.1.2] - 2026-10-23

### Změněno
- Nádrž bez nastaveného ventilu neukazuje nic o dopouštění (stav, tlačítka, graf „Dopouštění z řadu“, úseky v grafu hladiny, údaje v přehledu, senzor v YAML pro Home Assistant); rozložení se přeskládá bez mezer.
- Výraznější grafy: kontrastní a navzájem odlišené barvy řad (≥ 5:1 proti pozadí ve světlém i tmavém režimu), silnější čáry, slabší výplň a mřížka, čitelnější popisky, tečka u poslední hodnoty.

### Opraveno
- Nastavení: karty se skládají bez prázdných míst (2 sloupce, na širokém monitoru 3), sjednocené tabulky čidel.
- Instalace jedním příkazem (`curl … | sudo bash`) potichu skončila, aniž by cokoli nainstalovala (`system/bootstrap.sh` odpojil vstup, ze kterého bash četl sám sebe). Skript se stahuje přímo z `main`, oprava platí hned.

## [1.1.1] - 2026-10-23

### Opraveno
- Dashboard: hodnoty vedle nádrže ve 3 sloupcích bez prázdného místa; karty v přehledu všech nádrží se roztáhnou na celou šířku.
- Graf dopouštění bez dopouštění neukazuje osu „1 L, 1 L, 0 L“.
- Záznamy o Telegramu a vzdáleném servisu mají vlastní ikonu a název.

### Dokumentace
- Nové snímky webu v README (dashboard, přehled, mobil, tmavý režim, záznamy), aktuální cesty v menu.

## [1.1.0] - 2026-10-23

### Přidáno
- Typ baterie u každého čidla (lithiové Li-FeS2 / alkalické): vlastní vybíjecí křivka, kapacita a samovybíjení – u alkalických správná procenta i odhad výdrže, žádné předčasné varování.
- Menu účtu vpravo nahoře (Můj účet, Nastavení, Správa uživatelů, Logování, Odhlásit).

### Změněno
- Nový vzhled webu: světlý i tmavý režim podle systému, systémové písmo, SVG ikony místo emoji, bez gradientů a záře.
- Rozložení využívá celou šířku obrazovky (až 1920 px): hustá mřížka na PC, 2 sloupce na tabletu, kompaktní 1 sloupec na mobilu (hladina, dopouštění a varování bez posouvání).
- Výběr rozsahu grafu jako přepínač (24 h / 7 dní / 30 dní / rok / vlastní), přepínače zap/vyp místo rozbalovacích seznamů, jednotky přímo v polích, nastavení ve více sloupcích.
- Grafy podle barev motivu, úseky dopouštění a deště jako jemné plochy.
- Pod stavem „Nedopouští“ místo „V normě“ informace, co se stane (ventil nenastaven / jen ručně / automaticky pod X %).

### Opraveno
- Vývojový server na Windows (volné místo na disku, signály).

## [1.0.0] - 2026-10-23

První veřejné vydání. Nahrazuje původní prototyp (ESP32 + Flask).

### Čidlo (`uzel/`)
- ATtiny1624 + CC1101 (433,92 MHz) + SHT40 + hydrostatická sonda, napájení 3× Li-FeS2 (~12 µA průměrně, výdrž 10+ let).
- Měření každých 5 min, při rychlé změně hladiny každou minutu; ořezaný průměr 512 vzorků sondy.
- Šifrovaný a podepsaný přenos (AES-128-CTR + CMAC, ochrana proti přehrání), párování přes X25519, odpárování jen s přístupem k hubu i k čidlu.
- Klíč v EEPROM ve dvou kopiích, při startu se nic nezapisuje: výměna baterie, výpadek napájení ani nový firmware spárování nezruší.

### Hub (`server/`, Raspberry Pi Zero 2 W / 3 / 4)
- Jeden proces v Pythonu jen se standardní knihovnou, SQLite (WAL), ~25 MB RAM.
- Až 16 čidel, každé s vlastní nádrží, kalibrací, dopouštěním a grafy.
- Párování vždy jen po potvrzení uživatelem (ID ze štítku) – čidlo se k hubu nikdy nepřipojí samo.
- Dashboard: hladina s úseky dopouštění a přítoku z deště, spotřeba, dopouštění, klima, počasí, baterie a signál; přehled všech nádrží; mobilní zobrazení.
- Dopouštění přes Hunter Hydrawise: ruční i automatické, čekání na předpovězený déšť, uzavřená smyčka s kalibrací průtoku, pojistky (časovač ventilu, výpadek dat, porucha ventilu, opakované zastavení).
- Ventil nikdy nejede podle starých dat: ruční start bez čerstvé platné hladiny počká na příští měření (max. 15 min).
- Zrušení kdykoli – před startem i za chodu, ruční i automatické; zrušená automatika se na 24 h pozastaví (tlačítko *Obnovit automatiku*, Telegram `obnovit`).
- Home Assistant API (port 8081, token) a generátor YAML.
- Bezpečnost: scrypt, session na serveru, CSRF, rate limiting a zámek pro dvojici účet + IP, striktní CSP, role admin/uživatel, audit log.
- Zálohy: noční lokální, stažení a nahrání (sloučení) přes web.

### Telegram a vzdálený servis
- Telegram bot (vlastní pro každou instalaci): upozornění, stav, graf hladiny, dopouštění na dálku,
  servisní příkazy (diagnostika, log, restart, reboot, aktualizace) podle role propojeného účtu.
- Vzdálený servis přes NetBird (volba instalace), přístup jen se souhlasem zákazníka (24 h / trvale / vypnuto).
- Cloudflare Tunnel pro nasazení s vlastní doménou.

### Systém (`system/`)
- Instalace jedním příkazem (`curl … | sudo bash`): SPI bez restartu, země pro Wi-Fi, souhrn s heslem a kontrolou; restart jen když je potřeba.
- Wi-Fi hotspot s captive portálem (při připojeném kabelu se nespouští), mDNS `retencka.local`, HW watchdog, volitelně read-only systém.
- Podepsané aktualizace z GitHubu (Ed25519) s automatickým návratem aplikace i systemd jednotek, kanály stable/beta.
