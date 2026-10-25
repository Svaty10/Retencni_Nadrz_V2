# Programování čidla – krok za krokem

Do čidla (čip ATtiny1624) se firmware nahrává jedním vodičem **UPDI**. Jako programátor poslouží
**Arduino Uno nebo Nano** (běžné klony s čipem CH340 jsou v pořádku) a jeden odpor.
Nemusíte nic instalovat ani nic psát do příkazové řádky – stačí dvojklik.

## 1. Co budete potřebovat
- Arduino **Uno** nebo **Nano** a jeho USB kabel
  (nefungují typy s USB přímo v hlavním čipu: Leonardo, Micro, Nano Every, Uno R4),
- **odpor 4,7 kΩ** (jde 1–4,7 kΩ), nebo lépe **Schottkyho diodu** (1N5817, 1N5819, BAT85, BAT43),
- 4–5 drátů (dupont),
- **napájení čidla** – vyberte jednu možnost v bodě 3.

## 2. Zapojení signálu

Jsou dvě možnosti. **Klony Arduina s čipem CH340 (nejběžnější) potřebují variantu B** – mají kontrolky TX/RX
napevno na pinech 0/1 a ty signál na UPDI nepustí. Nevíte-li, zkuste A; skript při neúspěchu poradí.

### B) Arduino jako programátor jtag2updi – pin 6 (pro klony s CH340)
1. **Jednou** nahrajte do Arduina program programátoru: Arduino jen v USB, **žádné dráty ani kondenzátor**
   na Arduinu, dvojklik na **`0-priprav-arduino.cmd`**. (Přepíše program, který v Arduinu byl.)
2. Zapojení:

```
 Arduino                                  Čidlo
 pin 6 ──────[ 4,7 kΩ ]────────────────── UPDI   (pin 10 čipu ATtiny1624)
 GND ──────────────────────────────────── GND    (pin 14)
 (RESET nezapojovat, kondenzátor není potřeba)
```

### A) Arduino jako převodník – piny 0 a 1 (originální Uno s čipem 16U2, převodníky)

```
 Arduino                                  Čidlo
 RESET ──┐
 GND  ───┴── drát RESET–GND (Arduino se tím „uspí“ a slouží jen jako převodník USB)
 D0 (RX←) ──[ 4,7 kΩ ]───────┬────────── UPDI   (pin 10 čipu ATtiny1624)
 D1 (TX→) ───────────────────┘
 GND ──────────────────────────────────── GND    (pin 14)
```

S diodou místo odporu (šetrnější – do čidla se nedostane 5 V z Arduina):

```
 D0 (RX←) ───|◄───────────────┬────────── UPDI   (proužek na diodě směrem k Arduinu)
 D1 (TX→) ────────────────────┘
```

Piny D0 a D1 jsou zapojené „obráceně“ schválně – jsou popsané z pohledu čipu Arduina, který je teď v resetu.
Na desce čidla hledejte plošky / konektor **UPDI – GND**; pokud nejsou, je UPDI **pin 10**, GND **pin 14**.

**Proč odpor (nebo dioda) nesmí chybět:** Arduino posílá signál s napětím 5 V, čidlo pracuje s 3,3 V.
Bez omezení by napětí protékalo vstupem čipu do celého čidla (CC1101 a SHT40 snesou nejvýš 3,6 V).
Odpor proud omezí na neškodných ~0,2 mA, dioda 5 V nepustí vůbec.
**Čidlo mějte při programování napájené** – z Arduina Una (3,3 V, možnost B v bodě 3) nebo z baterie:
napájení připojit dřív než drát UPDI a odpojit UPDI dřív než napájení. Nenapájené čidlo by Arduino
přes vstup UPDI částečně napájelo.

## 3. Napájení čidla – vyberte JEDNU možnost (nikdy obě najednou)

**A) Baterie** – vložte do čidla jeho baterie. Funguje s každým Arduinem.

**B) Z Arduina Uno** – baterie **vytáhněte** a připojte:
```
 Uno 3.3V ──────────── + (kontakt baterie v čidle)
 Uno GND  ──────────── − (kontakt baterie v čidle)
```
- Jen **Uno**, které má pro 3,3 V vlastní malý stabilizátor (u originálu ano; u klonu hledejte
  u konektoru malou součástku se 3 nožičkami s nápisem 3.3 / 33 / LP2985 / AMS1117).
  **Nano s CH340 ne** – jeho pin 3,3 V dá jen pár miliampér.
- Připojujte na **kontakty baterie**, ne přímo na čip – napětí tak projde regulátorem čidla jako z baterie.
- Čidlo pak uvidí „baterii“ 3,3 V a nahlásí slabou baterii – při programování to nevadí.

**Nikdy nepřipojujte pin 5V Arduina k čidlu.**

## 4. Nahrání (dvojklik)
1. Zapojte podle bodu 2 a 3, Arduino připojte USB kabelem k počítači.
2. Otevřete složku projektu **`Retencni_Nadrz_V2\uzel`**
   (u vás `C:\Users\matej\OneDrive\Projekty\Retencni_Nadrz_V2\uzel`).
3. Dvojklik na **`1-test-spojeni.cmd`** – otevře se okno; na konci má být
   **„Spojení OK (signatura ATtiny1624 = 1E 94 2A)“**. Okno zavřete libovolnou klávesou.
4. **Nový čip (poprvé):** dvojklik na **`2-novy-cip.cmd`** – nastaví čip (fuses) a nahraje firmware.
   **Pozdější aktualizace firmwaru:** dvojklik na **`3-aktualizace-firmwaru.cmd`**.
5. Na konci: **„Hotovo. Čidlo se restartovalo a 10 minut nabízí párování“**.

Při prvním spuštění si skript sám stáhne program **avrdude** a hotový **firmware** z GitHubu
(oboje ověří kontrolním součtem) a Arduino na USB najde sám. Potřebuje k tomu internet.

### Nastavení čipu (fuses) – nic nevyplňujete
Soubor `2-novy-cip.cmd` zapíše správné hodnoty sám. Stačí jednou na každý nový čip; zopakování nevadí.
Pro představu, co nastavuje:

| Fuse | Hodnota | Význam |
|---|---|---|
| BODCFG | `0x04` | pojistka proti podpětí 1,8 V (firmware se sám vypne dřív – na 3,0 V baterie) |
| OSCCFG | `0x02` | vnitřní oscilátor 20 MHz – **čip na něm nikdy neběží přímo**, vždy se dělí (viz níže) |
| SYSCFG0 | `0xC5` | pin UPDI zůstává pro programování · klíče párování v EEPROM přežijí nové nahrání |

### Rychlost čipu (max. 10 MHz)
Při napájení 3,3 V smí ATtiny1624 běžet nejvýš na **10 MHz** (20 MHz až od 4,5 V). Firmware to dodržuje:
- po zapnutí i během měření a vysílání běží na **3,33 MHz** (20 MHz ÷ 6 – to je výchozí nastavení čipu
  hned po resetu, ještě než se spustí program),
- jen jednorázový výpočet šifrovacího klíče při párování (~9 s) běží na **10 MHz** (20 MHz ÷ 2), a to
  jen když baterie při zapnutí ukáže aspoň 3,1 V; jinak na **5 MHz** (20 MHz ÷ 4),
- **20 MHz se nikdy nepoužije.**

## 5. Po nahrání
1. Čidlo 10 minut nabízí párování. Na hubu: *Nastavení → Všechna čidla → Čidla → Přidat čidlo…*
   → v dialogu u nalezeného ID klikněte **Spárovat** (ID porovnejte se štítkem), zadejte **název** (povinný)
   a **Čekat na čidlo**. Bez potvrzení se čidlo
   nespáruje nikdy – ani první, ani k cizímu hubu.
2. Při napájení z Arduina (možnost B): po spárování odpojte Arduino a vložte baterie –
   čidlo pokračuje se stejným hubem.
3. Na štítek čidla napište jeho **ID** (ukáže ho hub).

Aktualizace firmwaru později = `3-aktualizace-firmwaru.cmd`. Párování zůstane.

### Smazání párování (`4-smazat-parovani.cmd`)
Když hub čidlo ukazuje jako **„cizí čidlo“**, čidlo má klíč, který hub nezná – bylo z hubu odebrané
(*Odebrat bez čidla*), nebo jde o nový hub bez zálohy. Takové čidlo se samo znovu nespáruje (z bezpečnosti
přijme nový klíč jen od hubu, který zná ten starý). Řešení:
1. Zapojit jako pro nahrání (bod 2 a 3), dvojklik na **`4-smazat-parovani.cmd`** – smaže EEPROM
   (klíč a čítač), firmware zůstane.
2. Odpojit a znovu připojit napájení (nebo vyndat a vložit baterii) – čidlo 10 minut nabízí párování.
3. Na hubu *Nastavení → Všechna čidla → Čidla → Přidat čidlo…*, v dialogu zadejte jeho ID a název a **Čekat na čidlo**.

## 6. Když to nejde

| Co vidíte | Co zkusit |
|---|---|
| `Nenalezen žádný USB sériový port` | Arduino není připojené, nebo chybí ovladač CH340 (stáhnout z webu výrobce WCH) |
| `Připojeno víc USB portů` | odpojte ostatní USB zařízení se sériovým portem (např. jiné Arduino) |
| **„Signál se nevrací“** | odpojte drát UPDI od čidla a test zopakujte. Pak „Arduino OK“ → čidlo drží linku: **nemá napájení** (bod 3) nebo je drát na jiném pinu než UPDI. Pořád nic → zapojení Arduina: RESET–GND, D0 (přes odpor) a D1 spojené v jednom bodě, dráty opravdu na D0/D1 (na Nanu RX0/TX1), kontakty v nepájivém poli. |
| **„Arduino neodpovídá ani jako převodník, ani jako jtag2updi“** | klon s CH340: spusťte `0-priprav-arduino.cmd` (bez drátů na Arduinu) a zapojte pin 6 podle varianty B |
| **„Arduino (jtag2updi) je v pořádku, ale čip neodpovídá“** | drát z pinu 6 přes odpor na UPDI (pin 10), GND, napájení čidla; na Arduinu nesmí být drát RESET–GND |
| **„Čip v čidle neodpovídá“** | Arduino je v pořádku, chyba je u čidla: napájení (bod 3), UPDI na pin 10, GND; zkuste `.
ahraj.ps1 -Test -Baud 57600` |
| `could not open port` | zavřete Arduino IDE (sériový monitor drží port) |
| `Device signature … expected 1E 94 2A` | špatný kontakt na UPDI nebo jiný čip |
| `… nemá správný kontrolní součet` | stažení se poškodilo – spusťte znovu |

---

## Pro pokročilé
- Příkazová řádka (PowerShell ve složce `uzel`): `.\nahraj.ps1 -Test`, `.\nahraj.ps1 -Fuses`, `.\nahraj.ps1`;
  volby `-Port COM3` (konkrétní port), `-Baud 57600` (pomalejší komunikace),
  `-Build` (přeložit firmware z této složky místo stažení; potřebuje
  [avr-gcc](https://github.com/ZakKemble/avr-gcc-build/releases) rozbalený v `C:\avr`).
  Zablokované skripty: `powershell -ExecutionPolicy Bypass -File .\nahraj.ps1 -Test`.
- Jiný programátor: USB-UART převodník 3,3 V (TX přes 4,7 kΩ na UPDI, RX přímo na UPDI, GND) nebo
  [Adafruit UPDI Friend](https://www.adafruit.com/product/5879) (přepínač na 3 V).
- Programátor (piny 0/1 = SerialUPDI, nebo pin 6 = jtag2updi) skript pozná sám; ručně `-Programmer serialupdi`
  / `-Programmer jtag2updi`. Program jtag2updi pro Uno/Nano je v `uzel\tools\jtag2updi` (zdroj, licence MIT,
  parametry sestavení v README tam) – sestavený tak, že se sám zotaví i bez restartu Arduina.
- ⚠️ Hodnotu SYSCFG0 (`fuse5`) neměňte – jiné nastavení by vypnulo UPDI a čip by šel naprogramovat jen
  vysokonapěťovým programátorem.
