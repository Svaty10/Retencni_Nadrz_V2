# Zadání – Retenční nádrž V3 (upřesněný „master prompt“)

> Tento dokument je závazná specifikace celého systému. Vznikl revizí původního zadání a kódu v2
> (2026-10-11). Při dalším vývoji (člověk i AI) se řiďte tímto souborem.

## 0. Cíl

Bezúdržbový systém pro měření hladiny v retenční nádrži, určený i pro koncové zákazníky:
zapojí se do zásuvky, jednou se nastaví Wi-Fi a dál běží roky bez zásahu – přežije výpadky proudu,
pády, výpadky internetu i aktualizace.

Tři části:

| Část | HW | Úkol |
|---|---|---|
| `uzel/` | ATtiny1624 + CC1101 + SHT40 + hydrostatická sonda, 3× Li-FeS2 | měří a odesílá každých 5 min |
| `server/` | Raspberry Pi Zero 2 W (512 MB RAM) + CC1101 na SPI0 | příjem, ukládání, web, Home Assistant, Hydrawise |
| `system/` | Raspberry Pi OS Lite (Bookworm/Trixie) | Wi-Fi provisioning, mDNS, aktualizace, odolnost |

---

## 1. Senzorový uzel (firmware, bare-metal C, avr-gcc ≥ 12 / avr-libc ≥ 2.1)

### 1.1 Priority
1. **Energie** – průměrný odběr ~12 µA (sonda ~1,4 mAs + šifrované TX ~1,7 mAs na cyklus + spánek 1,5 µA), výdrž omezená samovybíjením článků (10+ let).
2. **Přesnost** – hladina s rozlišením < 1 mm, šum < ±2 mm.
3. **Kvalita** – každý paket nese stav senzorů (flagy chyb), server pozná ztrátu paketů a restart uzlu.

### 1.2 Časování
- Normální interval **300 s** (RTC PIT 1 s tick, WDT 8 s se kopne při každém probuzení).
- **Adaptivní rychlý režim:** pokud se napětí sondy mezi měřeními změní o víc než `FAST_DELTA_100UV`
  (≈ 1 cm), další měření za **60 s**. Návrat na 300 s po 3 klidných měřeních. Pojistka: max. 90 rychlých
  cyklů po sobě (ochrana baterie při vadné sondě).
- **Dálková změna intervalu se NEDĚLÁ.** Downlink by vyžadoval RX okno po každém TX, Python na Pi nemá
  deterministickou latenci a energeticky to nemá smysl (rozpočet dominuje sonda a samovybíjení).

### 1.3 Měření
- **Sonda:** zapnout 5V boost (TPS63900), ustálení `PROBE_SETTLE_MS` (výchozí 150 ms), pak 16 dávek
  × 64 vzorků (ADC0 BURST, 12 bit, ref. 2,500 V) rozložených do ~100 ms; dávky seřadit, zprůměrovat
  prostředních 8 (ořezaný průměr 512 vzorků); okno ~70 ms potlačí vlny a rušení, energii určuje hlavně
  ustálení sondy, ne počet vzorků. Výstup = **napětí sondy v jednotkách 0,1 mV**.
  Přepočet na cm dělá **server** (vícebodová kalibrace) → kalibrace bez přeflashování.
  Saturace ADC → flag `PROBE_CLIP`.
- **Baterie:** 1× za hodinu (mezi tím se posílá poslední hodnota); dělič přes P-MOSFET, ustálení 20 ms,
  ref. 1,024 V, 4 × 64 vzorků, medián.
- **SHT40:** každý cyklus (5 min), high-precision, CRC-8. Bez ohřevu – čidlo je v uzavřené krabičce se sondou.
- ADC driver musí odpovídat **tinyAVR řady 2** (`ADC0.CTRLC.REFSEL`, `COMMAND.MODE`, 32bit `RESULT`).

### 1.4 Rádio
- CC1101, **433,92 MHz** (`FREQ = 0x10B071`, krystal 26 MHz), 2-FSK, 4,8 kBd, dev. 25,4 kHz,
  RX BW 101 kHz, 4 B preambule, sync 30/32, whitening, HW CRC-16, **proměnná délka** (max 61 B), +10 dBm.
- Po probuzení ze SLEEP znovu zapsat TEST0–2 (nejsou zachovány).
- Data bez ACK a bez opakování (ztráta 1 paketu = mezera 5 min, server ji eviduje podle čítače).
- Odhad dosahu (citlivost ~−108 dBm, +10 dBm, rezerva 10 dB): volný prostor stovky metrů; ze šachty pod
  betonovým/plastovým víkem a přes zdi domu typicky **30–100 m**. Ověřit podle RSSI v dashboardu
  (> −95 dBm spolehlivé, < −105 dBm na hraně).

### 1.5 Protokol v3 – šifrovaný a podepsaný (Little-Endian)

| Rámec | Délka | Obsah |
|---|---|---|
| DATA (čidlo → hub) | 21 B | `0x03` · `node_id` u16 · `ctr` u32 · šifrovaná telemetrie 10 B · MAC 4 B |
| PAIR_REQ (čidlo → hub) | 36 B | `0x04` · `node_id` u16 · veřejný klíč X25519 čidla (32 B, nový po každém zapnutí) · stav (b0 = čidlo má klíč) |
| PAIR_ACK (hub → čidlo) | 35 B | `0x05` · `node_id` u16 · veřejný klíč X25519 hubu (32 B) – přijme jen **nespárované** čidlo |
| UNPAIR (hub → čidlo) | 19 B | `0x06` · `node_id` u16 · CMAC(Kmac, `0x06‖node_id‖veřejný klíč čidla`) 16 B – jediné, co přijme spárované čidlo |

- **Telemetrie** (`struct '<BHhHHB'`): `flags` (b0 BAT_ERR, b1 SHT_ERR, b2 PROBE_ERR, b3 PROBE_CLIP,
  b4 BAT_LOW < 3,3 V jen varování, b5 BOOT, b6 WDT_RESET, b7 FAST) · `vbat_mv` · `temp_x100` (−32768 = chyba)
  · `hum_x100` (0xFFFF = chyba) · `probe_100uv` (0xFFFF = chyba) · `fw` (verze firmwaru).
- **Šifrování:** AES-128-CTR, keystream = AES(Kenc, `node_id|ctr|0…`). **Autentizace:** AES-CMAC(Kmac,
  bajty 0–16) zkrácený na 4 B. **Ochrana proti přehrání:** `ctr` je 32bit, za celý život čidla jen roste
  (v EEPROM se rezervuje po 1024, takže se nikdy neopakuje ani po resetu); hub přijme jen `ctr` > poslední.
- **Klíče:** X25519 → sdílené S; K0 = S[0..15] ⊕ S[16..31]; Kenc = AES(K0, "RTK-ENC-KEY"),
  Kmac = AES(K0, "RTK-MAC-KEY"). Odposlech párování klíč neprozradí. Klíče v EEPROM čidla
  (fuse EESAVE – přežijí i přehrání firmwaru) a v DB hubu (nikdy se neposílají do prohlížeče).
- `node_id` = CRC-16 výrobního sériového čísla čipu (zapsat na štítek čidla).

### 1.6 Párování a odpárování (návrh zadavatele)
**Princip:** spárované čidlo patří svému hubu. Jiný hub si ho nepřevezme (PAIR_ACK spárované čidlo
ignoruje); uvolnit ho může jen jeho hub podepsaným UNPAIR, a to jen během 10 min po vložení baterie.
Odpárování tedy vyžaduje **přihlášení do hubu i fyzický přístup k čidlu**.

1. Po vložení baterie (power-on reset) čidlo **10 minut** posílá každých 5 s PAIR_REQ a 0,6 s poslouchá.
   Spárované čidlo přitom normálně měří a vysílá se svým klíčem (každou minutu).
2. **Nové / nespárované čidlo** nabízí párování, dokud ho někdo nespáruje: v okně každých 5 s, pak
   každou minutu, po prvním dni každých 5 min. Uživatel v hubu u daného ID klikne **Spárovat** (ověří
   ID se štítkem) → PAIR_ACK. Nový klíč začne na hubu platit po prvním ověřeném DATA rámci.
   Hub bez jediného čidla (první instalace) přijme první nabídku automaticky.
3. **Odpárování:** v hubu *Odpárovat* → hub okamžitě přestane přijímat data čidla (klíč si zatím nechá).
   Pořadí nevadí – kdykoli potom (nebo předtím, dokud běží 10min okno) se vloží baterie: čidlo pošle
   PAIR_REQ se stavem „spárováno“, hub odpoví UNPAIR, čidlo ověří podpis svým klíčem, klíč smaže a
   pošle PAIR_REQ „nespárováno“ = potvrzení → hub smaže klíč i čidlo (naměřená data zůstanou).
   Bez potvrzení hub čeká libovolně dlouho, dá se zrušit.
4. **Znovu spárovat** (nový klíč, stejný hub) = UNPAIR + PAIR_ACK v jednom okně; nastavení i historie zůstanou.
5. **Výměna baterie** bez akce v hubu nic nemění – čidlo pokračuje se starým klíčem.
6. **Ztracené / zničené čidlo:** *Odebrat bez čidla* smaže klíč hned. Kdyby se čidlo našlo, zůstane
   zamčené na smazaný klíč – obnova jen přeprogramováním přes UPDI (vymazání EEPROM).
7. **Ochrana proti přehrání UNPAIR:** podpis zahrnuje veřejný klíč z aktuálního PAIR_REQ, který čidlo
   generuje po každém zapnutí znovu – nahraný UNPAIR z dřívějška je neplatný.
8. Výpočet X25519 trvá na ATtiny ~9 s (CPU dočasně na 10 MHz), jednou po vložení baterie. Entropie: šum ADC
   (teplotní čidlo), jitter mezi 32kHz a hlavním oscilátorem, sériové číslo, pool uložený v EEPROM.
9. **Záloha:** klíče čidel jsou jen v DB hubu (`/var/lib/retencka`). Při ztrátě SD karty bez zálohy
   zůstanou čidla zamčená (UPDI) – zálohovat DB.

### 1.7 Baterie
- Čidlo se **nikdy samo nevypíná** (elektronika funguje do ~1,8 V); pod 3,3 V nastaví flag BAT_LOW = jen varování.
- Server: křivka Li-FeS2 → 0 % při 3,0 V (1,0 V/čl.), ~1 % při 3,3 V; pod 5 % varování „vybitá baterie“.

### 1.7 Otevřené HW otázky (ověřit na schématu)
- Napájení MCU / CC1101 / SHT40 (CC1101 a SHT40 max. 3,6 V; 3× Li-FeS2 = až 5,4 V) – předpoklad: 3,3V LDO s Iq < 1 µA.
- ~~Dělič sondy~~ ověřeno: sonda 0–3,3 V, dělič 100k/220k → max 2,27 V na pinu proti ref. 2,5 V (OK).
- ~~Dělič baterie~~ ověřeno: 1M/220k proti ref. 1,024 V → rozsah do 5,68 V (OK).
- Typ sondy a její doba náběhu (`PROBE_SETTLE_MS`).

---

## 2. Server na Raspberry Pi Zero 2 W

### 2.1 Mantinely
- RSS procesu **< 35 MB** (cíl ~25 MB), `MemoryMax=64M` v systemd.
- **Bez Dockeru** (dockerd ≈ 60 MB RAM a zbytečné zápisy na SD).
- **Bez pip a bez frameworků**: jen standardní knihovna Pythonu (vlastní striktní HTTP/1.1 server nad asyncio)
  + `python3-spidev` (+ volitelně `python3-libgpiod`) z apt. Běží i na Pi 3 B/3 B+/4/5 (stejný konektor, stejná instalace). Aktualizace aplikace = jen výměna souborů.
  (aiohttp by přidal ~15 MB RSS a lišil by se mezi verzemi OS.)
- Žádné server-side šablony: statické HTML/JS/CSS, data přes JSON. **Grafy kreslí prohlížeč**
  (Chart.js hostovaný lokálně), server vrací max. ~600 předem zprůměrovaných bodů.
- Web musí fungovat i bez internetu (žádné CDN – font Outfit i Chart.js lokálně).

### 2.2 Procesy a porty
| Port | Kdo | Přístup |
|---|---|---|
| 80 | web aplikace (login) | LAN + Cloudflare Tunnel |
| 8081 | Home Assistant API | jen LAN, `Authorization: Bearer <token>`, **nikdy do tunelu** |
| 80 (jen v AP režimu) | captive portál | jen klienti hotspotu |

Jeden proces `retencka` (asyncio, 2 listenery), běží jako neprivilegovaný uživatel s `CAP_NET_BIND_SERVICE`.

### 2.3 Rádio (CC1101 přes spidev)
- Stejná konfigurace jako uzel (proměnná délka), append status (RSSI, LQI, CRC_OK). Na PAIR_REQ hub
  odpovídá okamžitě z rádiového vlákna (čidlo poslouchá jen 0,6 s).
- Příjem: GDO0 přes `gpiod` (edge), fallback polling `RXBYTES` po 200 ms.
- **Watchdog rádia:** každých 30 s kontrola `MARCSTATE` a zpětné čtení konfiguračních registrů →
  při nesouladu kompletní reset + rekonfigurace; výjimka SPI → zavřít a znovu otevřít spidev.
  Když nepřijde paket 3× interval, preventivní reset rádia.
- **Více čidel na jeden hub (max. 16):** server přijímá jen spárovaná čidla s platným podpisem. Každé čidlo
  má vlastní název, nádrž, kalibraci, přesnost sondy, **vlastní dopouštění**, grafy, export a entity v HA.

### 2.4 Data
- SQLite, WAL, `synchronous=NORMAL`, kompaktní schéma (celá čísla, epoch s) ≈ 30 B/záznam.
- Ukládá se **surová** hodnota (`probe_100uv`, `vbat_mv`, …) + vypočtená hladina v mm, takže změna kalibrace jde
  zpětně přepočítat.
- Retence: surová data 2 roky, hodinové agregace navždy. Promazávání 1× denně.
- Historie: SQL bucketing (`GROUP BY ts / bucket`) na max. ~600 bodů.
- Konfigurace a uživatelé v SQLite (transakce = atomické zápisy, žádné rozbité JSON po výpadku).

### 2.5 Výpočty (převzato a opraveno ze `old/`)
- Hladina: **2 kalibrační body** (napětí sondy → cm vodního sloupce; nový ADC je lineární, ESP32 potřeboval 3–4)
  + výška sondy nade dnem (offset), ořez na 0…max. hloubka; objem = plocha × výška.
- **Přesnost sondy** (% z rozsahu) × rozsah = mrtvé pásmo (např. 0,5 % × 200 cm = ±1 cm). Filtr: medián ze 3
  + exponenciální vyhlazení změn menších než 2× mrtvé pásmo, větší změny projdou hned → graf bez „zubů“.
  Ukládá se surová i vyhlazená hladina.
- Spotřeba / přítok: hystereze na vyhlazené řadě s polovičním mrtvým pásmem (šum se nepočítá, pomalý odběr ano).
- Dopouštění – **zvlášť pro každou nádrž** (vlastní ventil/zónu, pravidla, průtok): manuální na cílové %,
  automatické pod prahem, odložení při předpovědi deště (Open-Meteo), uzavřená smyčka – zastavení po
  dosažení cíle, kalibrace reálného průtoku, detekce poruchy ventilu. Ventilu se vždy nastaví i vlastní
  časovač (pojistka při výpadku hubu). Každá akce je v audit logu.
- Systémy dopouštění jsou za rozhraním „provider“. Teď: **Hunter Hydrawise** (oficiální REST API s API
  klíčem – Hunter jej uvádí jako veřejné API pro domácí uživatele; GraphQL API aplikace není oficiálně
  dokumentované). Kandidáti do budoucna: OpenSprinkler (lokální HTTP API), Rachio (oficiální cloud API),
  Gardena smart system (oficiální Husqvarna API), Shelly / libovolné relé (lokální HTTP/MQTT, bez cloudu),
  Home Assistant (zapnutí libovolného ventilu přes jeho REST API), Rain Bird / B-hyve (jen neoficiální API).
- Baterie Li-FeS2 3S: odhad SoC z napěťové křivky, odhad výdrže z trendu.
- Stav: `online` (< 3 intervaly), `stale` (< 30 min), `offline`.

### 2.6 Bezpečnost webu (vystaveno přes Cloudflare)
- **Doporučeno:** Cloudflare Access (Zero Trust) před tunelem – e-mail OTP / Google login.
- Hesla: `scrypt` (N=2^14, r=8, p=1) – výpočty serializované (RAM), min. 10 znaků.
- 2FA se nepoužívá (rozhodnutí zadavatele) – venkovní přístup chránit Cloudflare Access.
- Session na serveru: náhodný 256bit token, v DB jen SHA-256 hash; cookie `HttpOnly`, `SameSite=Lax`
  (Strict by rozbil návrat z Cloudflare Access),
  `Secure` přes HTTPS (`__Host-` prefix); idle timeout 7 dní, absolutní 30 dní; změna hesla zneplatní
  všechny session.
- CSRF: kontrola `Origin` + token v hlavičce `X-CSRF-Token` (bez něj žádná změna neprojde).
- Rate limiting: per IP i per uživatel, exponenciální zpomalení, zámek po 5 chybách (15 min);
  IP z `CF-Connecting-IP` se věří jen u spojení z loopbacku (cloudflared).
- Bezpečnostní hlavičky: striktní CSP (`default-src 'self'`, bez inline skriptů), HSTS přes HTTPS,
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`.
- **Všechny** `/api/*` vyžadují session (role `user` čte, `admin` mění). Tajné údaje (Hydrawise klíč)
  se do prohlížeče nikdy nevracejí celé.
- Audit log s escapováním (žádné XSS přes jméno při neúspěšném loginu).
- První přihlášení: výchozí účet `admin` + **heslo ze štítku** (stejné jako heslo hotspotu) → povinné vytvoření
  vlastního účtu (jiné jméno i heslo) → výchozí přístup přestane fungovat (existuje uživatel).
  Obnova továrního stavu = smazání uživatelů → opět heslo ze štítku.
- Tajemství nikdy v gitu (`.gitignore`), data mimo adresář aplikace (`/var/lib/retencka`).

### 2.7 Home Assistant
- `GET :8081/api/v1/water/state?node=XXXX` s Bearer tokenem (bez `node` = první čidlo); YAML generuje
  admin UI pro všechna čidla (první: `sensor.retencka_hladina` …, další `sensor.retencka_<nazev>_hladina` …).
- JSON: `water_level_cm, water_level_percent, volume_liters, temperature, humidity, battery_voltage,
  battery_percent, battery_low, rssi, last_seen_seconds_ago, status, is_refilling`.
- YAML: platforma `rest` (jeden request, více entit), `device_class`, `state_class`,
  `unit_of_measurement`, `availability` z pole `status`.

### 2.8 Dashboard
- Vizuálně **shodný se `old/`** (dark glassmorphism, Outfit, animovaná nádrž, stat boxy, karty grafů,
  záložky Dashboard / Nastavení / Logování / Uživatelé / Můj účet).
- Grafy: hladina (se zvýrazněním dopouštění), spotřeba/den, dopouštění/den, klima, počasí 7 dní, baterie.
- U grafů s historií (hladina, spotřeba, dopouštění, klima, baterie) volba 24 h / 7 d / 30 d / 1 rok
  **+ vlastní rozsah od–do** (date pickery). Předpověď počasí má pevný výhled 7 dní.
- Odstranit ESP32 relikty (OTA mód, intervaly den/noc, sync na celé hodiny) → nahradit stavem rádia
  (RSSI, ztráta paketů, ID uzlu, flagy chyb).

---

## 3. Systémová vrstva (Pi Zero 2 W, Raspberry Pi OS Lite)

### 3.1 Odolnost
- Root FS **read-only** (overlayfs), data na samostatném oddílu `/var/lib/retencka` (ext4, `commit=60`).
- Logy jen v RAM (`journald Storage=volatile`), `tmpfs` pro `/tmp`.
- **HW watchdog** (`RuntimeWatchdogSec=15`) + systemd `WatchdogSec` (aplikace posílá `WATCHDOG=1` jen když
  běží smyčka i rádiové vlákno) + `Restart=always`.
- Čas bez RTC: `fake-hwclock` + `systemd-timesyncd`. Měření před synchronizací se označí a po synchronizaci
  se **automaticky posunou** o změřený skok (referencí je monotónní čas systému). RTC modul (DS3231) je
  potřeba jen pro instalace trvale bez internetu.

### 3.2 Aktualizace z GitHubu
- Updater (oneshot, systemd timer 1× denně + tlačítko v adminu) stáhne nejnovější **GitHub Release**,
  ověří **Ed25519 podpis** (`openssl`, veřejný klíč v zařízení), rozbalí do `/opt/retencka/releases/<ver>`,
  přepne symlink `current` atomicky, restartuje službu.
- Pokud do 120 s neodpoví `/health`, **automatický rollback** na předchozí verzi.
- Kanály `stable` / `beta`, migrace DB jsou dopředně kompatibilní.

### 3.3 Wi-Fi provisioning (NetworkManager)
- Bez známé sítě (nebo po 3 min bez spojení) → hotspot `Retencka-XXXX` (XXXX = konec MAC), WPA2, heslo
  generované pro každý kus (na štítku, uloženo v `/boot/firmware/retencka.txt`; stejné heslo slouží k prvnímu
  přihlášení na web).
- Telefon sám nabídne „Přihlásit se k síti“: DNS wildcard + DHCP option 114 (RFC 8910); portál je i na
  `http://retencka-xxxx.local` a `http://192.168.4.1`.
- Captive portál: odpovídá na detekční URL (Android/iOS/Windows), seznam SSID (sken před spuštěním AP +
  tlačítko „znovu načíst“), zadání hesla → pokus o připojení → při neúspěchu se hotspot vrátí s chybou.
- Během AP režimu se každé 3 min (pokud není připojen klient) zkusí známá síť (router po výpadku proudu
  startuje pomaleji než Pi).
- Zero 2 W podporuje jen 2,4 GHz.

### 3.4 Jméno v síti (mDNS / Avahi)
- Hostname `retencka-xxxx` (unikátní, z MAC) → vždy dostupné `retencka-xxxx.local`.
- Alias `retencka.local` si zabere první zařízení v síti (avahi-publish; při kolizi se nepublikuje),
  takže se víc kusů v jedné síti „nepoperou“. Web ukazuje oba názvy.

### 3.5 Instalace
- `system/install.sh` – idempotentní instalace na čistý Pi OS Lite (balíčky, uživatel, služby, SPI,
  watchdog, overlay, Avahi, NetworkManager, polkit, generování tajemství).
