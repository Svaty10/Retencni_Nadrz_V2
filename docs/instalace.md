# Instalace a provoz (Raspberry Pi Zero 2 W)

## 1. Příprava karty
1. **Raspberry Pi Imager** → *Raspberry Pi OS Lite (64-bit)*, Bookworm nebo novější.
2. V nastavení Imageru zadejte uživatele, **SSH** a dočasně svou Wi-Fi (pro instalaci).
3. Doporučená karta: *high endurance* (SanDisk High Endurance, Samsung PRO Endurance).

## 2. Instalace
```bash
ssh pi@raspberrypi.local
sudo apt-get install -y git
git clone https://github.com/Svaty10/Retencni_Nadrz_V2.git
cd Retencni_Nadrz_V2
sudo ./system/install.sh          # volitelně: --cloudflare-token <TOKEN>
sudo reboot                        # po prvním zapnutí SPI
```
Po instalaci se zařízení přejmenuje na `retencka-xxxx` (xxxx = konec MAC adresy).
Do souboru `/boot/firmware/retencka.txt` se zapíše **heslo zařízení** (platí pro hotspot i pro první
přihlášení na web) – obsah vytiskněte na štítek. Funguje i na Pi 3 B/3 B+ (stejné zapojení i instalace).

## 3. Uvedení do provozu u zákazníka
1. Zapojit do zásuvky. Pokud zařízení nezná žádnou Wi-Fi (nebo se k ní 3 min nepřipojí), spustí
   hotspot **Retencka-XXXX** (heslo ze štítku).
2. Telefon se připojí → sám nabídne „Přihlásit se k síti“ a otevře portál (jinak `http://retencka-xxxx.local`
   nebo `http://192.168.4.1`).
3. Vybrat domácí síť, zadat heslo → zařízení se připojí, hotspot zmizí.
4. Telefon zpět na domácí Wi-Fi → `http://retencka.local` (nebo `http://retencka-xxxx.local`).
5. První přihlášení: jméno **admin** + **heslo ze štítku** → vytvořit vlastní účet (jiné jméno i heslo);
   výchozí přístup se tím zruší.
6. *Nastavení → Kalibrace*: rozměry nádrže, výška sondy nade dnem, přesnost a rozsah sondy a 2 kalibrační
   body („skutečná hladina je teď X cm“ – ideálně jednou při nízké a jednou při vysoké hladině).
   První čidlo se při první instalaci spáruje samo; další přes *Nastavení → Čidla a rádio → Spárovat*
   (po vložení baterie čidlo 10 min nabízí párování, ID porovnat se štítkem).

### Výměna baterie čidla
Vyndat a vložit novou baterii – čidlo hned pokračuje se stávajícím klíčem; **nic potvrzovat nemusíte**.

### Odpárování / přesun čidla k jinému hubu
1. Ve starém hubu *Nastavení → Čidla a rádio → Odpárovat* (hub hned přestane brát data).
2. Vyndat a vložit do čidla baterii (může být i v opačném pořadí, do 10 minut). Čidlo smaže klíč a hub ho odebere.
3. Čidlo teď nabízí párování – spárovat v novém hubu.

Čidlo spárované s jiným hubem se v seznamu zobrazí jako „spárované s jiným hubem“ a převzít ho nelze.
Ztracené čidlo: *Odebrat bez čidla*. **Zálohujte `/var/lib/retencka`** – bez klíčů v DB jdou čidla
odemknout jen přeprogramováním.

### Záloha a obnova
- Hub dělá každou noc zálohu na kartu (7 dní zpět). Proti odumření karty: *Nastavení → Záloha →
  Stáhnout zálohu* a soubor uložit do PC / cloudu (občas, např. po přidání čidla).
- **Obnova (např. nová karta):** nainstalovat podle bodu 2, přihlásit se, *Nastavení → Záloha → Nahrát zálohu*
  (zaškrtnout „včetně nastavení a uživatelů“). Data se přidají k současným, duplicity se přeskočí – import
  lze bezpečně opakovat. Vrátí se měření, čidla i s klíči (hned pokračují), nastavení, token HA a uživatelé.
- Data se nemažou (8GB karta vystačí na desítky let); retenci lze nastavit v pokročilém nastavení.

## 4. Co se děje při problémech (bezúdržbovost)
| Situace | Reakce |
|---|---|
| Výpadek proudu | SQLite WAL + atomické zápisy, logy jen v RAM; po startu vše naběhne samo |
| Router naběhne později než Pi | 3 min tolerance, pak hotspot; každé 3 min nový pokus o známou síť |
| Změna Wi-Fi hesla / nový router | po 3 min hotspot → nové nastavení přes portál |
| Pád aplikace | systemd restart do 3 s (bez limitu pokusů) |
| Zamrznutí aplikace / rádiového vlákna | systemd watchdog (60 s) → restart |
| Zamrznutí celého systému | HW watchdog BCM2835 (15 s) → reboot |
| Zaseknuté rádio CC1101 | kontrola registrů každých 30 s, reset; preventivní reset po 16 min ticha |
| Poškozená databáze | odložena jako `*.corrupt-*`, založena nová, provoz pokračuje |
| Nepovedená aktualizace | automatický návrat na předchozí verzi do 2 min, verze se už nezkouší |
| Bez internetu | web, grafy i HA běží dál (vše lokálně); čas drží `fake-hwclock`, dashboard upozorní |

## 5. Aktualizace z GitHubu
- Kontrola 1× denně (03:30 + náhodné zpoždění) a tlačítkem *Nastavení → Systém*.
- Instalují se jen **podepsané** releasy (Ed25519). Data a nastavení zůstávají v `/var/lib/retencka/data`.
- Jednorázová příprava klíče (na PC, soukromý klíč nikdy do gitu):
  ```bash
  openssl genpkey -algorithm ed25519 -out release.key
  openssl pkey -in release.key -pubout -out system/keys/release.pub   # tento commitnout
  ```
  Obsah `release.key` vložte do GitHub secretu `RELEASE_SIGNING_KEY`.
- Vydání verze: zvýšit `__version__` v `server/retencka/__init__.py`, commit, `git tag v1.0.1 && git push --tags`.
  GitHub Action sestaví a podepíše balík. Ručně: `tools/make-release.sh release.key`.
- Kanál (`stable`/`beta`) a vypnutí automatiky: `/etc/default/retencka`.

## 6. Telegram – upozornění a ovládání z telefonu
Zdarma, bez veřejné IP a bez otevřených portů (hub se k Telegramu jen sám připojuje).

**Při instalaci (dělá technik):**
1. V Telegramu napište **@BotFather** → `/newbot` → název (např. „Nádrž Novákovi“) a uživatelské jméno
   končící na `bot`. BotFather pošle **token**.
2. Web hubu → *Nastavení → Obecné → Telegram* → vložit token → *Uložit*. Stav ukáže „připojeno · @jméno_bota“.

**Každý uživatel (i zákazník):** *Můj účet → Telegram → Propojit s Telegramem* → otevřít odkaz v telefonu →
v Telegramu **Start**. Chat má roli svého účtu: uživatel jen čte, administrátor smí dopouštět a servisní příkazy.

| Příkaz | Kdo | Co dělá |
|---|---|---|
| `stav` | všichni | hladina, litry, baterie, „vystačí ~N dní“, dopouštění (s tlačítky) |
| `graf` | všichni | obrázek hladiny za 7 dní (dopouštění zeleně, déšť modře) |
| `upozorneni zap` / `vyp` | všichni | upozornění pro tento chat |
| `dopustit [nádrž] [%]`, `stop [nádrž]` | admin | dopouštění na dálku |
| `diag`, `log` | admin | diagnostika (signál, ztráty, rušení, baterie, místo, verze), poslední záznamy |
| `restart`, `reboot`, `aktualizovat` | admin | restart aplikace / zařízení, kontrola aktualizací (s potvrzením) |
| `servis zap` / `trvale` / `vyp` | admin | vzdálený servisní přístup (NetBird, viz níže) |
| `odhlasit` | všichni | odpojí chat |

Upozornění: hladina pod nastavenou hranicí (*Nastavení → nádrž → Upozornit pod*), dopouštění spuštěno /
dokončeno / porucha, čekání na déšť, slabá nebo vybitá baterie, čidlo neposílá data a znovu posílá.
Administrátoři navíc dostávají restarty čidel, párování, málo místa na kartě a změny servisního přístupu.

## 7. Vzdálený servis – NetBird
Technik se může k zařízení připojit na dálku (web i SSH), aniž by zákazník cokoli instaloval.
NetBird je zdarma do 5 uživatelů a 100 zařízení a je open source (lze provozovat i na vlastním serveru).

1. Účet na [app.netbird.io](https://app.netbird.io) → *Setup Keys* → vytvořit klíč (opakovaně použitelný,
   skupina např. `retencka`).
2. *Access Control*: pravidlo „skupina `servis` (vaše PC/telefon) → skupina `retencka`, TCP 22 a 80“ a **žádné**
   pravidlo `retencka → retencka` (huby se navzájem nevidí). Výchozí pravidlo „All“ smažte.
3. Instalace hubu: `sudo ./system/install.sh --netbird-setup-key <KLÍČ>`
   (vlastní server: navíc `--netbird-url https://…`).
4. Po instalaci je přístup povolen **24 hodin**, pak se sám vypne. Zákazník (nebo technik s jeho souhlasem)
   ho zapíná v *Nastavení → Telegram a vzdálený servis* (vypnuto / na 24 h / trvale) nebo příkazem
   `servis zap` v Telegramu. Každá změna je v záznamech a administrátorům přijde zpráva.
5. Připojení: web `http://<NetBird-IP>`, SSH `ssh <uživatel>@<NetBird-IP>` – doporučeno jen klíčem
   (`PasswordAuthentication no` v `/etc/ssh/sshd_config`).

NetBird ovládá pomocná služba `retencka-control` (s právy roota, aplikace sama žádná nemá): aplikace zapíše
požadavek, služba zavolá `netbird up` / `netbird down` a každých 5 minut kontroluje vypršení 24 h.

## 8. Vlastní doména – Cloudflare Tunnel
Pro vlastní nasazení s doménou (adresa typu `https://nadrz.mojedomena.cz`).

1. Cloudflare Zero Trust → *Networks → Tunnels* → vytvořit tunel, zkopírovat token.
2. `sudo ./system/install.sh --cloudflare-token <TOKEN>`
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
sudo systemctl start retencka-update # ruční kontrola aktualizace
sudo cat /boot/firmware/retencka.txt                              # heslo ze štítku
sudo sqlite3 /var/lib/retencka/data/retencka.db "DELETE FROM users"  # tovární přístup: admin + heslo ze štítku
```

## 11. Vývoj bez hardwaru
```bash
cd server
RETENCKA_DATA=/tmp/rt RETENCKA_WEB_PORT=8080 RETENCKA_HA_PORT=8081 RETENCKA_MOCK=1 python3 -m retencka
RETENCKA_DATA=/tmp/rt python3 tools/seed_demo.py 30      # 30 dní ukázkových dat
python3 -m unittest discover -s tests
```
Server potřebuje jen Python ≥ 3.11 ze standardní knihovny (+ `python3-spidev`, volitelně `python3-libgpiod`).
