# Changelog

Formát dle [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), verze dle [SemVer](https://semver.org/).

## [3.3.0] - 2026-10-11

### Změněno
- Párování „patří hubu“: spárované čidlo ignoruje cizí PAIR_ACK, přijme jen UNPAIR podepsaný svým klíčem (vázaný na nový veřejný klíč po každém zapnutí → nelze přehrát).
- Odpárování ve dvou krocích: hub hned přestane brát data, klíč se smaže až po potvrzení čidlem (vložení baterie, pořadí libovolné). *Odebrat bez čidla* pro ztracené čidlo.
- Znovu spárovat = UNPAIR + nový klíč v jednom okně, nastavení a historie zůstávají.
- Nespárované čidlo nabízí párování donekonečna: 10 min každých 5 s, pak 1× za minutu, po dni 1× za 5 min.
- PAIR_REQ 36 B (stav čidla), nový rámec UNPAIR 19 B; DB migrace v4 (`nodes.unpair_at`). Firmware 3.3.

## [3.2.0] - 2026-10-11

### Přidáno
- Šifrovaný a podepsaný rádiový protokol v3: AES-128-CTR + AES-CMAC, 32bit čítač proti přehrání, klíč z párování X25519 (firmware i server bez externích knihoven, ověřeno oficiálními vektory a vzájemnou interoperabilitou).
- Párování: po vložení baterie čidlo 10 min nabízí párování; bez potvrzení na hubu si ponechá starý klíč. Nový klíč platí až po prvním ověřeném paketu.
- Dopouštění zvlášť pro každou nádrž (vlastní ventil, pravidla, průtok); rozhraní pro další systémy dopouštění.
- Automatická oprava časových razítek po pozdní synchronizaci času (Pi bez RTC).

### Změněno
- Hotspot na adrese 192.168.4.1.
- Fuse EESAVE – klíče čidla přežijí přehrání firmwaru.

## [3.1.0] - 2026-10-11

### Změněno
- První přihlášení: výchozí účet `admin` + heslo ze štítku (= heslo hotspotu) → povinné vytvoření vlastního účtu, výchozí přístup se zruší.
- Hotspot: DHCP option 114 (RFC 8910) – telefon hned nabídne „Přihlásit se k síti“; portál i na `retencka-xxxx.local`.
- Firmware: baterie se měří 1× za hodinu, čidlo se nikdy samo nevypíná (pod 3,3 V jen varování); odstraněn ohřev SHT40; kratší okno vzorkování sondy.
- Baterie: křivka Li-FeS2 s 0 % při 3,0 V.
- Kalibrace: 2 body + výška sondy nade dnem; nové pole přesnost a rozsah sondy → mrtvé pásmo, vyhlazení hladiny (medián + adaptivní EMA) a výpočet spotřeby/přítoku bez šumu; ukládá se surová i vyhlazená hladina.

### Přidáno
- Více čidel na jeden hub (až 16): přidání podle ID nebo ze seznamu slyšených, vlastní nastavení/grafy/export/HA entity pro každé čidlo, výběr čidla pro dopouštění.

## [3.0.0] - 2026-10-11

Kompletní revize celého systému pro Raspberry Pi Zero 2 W a nasazení u koncových zákazníků.

### Uzel (firmware)
- **Opraveno:** ADC driver přepsán pro tinyAVR řady 2 (`CTRLC.REFSEL`, `COMMAND`, 32bit `RESULT`) – původní kód se pro ATtiny1624 nepřeložil.
- **Opraveno:** frekvence 433,92 MHz (`0x10B071`; dříve 434,00 MHz na obou stranách).
- Paket v2 (14 B): verze, flagy chyb, ID uzlu z výrobního sériového čísla, sekvenční číslo, napětí sondy (0,1 mV) místo pevného přepočtu na mm.
- Měření sondy: 16 dávek × 64 vzorků rozložených do ~100 ms, ořezaný průměr; ustálení 150 ms; detekce saturace.
- Interval 5 min, adaptivně 60 s při rychlé změně hladiny, 10× 60 s po zapnutí (instalace).
- Úsporný režim při vybité baterii (6 h) místo trvalého vypnutí; ohřev SHT40 proti kondenzaci.
- Obnova TEST registrů CC1101 po SLEEP, vypnuté vstupní buffery plovoucích pinů ve spánku.

### Server
- Náhrada FastAPI/Jinja vlastním HTTP serverem nad asyncio – **bez externích závislostí**, ~30 MB RSS.
- **Bezpečnost:** všechna API vyžadují přihlášení (dříve bylo možné bez přihlášení spustit dopouštění, číst Hydrawise klíč a podvrhnout data); session na serveru, CSRF, kontrola Origin, zpomalování a zamykání účtů, striktní CSP, oprava XSS v audit logu, maskování API klíče, první nastavení přes instalační kód.
- Home Assistant API na samostatném portu 8081 s Bearer tokenem + generátor YAML.
- Kalibrace na serveru (vícebodová, „skutečná hladina je teď X cm“), přepočet historie, korekce baterie.
- Párování čidla (sousední nádrže se nepletou), statistika ztrát paketů a RSSI.
- Dopouštění v uzavřené smyčce s bezpečnostními pojistkami (čerstvá data, max. doba, detekce poruchy ventilu, odklad při dešti, kalibrace průtoku).
- Spotřeba počítaná hysterezí (zachytí i pomalý odběr), přítok ze srážek.
- Grafy s volbou 24 h / 7 d / 30 d / rok **a vlastním rozsahem od–do**; vzhled převzat z původní verze; font a Chart.js lokálně.
- SQLite WAL, hodinové agregace, retence, samooprava poškozené DB; systemd watchdog.

### Systém
- Instalační skript, sandboxované systemd služby, HW watchdog, logy v RAM, vypnutý swap.
- Wi-Fi provisioning: hotspot `Retencka-XXXX` + captive portál, automatický návrat ke známé síti.
- Unikátní `retencka-xxxx.local` + sdílený alias `retencka.local` bez kolizí.
- Podepsané (Ed25519) aktualizace z GitHub Releases s automatickým rollbackem; GitHub Action pro vydání.

## [2.2.0] - 2026-10-10
- Přihlašování, role, brute-force ochrana, změna hesla.

## [2.1.0] - 2026-10-10
- Hydrawise, Open-Meteo, záložky dashboardu, CSV export.

## [2.0.0] - 2026-10-10
- Rozdělení na `uzel/`, `webserver/`, `docs/`; FastAPI server pro Pi 3 B+.

## [1.0.0] - 2026-10-10
- První firmware ATtiny1624.
