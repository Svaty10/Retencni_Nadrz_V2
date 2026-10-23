# Changelog

Formát dle [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), verze dle [SemVer](https://semver.org/).
Postup vydání nové verze: [docs/vydani.md](docs/vydani.md).

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
