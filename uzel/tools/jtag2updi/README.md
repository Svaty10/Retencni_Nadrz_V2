# jtag2updi pro Arduino Uno / Nano (ATmega328P, 16 MHz)

Z Arduina udělá programátor UPDI na **pinu 6** – pro klony s CH340, které neumí SerialUPDI
přes piny 0/1 (kontrolky TX/RX drží linku). Nahrání do Arduina: `uzel\0-priprav-arduino.cmd`.

- Zdroj: https://github.com/ElTangas/jtag2updi, commit `07be876` (licence MIT – viz `LICENSE`)
- Sestaveno avr-gcc 16.1.0 pro `atmega328p`:
  `-DNDEBUG -DUPDI_BAUD=225000U -DF_CPU=16000000 -DUPDI_IO_TYPE=2` – **bez** `-DDISABLE_HOST_TIMEOUT`,
  `-pipe -Os -fno-jump-tables -fno-gcse -ffunction-sections -fdata-sections -fpack-struct -fshort-enums -mrelax`
  (bez LTO; `-pipe` kvůli cestám s `\` ve Windows)
- Ověřeno na klonu Uno s CH340: avrdude `-c jtag2updi` se přihlásí (FW 6.00).
- **Bez** `DISABLE_HOST_TIMEOUT`: po ~250 ms ticha se jtag2updi sám vrátí do výchozího stavu (19200 Bd),
  takže se nezasekne ani bez restartu Arduina. Kondenzátor RESET–GND není potřeba (a při nahrávání
  jtag2updi do Arduina nesmí být zapojený).
