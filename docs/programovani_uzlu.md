# Programování čidla (ATtiny1624 přes UPDI)

ATtiny1624 se programuje jedním vodičem **UPDI** (pin PA0). Programátor stačí obyčejný USB-UART převodník
nebo Arduino Uno / Nano a jeden odpor – metoda **SerialUPDI**, kterou umí `avrdude` přímo.

## 1. Co budete potřebovat

| | Doporučeno | Jde i takhle |
|---|---|---|
| Programátor | USB-UART převodník **3,3 V** (CP2102, CH340 s přepínačem 3V3, FT232) | Arduino **Uno** nebo **Nano** (s čipem 16U2 / CH340 / FT232) |
| Odpor | 4,7 kΩ (funguje 1–4,7 kΩ) | – |
| Napájení čidla | jeho vlastní baterie (3× Li-FeS2) | laboratorní zdroj 3,6–5 V na svorky baterie |
| Kabely | 3 dráty (dupont) | – |

Arduino s vlastním USB v hlavním čipu (Leonardo, Micro, Nano Every, Uno R4) tímto způsobem **nefunguje**.

## 2. Programy (Windows)

1. **Překladač avr-gcc:** [github.com/ZakKemble/avr-gcc-build/releases](https://github.com/ZakKemble/avr-gcc-build/releases)
   → `avr-gcc-…-x64-windows.zip` → rozbalit do `C:\avr\`.
2. **avrdude** (nahrávání): [github.com/avrdudes/avrdude/releases](https://github.com/avrdudes/avrdude/releases)
   → `avrdude-v…-windows-x64.zip` → rozbalit do `C:\avr\`.
3. Ovladač převodníku, pokud ho Windows nenajde sám: CH340 (WCH), CP2102 (Silicon Labs).
4. Port zjistíte ve *Správce zařízení → Porty (COM a LPT)* – např. `COM5`.

Skript `uzel\nahraj.ps1` si oba nástroje najde v `C:\avr` sám (nebo v PATH). `make` není potřeba.

## 3. Zapojení

**Nikdy nepropojujte napájení programátoru (5V / 3V3 / VCC) s čidlem.** CC1101 a SHT40 snesou max. 3,6 V.
Čidlo napájejte jeho bateriemi; s programátorem má společnou jen **zem**.

### a) USB-UART převodník (doporučeno)

```
 Převodník (3,3 V)                    Čidlo
 TX  ──────[ 4,7 kΩ ]──────┬──────── UPDI (PA0, pin 10 ATtiny1624)
 RX  ──────────────────────┘
 GND ──────────────────────────────── GND  (pin 14)
```

### b) Arduino Uno / Nano jako převodník

Hlavní čip Arduina se drží v resetu a USB převodník na desce pak slouží jako „USB-UART“.
Piny 0 a 1 jsou popsané z pohledu hlavního čipu, proto je zapojení „obráceně“:

```
 Arduino                              Čidlo
 RESET ──┐
 GND  ───┴─── (drát RESET–GND drží hlavní čip v resetu)
 D0 (RX←) ──[ 4,7 kΩ ]─────┬──────── UPDI (PA0, pin 10)
 D1 (TX→) ─────────────────┘
 GND ──────────────────────────────── GND  (pin 14)
```

Arduino má logiku 5 V a čidlo 3,3 V. Odpor 4,7 kΩ (spolu s 1 kΩ na desce Arduina) omezí proud do vstupu
na zlomek miliampéru, takže je to pro občasné nahrání v pořádku. Pro sériové programování víc kusů
použijte převodník 3,3 V.

### Kde je UPDI na čidle
Na desce hledejte programovací plošky / konektor **UPDI – GND (– VCC)**. Pokud tam nejsou, UPDI je
**pin 10** ATtiny1624 (SOIC-14), GND **pin 14**, VDD **pin 1**.

## 4. Nahrání

V PowerShellu ve složce `uzel` (při prvním spuštění skriptů může být potřeba
`powershell -ExecutionPolicy Bypass -File .\nahraj.ps1 …`):

```powershell
cd C:\Users\<vy>\…\Retencni_Nadrz_V2\uzel
.\nahraj.ps1 -Port COM5 -Test      # 1) spojení: musí vypsat signaturu 1E 94 2A
.\nahraj.ps1 -Port COM5 -Fuses     # 2) nový čip: fuses + firmware (jednou)
.\nahraj.ps1 -Port COM5            #    pozdější aktualizace: jen firmware
```

Fuses (zapisují se jen s `-Fuses`, stačí jednou na čip):

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

Aktualizace firmwaru později: stačí `.\nahraj.ps1 -Port COM5`. Klíče párování jsou v EEPROM a zůstanou,
čidlo pokračuje se stejným hubem.

## 6. Když to nejde

| Hláška / stav | Co zkusit |
|---|---|
| `UPDI initialisation failed`, `no response` | prohoďte TX/RX (u Arduina D0/D1), zkontrolujte odpor a společnou GND, čidlo musí mít baterie |
| | nižší rychlost: `.\nahraj.ps1 -Port COM5 -Baud 57600 -Test` |
| `could not open port` | špatné číslo COM, port drží jiný program (Arduino IDE, sériový monitor) |
| Arduino: nic | drát RESET–GND, a u Nano s CH340 ovladač CH340 |
| `Device signature … expected 1E 94 2A` | jiný čip nebo špatný kontakt na UPDI |
| `Nenalezen avr-gcc / avrdude` | rozbalte zipy do `C:\avr` (stačí libovolné podsložky) |

Alternativa, kdyby SerialUPDI nespolupracoval: na Arduino Nano nahrajte projekt
[jtag2updi](https://github.com/ElTangas/jtag2updi), UPDI připojte přes 4,7 kΩ na pin **D6**
a mezi RESET a GND dejte kondenzátor 10 µF. Nahrání: `.\nahraj.ps1 -Port COM5 -Programmer jtag2updi`.
