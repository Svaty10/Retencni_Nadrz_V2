# Instalace a provoz (Raspberry Pi Zero 2 W)

## 1. Příprava karty
1. **Raspberry Pi Imager** → *Raspberry Pi OS Lite (64-bit)*, Bookworm nebo novější.
2. V nastavení Imageru zadejte uživatele, **SSH** a dočasně svou Wi-Fi (pro instalaci).
3. Doporučená karta: *high endurance* (SanDisk High Endurance, Samsung PRO Endurance).

## 2. Instalace
Pi musí mít internet (Wi-Fi z Imageru nebo kabel). Přihlaste se přes SSH a spusťte **jediný příkaz**:
```bash
ssh pi@raspberrypi.local
curl -fsSL https://raw.githubusercontent.com/Svaty10/Retencni_Nadrz_V2/main/system/bootstrap.sh | sudo bash
```
Skript sám doinstaluje `git`, stáhne repozitář do `/opt/retencka-src`, nainstaluje balíčky, zapne SPI
(bez restartu), nastaví zemi pro Wi-Fi (CZ) a spustí služby. Na konci vypíše rámeček s webovou adresou,
názvem zařízení, jménem a heslem hotspotu a krátkou kontrolu (služba, SPI, web: OK / CHYBA).
Restart je potřeba jen výjimečně (SPI se nepodařilo zapnout za běhu, overlay) – skript se pak sám
restartuje po 10 s (Ctrl+C zruší, volba `--no-reboot` restart vypne).

Volby se předávají za `-s --`, např. vzdálený přístup přes Cloudflare:
```bash
curl -fsSL https://raw.githubusercontent.com/Svaty10/Retencni_Nadrz_V2/main/system/bootstrap.sh | sudo bash -s -- --cloudflare-token <TOKEN>
```
| Volba | Význam |
|---|---|
| `--wifi-country XX` | země pro Wi-Fi (výchozí `CZ`; bez ní je Wi-Fi na čerstvém Pi zablokovaná) |
| `--no-reboot` | nikdy automaticky nerestartovat |
| `--timezone ZÓNA` | časová zóna (výchozí `Europe/Prague`) |
| `--no-hotspot` | bez záložního hotspotu |
| `--cloudflare-token`, `--netbird-setup-key`, `--netbird-url`, `--overlay` | viz oddíly 7–9 |
| `--force` | dovolit instalaci starší verze, než je nainstalovaná |

Alternativa bez `curl | bash` (stejný výsledek):
```bash
sudo apt-get install -y git
git clone https://github.com/Svaty10/Retencni_Nadrz_V2.git && cd Retencni_Nadrz_V2
sudo ./system/install.sh
```
Skript lze kdykoli spustit znovu (aktualizace ručně: stejný příkaz, nebo `cd /opt/retencka-src && sudo ./system/install.sh`;
data a nastavení se nemění, chybějící klíče v `/etc/default/retencka` se doplní jako zakomentované).

Po instalaci se zařízení přejmenuje na `retencka-xxxx` (xxxx = konec MAC adresy).
Do souboru `/boot/firmware/retencka.txt` se zapíše **heslo zařízení** (platí pro hotspot i pro první
přihlášení na web) – obsah vytiskněte na štítek. Funguje i na Pi 3 B/3 B+/4 (stejné zapojení i instalace).

## 3. Uvedení do provozu u zákazníka
1. Zapojit do zásuvky. Pokud zařízení nezná žádnou Wi-Fi (nebo se k ní 3 min nepřipojí), spustí
   hotspot **Retencka-XXXX** (heslo ze štítku). Je-li zapojen **kabel (Ethernet)**, hotspot se nespouští
   – zařízení je dostupné po kabelu (`http://retencka-xxxx.local`) a Wi-Fi lze nastavit později.
2. Telefon se připojí → sám nabídne „Přihlásit se k síti“ a otevře portál (jinak `http://retencka-xxxx.local`
   nebo `http://192.168.4.1`).
3. Vybrat domácí síť, zadat heslo → zařízení se připojí, hotspot zmizí.
4. Telefon zpět na domácí Wi-Fi → `http://retencka.local` (nebo `http://retencka-xxxx.local`).
5. První přihlášení: jméno **admin** + **heslo ze štítku** → vytvořit vlastní účet (jiné jméno i heslo);
   výchozí přístup se tím zruší. *Nastavení*, *Správa uživatelů*, *Logování* i *Můj účet* jsou v menu
   pod ikonou účtu vpravo nahoře.

   *Nastavení* má záložky **Obecné** (celý systém: poloha, Hunter Hydrawise, Telegram a vzdálený servis,
   Home Assistant, záloha a data, systém), **Všechny nádrže** (čidla, párování, výchozí nastavení nádrží)
   a **jednu záložku na každou nádrž** (všechny její parametry). Řádek s kartami pod záložkami vede rovnou
   na danou kartu.
6. *Nastavení → Obecné → Poloha*: do pole *Obec / adresa* napsat obec a vybrat ji ze seznamu (souřadnice
   pro předpověď počasí se doplní samy; přes HTTPS jde i *Použít polohu tohoto zařízení*, ruční
   souřadnice jsou v pokročilém režimu).
7. Čidlo se páruje vždy jen po potvrzení: *Nastavení → Všechny nádrže → Čidla → Spárovat* u jeho ID
   (po vložení baterie čidlo 10 min nabízí párování, ID porovnat se štítkem). Samo se nespáruje nikdy.
8. *Nastavení → ‹nádrž›*: *Nádrž* (název, rozměry), *Kalibrace* (2 body „skutečná hladina je teď X cm“ –
   ideálně jednou při nízké a jednou při vysoké hladině), *Sonda* (výška nade dnem, přesnost, rozsah –
   pokročilé), *Baterie*, *Upozornění*. Hodnoty označené **výchozí** se berou z *Všechny nádrže →
   Výchozí nastavení nádrží*; po změně u nádrže jsou **vlastní** (zpět tlačítkem *↺ výchozí*).
9. Dopouštění (volitelné): API klíč účtu do *Nastavení → Obecné → Hunter Hydrawise* (*Otestovat připojení*
   ukáže název ovladače), pak *Nastavení → ‹nádrž› → Dopouštění → Ventil: Hunter Hydrawise* →
   *Načíst zóny z Hydrawise* a vybrat zónu podle názvu (ruční Relay ID jen v pokročilém režimu; do pole
   zóny nepatří API klíč – hub ho odmítne). Dokud ventil není nastavený, dashboard o dopouštění nic
   neukazuje (stav, tlačítka, graf).

### Výměna baterie čidla
Do čidla patří 3× AA. Doporučené jsou **lithiové Li-FeS2** (Energizer Ultimate Lithium): 10+ let,
nevytékají, snesou mráz. Jdou i **alkalické** (vydrží několik let) – pak u čidla nastavte
*Nastavení → ‹nádrž› → Baterie → Baterie v čidle → Alkalické* (pro všechny nádrže najednou ve
*Všechny nádrže → Výchozí nastavení nádrží*; tam i kapacita baterií), jinak hub ukazuje špatná procenta a hlásí slabou
baterii už v polovině životnosti. Alkalické vyměňte včas, vybité mohou vytéct.

Vyndat a vložit novou baterii – čidlo hned pokračuje se stávajícím klíčem; **nic potvrzovat nemusíte**.
Totéž platí po nahrání nového firmwaru: klíč je v EEPROM, kterou nahrávání nepřepisuje (fuse `EESAVE`),
takže čidlo zůstane spárované se svým hubem.

### Odpárování / přesun čidla k jinému hubu
1. Ve starém hubu *Nastavení → Všechny nádrže → Čidla → Odpárovat* (hub hned přestane brát data).
2. Vyndat a vložit do čidla baterii (může být i v opačném pořadí, do 10 minut). Čidlo smaže klíč a hub ho odebere.
3. Čidlo teď nabízí párování – spárovat v novém hubu.

Čidlo spárované s jiným hubem se v seznamu zobrazí jako „patří jinému hubu“ a převzít ho nelze.

Čidlo neodpovídá (vybitá baterie, porucha): u čidla *Zrušit* (data se zase přijímají) nebo
*Odebrat bez čidla*. Odebrané čidlo si hub pamatuje (archiv klíčů): když se najde, ukáže se v
*Čidla v dosahu* jako „cizí čidlo“ s tlačítkem **Spárovat znovu** – kliknout a do čidla vyjmout a
vložit baterii; hub ho uvolní a spáruje znovu (bez UPDI). Stejně se obnoví čidlo, kterému se po
spárování ztratila první data a hub je do 24 h neviděl.

Čidlo, které hub opravdu nezná (patří jinému hubu, nebo bylo spárované před přeinstalací hubu bez
zálohy), ukazuje *Čidla v dosahu* jako „cizí čidlo“ s nápovědou – odemknout ho jde jen smazáním
párování přes UPDI (`uzel/4-smazat-parovani.cmd`, viz
[programovani_uzlu.md](programovani_uzlu.md#smazání-párování-4-smazat-parovanicmd)).
**Zálohujte `/var/lib/retencka`** – záloha obsahuje klíče čidel i archiv klíčů.

Čidlo ztratilo klíč (smazaná EEPROM, vyměněný čip se stejným ID): u jeho ID je „čidlo ztratilo klíč“ a
**Znovu spárovat** – název, nastavení i historie zůstanou.

### Záloha a obnova
- Hub dělá každou noc zálohu na kartu (7 dní zpět). Proti odumření karty: *Nastavení → Obecné → Záloha a data →
  Stáhnout zálohu* a soubor uložit do PC / cloudu (občas, např. po přidání čidla).
- **Obnova (např. nová karta):** nainstalovat podle bodu 2, přihlásit se, *Nastavení → Obecné → Záloha a data → Nahrát zálohu*
  (zaškrtnout „včetně nastavení a uživatelů“). Data se přidají k současným, duplicity se přeskočí – import
  lze bezpečně opakovat. Vrátí se měření, čidla i s klíči (hned pokračují), nastavení, token HA a uživatelé.
- Data se nemažou (8GB karta vystačí na desítky let); retenci lze nastavit v *Záloha a data →
  Uchovávání dat*.
- **Smazat naměřená data** (např. po zkušební montáži): *Nastavení → Obecné → Záloha a data → Smazat
  naměřená data* – vybrat nádrž nebo všechny nádrže a rozsah *od – do* (nebo *vše*). Hub nejdřív ukáže,
  kolik měření, hodinových souhrnů a záznamů o dopouštění smaže (historie dopouštění v rozsahu jde také
  pryč) a nabídne stažení zálohy. Nádrže, nastavení a záznamy v *Logování* zůstanou; smazání se zapíše
  do Logování. Nejde vrátit – jen nahráním zálohy.

### Restart a vypnutí
V menu účtu (vpravo nahoře, jen administrátor): **Restartovat aplikaci** (asi 10–30 s, měření se neztratí),
**Restartovat zařízení** (1–2 min) a **Vypnout zařízení** (znovu ho zapnete odpojením a připojením
napájení). Každá akce se nejdřív potvrdí a zapíše do Logování; po restartu stránka ukáže „Čekám na
zařízení…“ a sama se znovu připojí. Před odpojením napájení je šetrnější zařízení vypnout (karta SD).
Totéž v Telegramu: `restart`, `reboot`, `vypnout`.

## 4. Co se děje při problémech (bezúdržbovost)
| Situace | Reakce |
|---|---|
| Výpadek proudu | SQLite WAL + atomické zápisy, logy jen v RAM; po startu vše naběhne samo |
| Router naběhne později než Pi | 3 min tolerance, pak hotspot (jen bez kabelu); každé 3 min nový pokus o známou síť |
| Změna Wi-Fi hesla / nový router | po 3 min hotspot → nové nastavení přes portál |
| Pád aplikace | systemd restart do 3 s (bez limitu pokusů) |
| Zamrznutí aplikace / rádiového vlákna | systemd watchdog (60 s) → restart |
| Zamrznutí celého systému | HW watchdog BCM2835 (15 s) → reboot |
| Zaseknuté rádio CC1101 | kontrola registrů každých 30 s, reset; preventivní reset po 16 min ticha |
| Poškozená databáze | odložena jako `*.corrupt-*`, založena nová, provoz pokračuje |
| Nepovedená aktualizace | automatický návrat na předchozí verzi do 2 min, verze se už nezkouší |
| Bez internetu | web, grafy i HA běží dál (vše lokálně); čas drží `fake-hwclock`, dashboard upozorní |

## 5. Aktualizace z GitHubu
Hub instaluje jen **podepsaný** balík z GitHub Releases (Ed25519). Data a nastavení
(`/var/lib/retencka/data`) se nemění; když nová verze do 2 min nenaběhne, vrátí se předchozí
i se systemd jednotkami.

**Ručně z webu – zkontrolovat → vybrat → nainstalovat:**
1. *Nastavení → Obecné → Systém → Zkontrolovat aktualizace* otevře dialog „Hledám aktualizace…“.
   Kontrola **nikdy nic neinstaluje**.
2. Výsledek: „Máte nejnovější verzi X“, nebo nová verze s datem vydání a poznámkami
   z `CHANGELOG.md` (všechny verze mezi nainstalovanou a novou), nebo chyba se „Zkusit znovu“.
3. **Aktualizovat na X** spustí instalaci s průběhem *Stahuji → Ověřuji podpis → Instaluji →
   Restartuji*. Během restartu dialog chvíli čeká („Čekám na restart…“, až 3 min), pak ukáže
   „Aktualizováno na X“ s tlačítkem *Načíst znovu*. Při chybě nebo návratu dál běží předchozí verze.
   Nainstaluje se jen přesně ta verze, která byla nabídnuta, a jen pokud je stále nejnovější.

Když je k dispozici nová verze, svítí tečka u ikony účtu a u položky *Nastavení*; v kartě *Systém*
je řádek „K dispozici verze X“ s tlačítkem do dialogu.

**Automaticky v noci:** 1× denně (03:30 + náhodné zpoždění, a 15 min po startu) se hub podívá na
GitHub. Přepínač *Instalovat aktualizace automaticky (v noci)* (v dialogu i v kartě *Systém*)
rozhoduje, zda novou verzi rovnou nainstaluje, nebo ji jen nabídne.

Jak to funguje uvnitř: web (bez práv roota) zapíše žádost `data/update-request`
(`{"action": "check"}` nebo `{"action": "install", "version": "X"}`), kterou zpracuje
`retencka-update.service` (root). Průběh zapisuje do `data/update-status.json` (`state`: checking →
available / up_to_date / error, downloading → verifying → installing → restarting → done /
rolled_back / error). Volby automatiky a kanálu jsou v `data/update-prefs.json`; root je čte přísně
validované (jen ano/ne a `stable`/`beta`) a nikdy z nich nebere adresu repozitáře ani cesty.

### 5.1 Jednorázová příprava podpisového klíče
Na PC (Windows: **Git Bash**, OpenSSL je jeho součástí), **mimo** složku repozitáře a mimo OneDrive:
```bash
cd ~ && mkdir -p retencka-klic && cd retencka-klic
openssl genpkey -algorithm ed25519 -out release.key                 # SOUKROMÝ klíč – nikdy do gitu
openssl pkey -in release.key -pubout -out release.pub               # veřejný klíč
cat release.key                                                     # zkopírovat celý text vč. BEGIN/END
```
1. **GitHub → repozitář → Settings → Secrets and variables → Actions → New repository secret**:
   název `RELEASE_SIGNING_KEY`, hodnota = celý obsah `release.key` (od `-----BEGIN PRIVATE KEY-----`
   po `-----END PRIVATE KEY-----`).
2. `release.pub` zkopírovat do repozitáře jako `system/keys/release.pub`, commitnout a pushnout.
3. `release.key` uložit bezpečně (správce hesel / šifrovaný disk). Bez něj nejde vydat další aktualizaci,
   kterou by stávající huby přijaly.
4. Huby nainstalované **před** přidáním klíče ho ještě nemají – jednou na nich spusťte znovu instalaci
   (stejný příkaz `curl … | sudo bash` z oddílu 2). Nové instalace ho mají rovnou.

Repozitář musí být **veřejný** (hub se ptá GitHubu bez přihlášení).

### 5.2 Vydání nové verze
Podrobný postup (číslování, kontrola, ověření): [vydani.md](vydani.md). Ve zkratce:
```bash
# 1) zvýšit verzi v server/retencka/__init__.py, např. __version__ = "1.0.1"
git commit -am "Verze 1.0.1"
git tag v1.0.1                      # tag = "v" + přesně __version__
git push && git push --tags
```
GitHub Action *release* spustí testy, sestaví `retencka-1.0.1.tar.gz`, podepíše ho a vytvoří Release.
Průběh: záložka **Actions**. Huby s automatikou si ho nainstalují do 24 h, ostatní ho nabídnou
(nebo hned přes *Zkontrolovat aktualizace* → *Aktualizovat na X*).
Ručně (bez Actions): `tools/make-release.sh ~/retencka-klic/release.key` a oba soubory z `dist/`
nahrát k Release s tagem `v1.0.1`.

### 5.3 Když aktualizace neprojde
| Hláška / chyba | Příčina a řešení |
|---|---|
| Action: *Tag v… != verze …* | tag neodpovídá `__version__` – smazat tag (`git tag -d v1.0.1 && git push --delete origin v1.0.1`), opravit, znovu |
| Action: *Soukromý klíč … chybí* | chybí secret `RELEASE_SIGNING_KEY` |
| Action: *Chybí system/keys/release.pub* / *Podpis nesedí* | veřejný klíč není commitnutý, nebo nepatří k secretu |
| Hub: *chybí veřejný klíč release.pub* | hub instalovaný před přidáním klíče – spustit instalaci znovu (5.1 bod 4) |
| Hub: *… má neplatný podpis – odmítnuto* | balík podepsaný jiným klíčem než má hub |
| Hub: *Verze … nenaběhla – vráceno* | nová verze na hubu nenastartovala; automatika ji už nezkouší (ručně z dialogu ji lze zkusit znovu), opravte a vydejte vyšší verzi |
| Hub: *Máte nejnovější verzi …* | na GitHubu není novější podepsaný Release (pozor: Release musí obsahovat `.tar.gz` i `.sig`) |
| Web: *Služba aktualizací na žádost nereaguje* | neběží `retencka-update.path` – `sudo systemctl enable --now retencka-update.path`, viz `journalctl -u retencka-update` |

Stav poslední kontroly je v *Nastavení → Obecné → Systém*, podrobně `journalctl -u retencka-update`.

### 5.4 Kanál a automatika
Přepínač automatiky a volba kanálu (*Stabilní* / *Beta*) jsou v dialogu aktualizací a v kartě
*Nastavení → Obecné → Systém* (uloží se do `data/update-prefs.json`). Dokud je web nezměnil, platí
`/etc/default/retencka`: `RETENCKA_CHANNEL=stable|beta`, `RETENCKA_AUTO_UPDATE=1|0`. Kanál `beta`
bere i předběžné verze (tag s pomlčkou, např. `v1.3.0-beta1` → GitHub *pre-release*). Předběžná verze
je vždy starší než finální (`1.3.0-beta1` < `1.3.0`), takže z bety se po vydání finální verze přejde samo.

## 6. Telegram – upozornění a ovládání z telefonu
Zdarma, bez veřejné IP a bez otevřených portů (hub se k Telegramu jen sám připojuje).

**Při instalaci (technik nebo zákazník, přímo z webu):** *Nastavení → Obecné → Telegram a vzdálený
servis*. Dokud bot není nastavený, karta ukazuje návod:
1. V Telegramu otevřete [@BotFather](https://t.me/BotFather),
2. pošlete `/newbot`, zvolte název (např. „Nádrž Novákovi“) a uživatelské jméno končící na `bot`,
3. token, který BotFather pošle, vložte do pole *Token bota*,
4. *Ověřit a uložit* – hub se k botovi připojí a ukáže „Bot: připojeno · @jméno_bota“ (jinak chybu, např.
   neplatný token). Token lze později vyměnit tlačítkem *Změnit token*.

**Každý uživatel (i zákazník):** *Můj účet → Telegram → Propojit s Telegramem* → otevřít odkaz v telefonu →
v Telegramu **Start**. Chat má roli svého účtu: uživatel jen čte, administrátor smí dopouštět a servisní příkazy.

| Příkaz | Kdo | Co dělá |
|---|---|---|
| `stav` | všichni | hladina, litry, baterie, „vystačí ~N dní“, dopouštění (s tlačítky) |
| `graf` | všichni | obrázek hladiny za 7 dní (dopouštění zeleně, déšť modře) |
| `upozorneni zap` / `vyp` | všichni | upozornění pro tento chat |
| `dopustit [nádrž] [%]`, `stop [nádrž]` | admin | dopouštění na dálku; `stop` zruší i čekající nebo automatické dopouštění |
| `obnovit [nádrž]` | admin | znovu zapne automatiku pozastavenou zrušením |
| `diag`, `log` | admin | diagnostika (signál, ztráty, rušení, baterie, místo, verze), poslední záznamy |
| `restart`, `reboot`, `vypnout` | admin | restart aplikace / restart zařízení / vypnutí zařízení (vždy s potvrzením tlačítkem) |
| `aktualizovat` | admin | zkontroluje aktualizace a pošle výsledek; je-li nová verze, s poznámkami a tlačítkem *Aktualizovat na X* (nebo `aktualizovat instalovat X`) |
| `servis zap` / `trvale` / `vyp` | admin | vzdálený servisní přístup (NetBird, viz níže) |
| `odhlasit` | všichni | odpojí chat |

Upozornění: hladina pod nastavenou hranicí (*Upozornit v Telegramu pod* – výchozí pro všechny nádrže
v *Nastavení → Všechny nádrže → Výchozí nastavení nádrží*, vlastní hodnota v záložce nádrže), dopouštění spuštěno /
dokončeno / porucha, čekání na déšť, slabá nebo vybitá baterie, čidlo neposílá data a znovu posílá.
Administrátoři navíc dostávají restarty čidel, párování, málo místa na kartě a změny servisního přístupu.

## 7. Vzdálený servis – NetBird
Technik se může k zařízení připojit na dálku (web i SSH), aniž by zákazník cokoli instaloval.
NetBird je zdarma do 5 uživatelů a 100 zařízení a je open source (lze provozovat i na vlastním serveru).

1. Účet na [app.netbird.io](https://app.netbird.io) → *Setup Keys* → vytvořit klíč (opakovaně použitelný,
   skupina např. `retencka`).
2. *Access Control*: pravidlo „skupina `servis` (vaše PC/telefon) → skupina `retencka`, TCP 22 a 80“ a **žádné**
   pravidlo `retencka → retencka` (huby se navzájem nevidí). Výchozí pravidlo „All“ smažte.
3. Instalace na hubu – jedna z možností:
   * **z webu:** *Nastavení → Obecné → Telegram a vzdálený servis → Nainstalovat vzdálený servis*:
     vložit setup key (vlastní server: pole *Vlastní server (URL)* v pokročilém režimu, jen `https://`).
     Karta ukazuje průběh instalace a výsledek. Klíč se nikam neukládá ani nezapisuje do záznamů.
   * **při instalaci hubu:** `cd /opt/retencka-src && sudo ./system/install.sh --netbird-setup-key <KLÍČ>`
     (vlastní server: navíc `--netbird-url https://…`).

   Obě cesty dělají totéž (`system/bin/retencka-netbird-install.sh`): repozitář NetBird s podpisovým
   klíčem, instalace balíku, `netbird service install/start` a `netbird up --setup-key …`.
4. Po instalaci je přístup povolen **24 hodin**, pak se sám vypne. Zákazník (nebo technik s jeho souhlasem)
   ho zapíná v *Nastavení → Obecné → Telegram a vzdálený servis* (vypnuto / na 24 h / trvale) nebo příkazem
   `servis zap` v Telegramu. Každá změna je v záznamech a administrátorům přijde zpráva.
5. Připojení: web `http://<NetBird-IP>`, SSH `ssh <uživatel>@<NetBird-IP>` – doporučeno jen klíčem
   (`PasswordAuthentication no` v `/etc/ssh/sshd_config`).

NetBird ovládá pomocná služba `retencka-control` (s právy roota, aplikace sama žádná nemá): aplikace zapíše
požadavek, služba zavolá `netbird up` / `netbird down` a každých 5 minut kontroluje vypršení 24 h. Instalaci
z webu provádí stejná služba (`data/control-netbird-install`, soubor se klíčem smaže hned po přečtení;
průběh v `data/netbird-install.json`, podrobnosti `journalctl -u retencka-control`).

## 8. Vlastní doména – Cloudflare Tunnel
Pro vlastní nasazení s doménou (adresa typu `https://nadrz.mojedomena.cz`).

1. Cloudflare Zero Trust → *Networks → Tunnels* → vytvořit tunel, zkopírovat token.
2. `cd /opt/retencka-src && sudo ./system/install.sh --cloudflare-token <TOKEN>`
3. *Public hostname* → `http://localhost:80`. **Port 8081 (Home Assistant) do tunelu nedávejte.**
4. **Doporučeno:** *Access → Applications* → aplikace pro stejný hostname s politikou
   „e-mail je jeden z …“ (jednorázový kód e-mailem). Útočník se pak k přihlašovací stránce vůbec nedostane.

Web sám o sobě: hesla scrypt, session na serveru (HttpOnly, SameSite, Secure přes HTTPS),
CSRF token + kontrola Origin, zpomalování a zamykání po neúspěšných pokusech, striktní CSP,
všechna API jen po přihlášení, audit log.

## 9. Read-only systém (volitelné, maximální odolnost SD karty)
Overlay root vyžaduje, aby `/var/lib/retencka` byl **samostatný oddíl** (jinak by se ztrácela
data i aktualizace). Postup na čisté kartě (na PC s Linuxem, před prvním spuštěním):
```bash
sudo parted /dev/sdX resizepart 2 8GiB
sudo parted /dev/sdX mkpart primary ext4 8GiB 100%
sudo mkfs.ext4 -L retdata /dev/sdX3
```
Na Pi: přidat do `/etc/fstab` řádek
`LABEL=retdata /var/lib/retencka ext4 defaults,noatime,commit=60 0 2`, připojit, pak
`sudo ./system/install.sh --overlay`. Systémové změny pak vyžadují dočasné vypnutí overlaye
(`sudo raspi-config` → Performance → Overlay FS).

## 10. Užitečné příkazy
```bash
systemctl status retencka            # stav služby
journalctl -u retencka -f            # log aplikace (jen v RAM)
journalctl -u retencka-netwatch -f   # Wi-Fi / hotspot
journalctl -u retencka-control       # restart / servisní přístup (NetBird)
netbird status                       # stav NetBird
sudo systemctl start retencka-update # běh jako v noci (instaluje jen se zapnutou automatikou)
cat /var/lib/retencka/data/update-status.json   # stav poslední kontroly / instalace
sudo cat /boot/firmware/retencka.txt                              # heslo ze štítku
sudo sqlite3 /var/lib/retencka/data/retencka.db "DELETE FROM users"  # tovární přístup: admin + heslo ze štítku
```

## 11. Vývoj bez hardwaru
```bash
cd server
RETENCKA_DATA=/tmp/rt RETENCKA_WEB_PORT=8080 RETENCKA_HA_PORT=8081 RETENCKA_MOCK=1 python3 -m retencka
RETENCKA_DATA=/tmp/rt python3 tools/seed_demo.py 30      # 30 dní ukázkových dat
RETENCKA_DATA=/tmp/rt python3 tools/seed_demo.py 30 A1C3 --name "Garáž"   # další nádrž
python3 -m unittest discover -s tests
```
Server potřebuje jen Python ≥ 3.11 ze standardní knihovny (+ `python3-spidev`, volitelně `python3-libgpiod`).
