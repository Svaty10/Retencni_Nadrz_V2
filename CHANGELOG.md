# Changelog

Formát dle [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), verze dle [SemVer](https://semver.org/).
Postup vydání nové verze: [docs/vydani.md](docs/vydani.md).

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
