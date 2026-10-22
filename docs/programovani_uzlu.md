# Programování čidla (ATtiny1624 přes UPDI)

ATtiny1624 se programuje jedním vodičem **UPDI** (pin PA0). Jako programátor stačí **Arduino Uno / Nano**
a jeden odpor, nebo USB-UART převodník – metoda **SerialUPDI**, kterou umí nástroj `avrdude`.

## 1. Co budete potřebovat

| | Stačí | Pohodlnější (do budoucna) |
|---|---|---|
| Programátor | Arduino **Uno** nebo **Nano** (převodník 16U2 / CH340 / FT232 na desce) | [Adafruit UPDI Friend](https://www.adafruit.com/product/5879) (~7 $, přepínač 3 V/5 V, odpor uvnitř) nebo USB-UART 3,3 V |
| Odpor | 4,7 kΩ (funguje 1–4,7 kΩ) | – (UPDI Friend ho má) |
| Napájení čidla | jeho vlastní baterie | – |
| Kabely | 3–4 dráty (dupont) | programovací konektor / klips na desce čidla |

Arduino s USB přímo v hlavním čipu (Leonardo, Micro, Nano Every, Uno R4) tímto způsobem **nefunguje**.

## 2. Programy
**Nic instalovat nemusíte.** Skript `uzel\nahraj.ps1` si při prvním spuštění sám stáhne nástroj **avrdude**
a hotový **firmware** z GitHubu (vydání *uzel-latest*, sestavuje se automaticky při každé změně firmwaru),
oboje ověří kontrolním součtem SHA-256 a programátor (COM port) najde sám.

Jen pokud Windows nepozná převodník na Arduinu (klony Nano s čipem CH340), nainstalujte ovladač CH340.

## 3. Zapojení

**Nikdy nepropojujte napájení programátoru (5V / 3V3 / VCC) s čidlem.** CC1101 a SHT40 snesou max. 3,6 V.
Čidlo napájejte jeho bateriemi; s programátorem má společnou jen **zem**.

### a) Arduino Uno / Nano
Hlavní čip Arduina se drží v resetu a USB převodník na desce pak slouží jako „USB-UART“.
Piny 0 a 1 jsou popsané z pohledu hlavního čipu, proto je zapojení „obráceně“:

```
 Arduino                              Čidlo
 RESET ──┐
 GND  ───┴─── (drát RESET–GND drží hlavní čip v resetu)
 D0 (RX←) ──[ 4,7 kΩ ]─────┬──────── UPDI (PA0, pin 10 ATtiny1624)
 D1 (TX→) ─────────────────┘
 GND ──────────────────────────────── GND  (pin 14)
```

Arduino má logiku 5 V a čidlo 3,3 V. Napětí nad napájením čidla teče přes ochranné diody pinu do jeho
3,3V větve – odpor 4,7 kΩ (spolu s 1 kΩ na desce Arduina) to omezí na ~0,2 mA, což napájená větev
(baterie v čidle!) bez problému pohltí. **Nikdy bez odporu a nikdy bez napájení čidla.**

**Napájení čidla z Arduina místo baterií** – jen **Arduino Uno** (pin 3,3 V dá ~150 mA). U klonů Nano s CH340
je pin 3,3 V jen pomocné napětí převodníku (pár mA) – nepoužívat.

```
 Uno 3.3V ──────────── + vstup baterie čidla   (baterie VYTAŽENÉ – nikdy oba zdroje najednou)
 Uno GND  ──────────── GND / − baterie
```

- Připojte na **vstup baterie**, ne až na 3,3V větev za regulátorem (zpětné napájení LDO).
- Po nahrání čidlo hned vysílá (párování, ~30 mA ve špičkách) – Uno to utáhne.
- Čidlo uvidí „baterii“ 3,3 V a hlásí slabou baterii – při programování nevadí.
- Pin 3,3 V nemění logiku Arduina: D0 dál dává 5 V, odpor nebo dioda zůstává.

**Nejšetrnější varianta – Schottkyho dioda místo odporu** (1N5817, 1N5819, BAT85, BAT43 …):

```
 D0 (RX←) ───|◄───────────┬──────── UPDI     (proužek = katoda směrem k Arduinu)
 D1 (TX→) ────────────────┘
```

Dioda linku jen stahuje k nule, klidovou úroveň drží čidlo svým pull-upem na 3,3 V – 5 V z Arduina se
do čidla vůbec nedostane.

### b) USB-UART převodník 3,3 V / UPDI Friend
```
 TX  ──────[ 4,7 kΩ ]──────┬──────── UPDI
 RX  ──────────────────────┘
 GND ──────────────────────────────── GND
```
(UPDI Friend: přepínač na **3 V**, jeho výstup UPDI rovnou na UPDI čidla, GND na GND.)

### Kde je UPDI na čidle
Na desce hledejte programovací plošky / konektor **UPDI – GND (– VCC)**. Pokud tam nejsou, UPDI je
**pin 10** ATtiny1624 (SOIC-14), GND **pin 14**, VDD **pin 1**.

## 4. Nahrání
1. Zapojte podle bodu 3, Arduino připojte USB kabelem k PC, do čidla vložte baterie.
2. Ve složce `uzel` klikněte pravým tlačítkem na `nahraj.ps1` → **Spustit v prostředí PowerShell**
   (= aktualizace firmwaru), nebo v PowerShellu ve složce `uzel`:

```powershell
.\nahraj.ps1 -Test      # 1) spojení: musí vypsat signaturu 1E 94 2A
.\nahraj.ps1 -Fuses     # 2) nový čip: fuses + firmware (jednou na čip)
.\nahraj.ps1            #    pozdější aktualizace: jen firmware
```

- Když Windows spuštění skriptu zablokuje: `powershell -ExecutionPolicy Bypass -File .\nahraj.ps1 -Test`
- Víc připojených USB zařízení se sériovým portem: přidejte `-Port COM5`.
- Vlastní překlad místo staženého firmwaru: `-Build` (potřebuje
  [avr-gcc](https://github.com/ZakKemble/avr-gcc-build/releases) rozbalený v `C:\avr`).

Fuses (zapisují se jen s `-Fuses`):

| Fuse | Hodnota | Význam |
|---|---|---|
| `fuse1` BODCFG | `0x04` | hlídání napětí 1,8 V v běhu, ve spánku vypnuté (firmware se sám vypne už na 3,0 V baterie) |
| `fuse2` OSCCFG | `0x02` | interní oscilátor 20 MHz (firmware běží na 3,33 MHz, párování 10 MHz) |
| `fuse5` SYSCFG0 | `0xC5` | **PA0 zůstává UPDI** · EEPROM se při přehrání nemaže (klíče párování zůstanou) |

> ⚠️ Hodnotu `fuse5` neměňte. Jiné nastavení pinu PA0 by UPDI vypnulo a čip by šel znovu naprogramovat
> jen vysokonapěťovým programátorem.

## 5. Po nahrání
1. Čidlo se po nahrání samo restartuje a **10 minut nabízí párování** (stejně jako po vložení baterie).
2. Hub: *Nastavení → Čidla → Přidat čidlo*; ID porovnejte se štítkem (první čidlo se spáruje samo).
3. Na štítek čidla napište jeho **ID** (ukáže ho hub při párování).

Aktualizace firmwaru později: stačí spustit `nahraj.ps1`. Klíče párování jsou v EEPROM a zůstanou,
čidlo pokračuje se stejným hubem.

## 6. Když to nejde

| Hláška / stav | Co zkusit |
|---|---|
| `Nenalezen žádný USB sériový port` | Arduino není připojené nebo chybí ovladač (CH340) |
| `Připojeno víc USB portů` | přidejte `-Port COMx` (číslo je v hlášce nebo ve Správci zařízení) |
| `UPDI initialisation failed`, `no response` | prohoďte D0/D1 (TX/RX), zkontrolujte odpor, drát RESET–GND, společnou GND a baterie v čidle |
| | nižší rychlost: `.\nahraj.ps1 -Baud 57600 -Test` |
| `could not open port` | port drží jiný program (Arduino IDE, sériový monitor) |
| `Device signature … expected 1E 94 2A` | jiný čip nebo špatný kontakt na UPDI |
| `… nemá správný kontrolní součet` | stažení se poškodilo – spusťte znovu |

Alternativa, kdyby SerialUPDI nespolupracoval: na Arduino Nano nahrajte projekt
[jtag2updi](https://github.com/ElTangas/jtag2updi), UPDI připojte přes 4,7 kΩ na pin **D6**
a mezi RESET a GND dejte kondenzátor 10 µF. Nahrání: `.\nahraj.ps1 -Programmer jtag2updi`.
