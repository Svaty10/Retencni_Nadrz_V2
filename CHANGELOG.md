# Changelog

Formát dle [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), verze dle [SemVer](https://semver.org/).
Postup vydání nové verze: [docs/vydani.md](docs/vydani.md).

## [1.14.4] - 2026-10-25

### Změněno
- **Jednotné formuláře v Nastavení:** formulář s jedním polem má tlačítka hned vedle pole (dřív u klíče Hydrawise pole jen do půlky a *Uložit* pod ním, u Home Assistanta a Telegramu vedle). Klíč Hydrawise je přes celou šířku s *Uložit* a *Otestovat připojení* vedle, *Uchovávání dat* má *Uložit* vedle počtu dní. Formuláře s více poli mají tlačítko dál pod poli.
- Snímky v dokumentaci jsou v tmavém režimu (světlý jako ukázka `docs/img/dashboard-svetly.webp`).

## [1.14.3] - 2026-10-25

### Změněno
- **Nastavení přes celou šířku stránky** (dřív nejvýš 960 px, na širokém monitoru zůstávalo vpravo prázdné místo).
- **Graf hladiny stejně vysoký jako ostatní grafy**; karta nádrže se výškou srovná s pravým sloupcem.

## [1.14.2] - 2026-10-25

### Změněno
- **Graf hladiny není menší než ostatní:** v pravém sloupci vedle nádrže má nejméně 320 px (na mobilu 280 px, ostatní grafy 260 px); když je vyšší než karta nádrže, karta se protáhne a schéma toků se v ní vycentruje.

## [1.14.1] - 2026-10-25

### Změněno
- **Přehled nádrže bez prázdného místa:** graf *Hladina vody* je nově v pravém sloupci pod doplňkovými údaji a dorovná výšku karty nádrže (dřív vedle nádrže zůstávala prázdná plocha); schéma toků se v kartě nádrže svisle vycentruje. Na úzkém displeji zůstává vše pod sebou.

### Opraveno
- Karta doplňkových údajů měla na mobilu zdvojený vnitřní okraj.

## [1.14.0] - 2026-10-25

### Přidáno
- **Schéma toků vody u nádrže:** kulatá nádrž s hladinou uprostřed a kolem ní *Déšť dnes*, *Z řadu dnes*, *Déšť 7 dní* (předpověď, čárkovaně) a *Spotřeba dnes*. Uzel se ukáže, jen když je nastavený (déšť a předpověď podle plochy střechy, řad podle ventilu). Cesta je zelená, když vše funguje (tečky běží, když tudy dnes teklo), oranžová při zpožděných datech čidla a červená při poruše (čidlo offline, chyba ventilu). API `/api/state` má nově `catchment_m2`, `rain_today`, `refill_today` a `refill.fault`.
- **Mapa systému v přehledu Všechny nádrže:** hub uprostřed, nádrže kolem (s hladinou), cesta ke každé nádrži zelená (online), oranžová (zpožděná data) nebo červená (offline); klepnutím se otevře detail nádrže.
- **IP adresa hubu** v Nastavení → Systém → Adresa v síti (`/api/system` vrací `ips`).
- **Volby aktualizací** (dialog i karta Systém) ve dvou zarovnaných řádcích s vysvětlivkou: *Automatické aktualizace* (instalace v noci) a *Kanál* (Stabilní / Beta).

### Změněno
- **Nový vzhled rozhraní** (světlý i tmavý motiv): chladná „vodohospodářská“ paleta s hlubokou vodní modří, tmavý pruh hlavičky a čísla v písmu Bahnschrift / DIN ze systému (nic se nestahuje).
- **Plynulá změna hladiny:** voda v nádrži stoupá a klesá plynule (i při přepnutí nádrže), a to i v režimu omezených animací systému, stejně jako vlnka.
- **Srozumitelné údaje o systému:** *Aplikace běží*, *Vytížení procesoru zařízení*, *Paměť zařízení (RAM)*, *Paměť, kterou zabírá aplikace* (dříve „Paměť procesu“), *Databáze / volné místo na kartě*.
- **Navigace:** sekce *Přehled / Nastavení / Historie událostí* jsou přímo v hlavní liště (dřív schované v menu účtu); nádrže mají vlastní řádek pod hlavičkou. Menu účtu obsahuje jen *Můj účet*, *Správa uživatelů*, motiv, jazyk a akce zařízení; tečka o dostupné aktualizaci svítí u záložky *Nastavení*.
- **Stav čidel:** hlavička ukazuje stav celého hubu („Čidla online: 2/3“), stav vybraného čidla je na kartě nádrže pod jejím názvem (u zastaralých dat zvýrazněný). Zdvojený štítek u procent byl odstraněn.
- **Přehled nádrže:** doplňkové údaje ve čtyřech skupinách 2 × 2 (Výhled, Spotřeba, Baterie čidla, Čidlo v šachtě) bez zbytečných popisků a prázdných míst; karty v přehledu všech nádrží jsou tlačítka s větší měrkou a čísly.
- **Nastavení rozdělené do sekcí:** místo karet poskládaných do sloupců jeden sloupec s pojmenovanými skupinami (*Čidla a poloha, Propojení, Data a systém*; u nádrže *Nádrž a měření, Čidlo, Automatika*) a rozcestníkem vlevo, který zvýrazní právě viditelnou kartu (na mobilu vodorovná lišta).
- **Jedno uložení nádrže:** šest tlačítek *Uložit*, která všechna ukládala celý formulář nádrže, nahradila dolní lišta *Neuložené změny nádrže – Zahodit / Uložit změny*, která se objeví po úpravě. Kalibrace má tlačítko *Přidat bod*, kalibrace baterie *Opravit měření*. Přepínač *Zobrazit pokročilá nastavení* místo „Pokročilé (zobrazeno: …)“.
- *Historie událostí* bez zdvojeného nadpisu „Záznamy“; karta *Hunter Hydrawise* má kratší název.

### Opraveno
- **Lepicí hlavička a rozcestník nastavení se nelepily:** `overflow-x: hidden` na `html` i `body` dělal z `body` posuvný kontejner a rušil `position: sticky`; nahrazeno `overflow-x: clip`.
- **Nastavení na mobilu přetékalo doprava** (mřížka `.stack` bez omezení šířky sloupce).
- **Datumová pole v mazání dat** měla ikonu kalendáře odtrženou od pole.
- Odstraněn inline `style` u skryté sekce, který porušoval Content-Security-Policy (chyba v konzoli).
- **Trhané kolečko čekání v dialogu aktualizací:** dialog se při čekání na restart každých ~1,5 s překresluje a nové kolečko začínalo animaci od nuly (skok zpět zhruba na „3 hodiny“). Animace všech koleček je teď ukotvená na začátek časové osy dokumentu, takže nové kolečko pokračuje plynule ve stejném úhlu.

## [1.13.1] - 2026-10-25

### Přidáno
- **Oponentura architektury (`docs/oponentura.md`):** posouzení architektury nad verzí 1.12.1 – silné stránky, nálezy (nejistý výsledek startu ventilu, rollback aktualizace bez vrácení DB, ztráta párování při poškozené DB, přetížená třída `Core`, překlad rozhraní) a doporučené pořadí oprav; odkaz v `README.md`.
- **Bezpečnostní kontrola 2026-10 (`docs/bezpecnostni-kontrola-2026-10.md`):** hloubková kontrola frontendu, backendu, root pomocníků, aktualizací, firmwaru a rádiového protokolu nad verzí 1.12.1; jeden neopravený nález vysoké závažnosti v root pomocnících (detaily v soukromém advisory do vydání opravy) a doporučení ke zpevnění; odkaz v `README.md`.
- **Posudek frontendu (`docs/posudek-frontendu.md`):** posouzení webového rozhraní z pohledu uživatele nad verzí 1.12.1 – silné stránky, nálezy (zastaralá data vypadají jako aktuální, tlačítko zastavení bez ohledu na stav, míchání pojmů nádrž / čidlo, vývojářské názvy, ztráta neuložených změn v nastavení, oříznutá navigace na mobilu) a doporučené pořadí úprav; odkaz v `README.md`.

### Změněno
- **Ztlumení procent a měrky při stavu `stale` / `offline`** včetně štítku se stářím dat přímo u hladiny (`#txtStaleAge`, `.is-stale`).
- **Tlačítko pro zastavení/zrušení dopouštění (`#refillStopBtn`)** se zobrazuje jen když dopouštění běží nebo čeká; dialog uvádí přesný čas pozastavení automatiky (`do zítřka HH:MM`).
- **Sjednocení pojmů a UX vylepšení:** přejmenování na `Přehled`, `Všechny nádrže`, `Historie událostí`, stav `Čidlo: online/offline` a `Hub odpojen`, slovní hodnocení kvality signálu, zjednodušení popisku výhledu srážek s nápovědou `?` (`Srážky (48 h)`), předvyplnění cíle ručního dopouštění (`auto_target_pct`) a hlídání neuložených změn v nastavení nádrže (`#calSaveTop`, varování při přepnutí).

### Opraveno
- **Dokumentace:** odznak verze v `README.md` odpovídá aktuální verzi (1.12.1); `docs/architektura.md` uvádí migrace databáze v1–v13 (motiv a jazyk u účtu v tabulce `users`) a mezi soubory webu `js/i18n.js` a `js/theme.js`.
- **`GHSA-pc46-j66c-c3w2` (eskalace oprávnění v root pomocnících):** `retencka-control.py`, `retencka-update.py` a `retencka-identity.sh` nyní nastavují práva a vlastníka výhradně přes otevřený deskriptor (`os.fchmod`, `os.fchown`, `install -d -o -g`), nikdy přes cestu v adresáři `data/` vlastněném aplikací; doplněno systemd sandboxing (`retencka-update.service`: `ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateTmp=yes`; `retencka-control.service` jen omezení, která nebrání instalaci NetBirdu přes `apt`).
- **Zpevnění z bezpečnostního auditu:** příznak `Secure` u session cookie za HTTPS tunelem (`X-Forwarded-Proto`), kontrola `Origin` výhradně proti hlavičce `Host`, ukotvení regulárních výrazů přes `\Z`, preference nejsilnějšího RSSI u čekajících klíčů (`PENDING_MAX`) a zobrazení RSSI + varování při více veřejných klíčích během párování.
- **Pojistka při nejistém startu ventilu:** když Hydrawise na `run` vůbec neodpoví (timeout, přerušené spojení), hub pošle preventivní `stop` a při jeho neúspěchu zařadí dopouštění do `stop_retry`. Odpoví-li Hydrawise chybou, ventil se neotevřel – nic se nezastavuje (neukončí se běh z rozvrhu Hydrawise) a v historii nevznikne falešné dopouštění.
- **Ochrana databáze a párování:** snapshot a automatický rollback databáze při neúspěšné aktualizaci (`retencka-update.py`), odmítnutí startu nad novějším schématem (`SchemaTooNewError`), automatická obnova z poslední noční zálohy při poškození DB a zrcadlení klíčů čidel do `<db>.keys`. Aktualizace se nespustí, pokud se nepodaří snímek databáze (návrat by jinak nechal starou verzi nad novějším schématem); snímek ověřuje, že soubor DB nebyl během zálohy vyměněn za odkaz.
- **Oprava oříznutí mobilní navigace** (`.main-nav`, `.node-bar`) a aktualizovaný překladový slovník `i18n.js` včetně automatického testu pokrytí.
- **Zachování dosavadních výchozích hodnot u stávajících nádrží (migrace v14):** nádrž, která jen dědila původní výchozí typ baterie (Li-FeS2) a průtok (41,6 L/min), je dostane uložené jako vlastní; nové nádrže dostanou nové výchozí hodnoty.
- **Názvy nádrží se nepřekládají** do angličtiny (`translate="no"`, respektuje ho `i18n.js`); zadržený velký skok čítače čidla se zapisuje do logu.
- **Dokumentace odpovídá kódu:** cesty v nastavení (*Obecné → Čidla* místo zrušené záložky *Všechna čidla* a karty *Výchozí nastavení nádrží*), výchozí alkalické baterie, sandbox systemd jednotek, cookie `Secure` za tunelem, snímek DB při aktualizaci; opravy z posudků jsou vedeny ve verzi 1.13.1 (verze 1.12.2 nikdy nevyšla).

## [1.13.0] - 2026-10-25

### Přidáno
- **Volba tvaru nádrže (`tank_shape`):** podpora pro plochu dna (`area`), ležatý válec (`cyl_h`), stojatý válec (`cyl_v`) a kvádr (`rect`). U ležatého válce se objem i procento hladiny počítají přesně podle kruhového průřezu (v horní a spodní části přibývá objem pomaleji než uprostřed). Formulář dynamicky zobrazuje pouze relevantní rozměry pro zvolený tvar.
- **Informace o systému v Nastavení:** na kartě *Systém* (`GET /api/system`) se nově zobrazuje vytížení procesoru (`cpu_pct` v %) a využití operační paměti (`ram_used_mb`, `ram_total_mb`, `ram_pct` v MB a %).

### Změněno
- **Plná šířka rozhraní na širokoúhlých monitorech:** zrušeno omezení maximální šířky `.app-container` (`max-width: none;`), rozhraní plně využívá plochu 1440p i větších monitorů.
- **Odstranění sdílené karty „Výchozí nastavení“ a záložky „Všechna čidla“ v Nastavení:** konfigurace parametrů probíhá přímo na kartě každé jednotlivé nádrže; čidla byla přesunuta do *Nastavení → Obecné*. Odstraněna předvyplněná fiktivní výchozí čísla (plocha střechy, průtok, rozměry nádrže), zatímco výchozí parametry baterie (výchozí typ *Alkalické AA*, kapacity mAh) a sondy (přesnost 0,5 %, rozsah 200 cm, výška nad dnem 1 cm) zůstávají automaticky předvyplněné.
- **Podmíněné zobrazení karet integrací:** karta *Telegram* v *Můj účet* i karta *Upozornění* u nádrže se zobrazují pouze při nakonfigurovaném Telegram botu; karta *Dopouštění* u nádrže se skrývá, pokud není zadán API klíč Hydrawise na úrovni hubu ani nádrže.
- **Horní limit dopouštění („Nejvýš dopustit na“) v základním nastavení:** pole `max_refill_pct` bylo přesunuto z pokročilých voleb přímo mezi základní viditelná pravidla dopouštění každé nádrže.
- Z popisku (tooltipu) grafu **Hladina v čase** byl odebrán údaj o napětí sondy (`sonda XXX mV`); zobrazuje se pouze procento, výška hladiny v cm a objem v litrech.

## [1.12.1] - 2026-10-25

### Opraveno
- **Animace vlnky v měrce nádrže se opět hýbe i při vypnutých systémových animacích ve Windows:** globální pravidlo `@media (prefers-reduced-motion: reduce) { * { animation: none !important; } }` přebíjelo animaci `.wave svg` bez `!important`; doplněno `!important` přímo na `.wave svg` (`3 s`) a `.wave .wave-b` (`4,5 s`).

## [1.12.0] - 2026-10-25

### Přidáno
- **Přepínač jazyka (Čeština / English)** v uživatelském menu pod přepínačem motivu (`POST /api/me/lang`, databázová migrace v13): kompletní překlad celého webového rozhraní včetně grafů, popisků, nápovědy a auditního logu s okamžitým přepnutím bez nutnosti obnovení stránky a ukládáním volby do profilu uživatele i lokálního úložiště.
- **GitHub Community Standards (`.github/`):** doplněna bezpečnostní politika (`SECURITY.md`), pokyny pro přispěvatele (`CONTRIBUTING.md`), kodex chování (`CODE_OF_CONDUCT.md`), prohlášení o přístupnosti (`ACCESSIBILITY.md`) a šablony pro Issues (`ISSUE_TEMPLATE/bug_report.yml`, `feature_request.yml`) a Pull Requesty (`pull_request_template.md`).
- **Licence (`LICENSE`):** přepnuto z MIT na **GNU AGPLv3** (ochrana proti uzavření kódu třetí stranou při zachování plných práv autora).

### Změněno
- **Ruční dopouštění** (*Dopustit*) se ptá na cílovou hladinu pouze v dialogu *Dopustit z řadu* (duplicitní vstupní pole na kartě *Stav nádrže* bylo odstraněno) a nepředvyplňuje výchozí procenta.
- Výběr **zóny Hydrawise** v nastavení dopouštění je zarovnaný ve dvousloupcové mřížce vedle výběru ventilu (s vysvětlením v nápovědě, že API Hydrawise vrací názvy zkrácené na 15 ASCII znaků).

### Opraveno
- **Animace vlnky na ukazateli hladiny nádrže je rychlejší** (`3 s` / `4,5 s`) a už se nezpomaluje při omezených systémových animacích (např. vypnuté *Efekty animace* ve Windows).
- **Bezpečnostní zpřísnění (dokončení revize):**
  - *Rádiový čítač:* velký skok čítače rámců (`ctr > last_ctr + 4096`) vyžaduje potvrzení následujícím rámcem čidla – náhodně trefený 32bitový MAC s vysokým `ctr` tak nikdy neodřízne čidlo, zatímco čidlo vracející se po letech vypnutí se plynule resynchronizuje druhým rámcem.
  - *Captive portál a root pomocník:* odmítnutí SSID začínajícího `-` (ochrana před předáním přepínače do `nmcli`) a sjednocení kontroly `is_symlink()` u `control-reboot`.
  - *HTTP server:* přidán limit současných spojení na jednu IP (`MAX_PER_IP_CONNECTIONS = 24`) a slučování vícenásobných hlaviček `Cookie` podle RFC 6265.

## [1.11.0] - 2026-10-25

### Změněno
- **Přidání čidla je celé v dialogu** *Přidat čidlo…*: kroky (baterie → ID → první data → hotovo), ID ze štítku, **povinný název** a tabulka nových čidel v dosahu. Dialog po potvrzení zůstane otevřený a průběh se v něm obnovuje; „Spárovat“ u nalezeného čidla doplní jeho ID. Na kartě *Čidla* zůstalo jen tlačítko a upozornění, když nové čidlo nabízí párování. Server nové čidlo bez názvu odmítne (odebrané čidlo spárované znovu si vezme svůj dřívější název).
- **Signál v procentech**: v grafu *Baterie a signál* (společná osa 0–100 %), v kartě *Spojení* i v tabulkách čidel – „67 % (−70 dBm)“. Převod lineárně mezi −110 dBm (0 %) a −50 dBm (100 %).
- Výběr **zóny Hydrawise** je přes celou šířku karty, dlouhé názvy zón se neořezávají.
- Pole názvu čidla už nemá vzorový text „Jímka“.

### Opraveno
- **Vlnka na hladině se nehýbala**, když má systém omezené animace (ve Windows *Efekty animace* vypnuté, v telefonu „Omezit pohyb“). Teď se vlní vždy, při omezených animacích pomaleji.

## [1.10.0] - 2026-10-24

### Odstraněno
- **HTTPS s vlastní certifikační autoritou hubu** (z verzí 1.7–1.9): web běží v domácí síti jen po `http://` jako dřív. Zmizela karta *Zabezpečené připojení (HTTPS)*, přepínač *Vždy používat HTTPS*, stažení certifikátu a odkaz na přihlašovací stránce. Root pomocník při dalším běhu (do 5 minut) smaže certifikační autoritu (`/var/lib/retencka/tls-ca`) i `data/tls`; certifikát „Retencka CA“, pokud jste ho nainstalovali do telefonu nebo PC, můžete tam odebrat. Kdo měl web otevřený přes `https://`, přihlásí se jednou znovu přes `http://`.
- Session cookie je opět jen `rt_sid` (HttpOnly, SameSite=Lax) bez příznaku Secure a prefixu `__Host-`.

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
