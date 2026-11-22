# Diagnostika čidla přes programátor

Když se čidlo neobjeví na hubu nebo posílá divné hodnoty, diagnostický firmware ukáže, co se v čidle
děje: jestli rádio CC1101 odpovídá, vysílá a slyší ostatní čidla v dosahu, jestli odpovídá SHT40 a
jaké napětí dává sonda. Všechno se čte **jen přes UPDI programátor**, na hubu se nic nemění. Funguje
i s úplně mrtvým rádiem.

## Postup

1. Programátor zapojte stejně jako při nahrávání firmwaru ([programovani_uzlu.md](programovani_uzlu.md)).
2. Dvojklik na **`uzel/6-diagnostika.cmd`** – nahraje diagnostický firmware.
   Párovací klíč v čidle zůstává (EEPROM se při nahrávání nemaže).
3. Nechte čidlo běžet **aspoň 5 minut** (napájené baterií, nebo 3,3 V z programátoru).
   Programátor může zůstat zapojený. Sonda může být připojená – změří se i ona.
4. Dvojklik na **`uzel/7-precist-diagnostiku.cmd`** – vyčte výsledek a vypíše, co je v pořádku
   a co zkontrolovat. Vyčtení čidlo restartuje a test začne znovu.
5. Zpět na normální firmware: **`uzel/3-aktualizace-firmwaru.cmd`** (párování zůstane).

## Co se testuje (každých ~6 s)

**Test linek SPI** – proběhne jako první a nespoléhá na čtení dat přes MISO, takže rozhodne
i tehdy, když se po SPI čtou jen nuly:

| Test | Co znamená výsledek |
|---|---|
| MISO volná (CSn=1, pull-up) | NE = linku SO/MISO něco drží v nule, i když rádio není vybrané (vadný výstup SO čipu) |
| MISO spojená s MOSI / SCLK | ANO = můstek mezi linkami |
| Rádio stáhne SO po CSn=0 / pustí po CSn=1 | čip reaguje na výběr (CSn) a běží mu krystal |
| Hodiny na GDO0 po resetu | ANO = čip je napájený a krystal 26 MHz kmitá (výchozí výstup CLK_XOSC/192) |
| Rádio přijme zápis (GDO0 0 a 1) | do IOCFG0 se zapíše „trvale 0“ a „trvale 1“; ANO = rádio dostává příkazy (CSn, SCLK, MOSI v pořádku) |
| Rádio ovládá pin SO (0 a 1) | totéž přes IOCFG1 na pinu SO; NE při jinak funkčním zápisu = vadný výstup SO čipu |

Výpis z toho udělá **VERDIKT** (např. „rádio po CSn=0 nestáhne SO – vadný čip nebo spoj CSn na modulu“).

**Rádio a příjem:**

| Test | Co znamená NE |
|---|---|
| Rádio odpovídá (SO, krystal) | CC1101 nemá napájení, nebo vadný spoj CSn (PA4) / SO-MISO (PA2), nekmitá krystal 26 MHz |
| PARTNUM / VERSION | má být 0x00 / 0x14; jiné číslo = čip není CC1101 nebo SPI čte nesmysly |
| Zápis a čtení registru | vadný spoj SI-MOSI (PA1) nebo SCLK (PA3) |
| PLL zamčená | krystal 26 MHz a jeho kondenzátory, napájení rádia |
| Vysílání dokončeno | rádio nedokončilo vysílání (napájení při zátěži ~30 mA, PLL) |
| GDO0 při vysílání | přerušený spoj GDO0 (PB2) – čidlo pak nepozná odpověď hubu, **párování nefunguje** |
| Šum kanálu | nad −80 dBm = rušení nebo zvlněné napájení rádia |
| Zachyceno od startu | pakety ostatních čidel v dosahu (anemometr vysílá každou minutu, čidla retenčky každých 5 min); 0 po 5 min = nefunguje příjem (anténa, krystal, GDO0) |

**Čidlo nádrže:**

| Test | Co znamená výsledek |
|---|---|
| SHT40 | teplota a vlhkost v krabičce; NEODPOVÍDÁ = spoje SDA (PB1, pin 8) / SCL (PB0, pin 9), napájení senzoru, pull-upy I2C |
| Sonda (napětí) | výstup sondy s 5 V zapnutými jen na měření; odpojená sonda ≈ 0 V, jinak porovnejte s výškou hladiny. „Na plném rozsahu ADC“ = zkrat signálu na 5 V nebo vadná sonda |

Výpis navíc ukáže **ID čidla** (stejné jako na hubu), **příčinu posledního restartu** (podpětí
= slabá baterie nebo napájení), napětí baterie a jestli je v čidle **uložený párovací klíč**.

## Technicky

- Firmware `uzel/tools/diag.c`, překlad `make diag`; hotový `uzel/tools/diag.hex` je v repozitáři.
  Sdílí `power.c`, `adc.c`, `sht40.c` a `cc1101.c` s normálním firmwarem; diagnostické části
  ovladače rádia se překládají jen s `-DCC1101_DIAG`, normální firmware se tím nemění.
- Výsledek je v EEPROM od adresy 0xC0, 39 B (klíče párování jsou na 0x00–0x75, diagnostika je jen čte).
- V každém cyklu čidlo jednou vysílá testovací paket typu 0x07 bez šifrování. Hub ho započítá
  jako cizí paket a zahodí.
