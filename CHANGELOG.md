# Changelog

Formát dle [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), verze dle [SemVer](https://semver.org/).

## [Unreleased]

### Instalace
- Instalace jedním příkazem: `curl -fsSL …/system/bootstrap.sh | sudo bash` (sám doinstaluje git, stáhne repozitář do `/opt/retencka-src`).
- SPI se zapne bez restartu; restart jen když je opravdu potřeba (automaticky po 10 s, `--no-reboot` vypne).
- Nastaví zemi pro Wi-Fi (`--wifi-country`, výchozí CZ) a odblokuje Wi-Fi – na čerstvém Pi OS jinak hotspot nenaběhl.
- Na konci souhrn (adresa, hotspot, heslo) a kontrola OK/CHYBA; opakovaná instalace bez okamžiku s neplatnou verzí, odmítne downgrade (`--force`), doplní nové klíče do `/etc/default/retencka`.

### Opravy – hub
- Párování: automatické spárování prvního čidla jen jednou (odpárované čidlo ani cizí čidlo se do prázdného hubu samo nespáruje); podvržený požadavek nemůže dokončit odpárování; čekající klíče mají platnost.
- Ochrana proti přehrání: poslední čítač rámce se ukládá k čidlu (přežije restart i promazání dat).
- Dopouštění: nepovedené zavření ventilu se opakuje; chyba u jedné nádrže nezastaví kontroly ostatních; porucha ventilu (24h blokace) jen po doběhnutí časovače; bez platné hladiny se dopouštění nespustí.
- Kalibrace se dvěma body se stejným napětím už neztrácí měření.
- Upozornění z vláken údržby (málo místa) se doručí do Telegramu.
- Telegram: vlákno se po chybě spojení samo obnoví; `dopustit` kontroluje rozsah 1–100 %.
- Přihlášení: zámek po neúspěšných pokusech platí pro dvojici účet + IP (útočník nezamkne admina), stejná odpověď pro neexistující i zamčený účet; omezení pokusů i při změně hesla; rezervovaná jména `system*`.
- HTTP: přísnější Content-Length, chybné dotazy → 400 místo pádu spojení, chyba během stahování exportu nepoškodí odpověď, ošetřené přetečení čísel.
- Počasí: při výpadku internetu se nestahuje při každém obnovení stránky.

### Opravy – systém
- Pi připojené kabelem nespouští hotspot; `retencka.local` ukazuje na adresu aktivního rozhraní.
- Identita zařízení se nevytvoří z nulové MAC adresy (dříve hrozilo `retencka-0000`).
- Aktualizace: správné řazení předběžných verzí (`1.2.0-beta1` < `1.2.0`), kontrola verze v balíku, návrat i systemd jednotek při rollbacku, port kontroly zdraví z konfigurace; release s pomlčkou je *pre-release*.
- `MemoryMax` služby 128 MB (rezerva pro 64bit Python), `DeviceAllow` podle třídy zařízení.
- `.gitattributes`: shellové skripty a konfigurace vždy s LF.

### Opravy – čidlo
- EEPROM se při startu nezapisuje (opakované resety slabé baterie ji neopotřebují); úložiště ve dvou kopiích, takže výpadek během zápisu nesmaže spárování. Zpětně kompatibilní.
- Omezená čekání na RTC/WDT, SHT40 si při chybě nechá původní adresu, nepovedené uspání CC1101 vede k novému init.
- `make test` funguje i na Windows; `nahraj.ps1` kontroluje programátor před stažením firmwaru.

## [1.0.0] - 2026-10-13

První veřejné vydání. Nahrazuje původní prototyp (ESP32 + Flask).

### Čidlo (`uzel/`)
- ATtiny1624 + CC1101 (433,92 MHz) + SHT40 + hydrostatická sonda, napájení 3× Li-FeS2 (~12 µA průměrně, výdrž 10+ let).
- Měření každých 5 min, při rychlé změně hladiny každou minutu; ořezaný průměr 512 vzorků sondy.
- Šifrovaný a podepsaný přenos (AES-128-CTR + CMAC, ochrana proti přehrání), párování přes X25519, odpárování jen s přístupem k hubu i k čidlu.

### Hub (`server/`, Raspberry Pi Zero 2 W / 3 / 4)
- Jeden proces v Pythonu jen se standardní knihovnou, SQLite (WAL), ~25 MB RAM.
- Až 16 čidel, každé s vlastní nádrží, kalibrací, dopouštěním a grafy.
- Dashboard: hladina s úseky dopouštění a přítoku z deště, spotřeba, dopouštění, klima, počasí, baterie a signál; přehled všech nádrží; mobilní zobrazení.
- Dopouštění přes Hunter Hydrawise: ruční i automatické, čekání na předpovězený déšť, uzavřená smyčka s kalibrací průtoku, pojistky (časovač ventilu, výpadek dat, porucha ventilu).
- Home Assistant API (port 8081, token) a generátor YAML.
- Bezpečnost: scrypt, session na serveru, CSRF, rate limiting, striktní CSP, role admin/uživatel, audit log.
- Zálohy: noční lokální, stažení a nahrání (sloučení) přes web.

### Telegram a vzdálený servis
- Telegram bot (vlastní pro každou instalaci): upozornění, stav, graf hladiny, dopouštění na dálku,
  servisní příkazy (diagnostika, log, restart, reboot, aktualizace) podle role propojeného účtu.
- Vzdálený servis přes NetBird (volba instalace), přístup jen se souhlasem zákazníka (24 h / trvale / vypnuto).
- Cloudflare Tunnel pro nasazení s vlastní doménou.

### Systém (`system/`)
- Wi-Fi hotspot s captive portálem, mDNS `retencka.local`, podepsané aktualizace z GitHubu s automatickým návratem, HW watchdog, volitelně read-only systém.
