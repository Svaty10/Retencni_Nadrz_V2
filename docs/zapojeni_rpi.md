# Zapojení CC1101 k Raspberry Pi Zero 2 W

Zero 2 W má stejný 40pinový konektor jako Pi 3/4/5, zapojení je tedy shodné.
CC1101 napájejte **jen z 3,3 V** (pin 1 nebo 17), nikdy z 5 V.

| CC1101 | Signál | Pin RPi | BCM | Poznámka |
|:---:|:---|:---:|:---:|:---|
| 1 | VCC | 1 / 17 | 3V3 | 3,3 V |
| 2 | GND | 6 / 9 / 14 / 20 / 25 | GND | |
| 3 | CSN | 24 | GPIO 8 (CE0) | `/dev/spidev0.0` |
| 4 | SCLK | 23 | GPIO 11 | SPI0, 5 MHz |
| 5 | SI (MOSI) | 19 | GPIO 10 | |
| 6 | SO (MISO) | 21 | GPIO 9 | |
| 7 | GDO0 | 22 | GPIO 25 | konec paketu (hrana) – volitelné, bez něj server polluje |
| 8 | GDO2 | – | – | nezapojeno |

```text
                 3.3V [ 1] [ 2]  5V
                GPIO2 [ 3] [ 4]  5V
                GPIO3 [ 5] [ 6]  GND ----- CC1101 GND
                      ...
 (SI)  MOSI -- GPIO10 [19] [20]  GND
 (SO)  MISO --- GPIO9 [21] [22]  GPIO25 -- CC1101 GDO0
(SCLK) SCLK -- GPIO11 [23] [24]  GPIO8 --- CC1101 CSN
```

Tipy pro dosah:
- Anténa CC1101 modulu musí odpovídat 433 MHz (λ/4 ≈ 17,3 cm drát nebo pružinka pro 433 MHz).
- Pi s anténou dejte co nejblíž šachtě a výš nad zem. Kovový kryt Pi stíní – anténu vyveďte ven.
- Kvalitu spojení ukazuje dashboard (RSSI a ztráty paketů za 24 h). Pod −100 dBm je spojení na hraně.

SPI zapíná instalační skript sám (`dtparam=spi=on`); ověření: `ls /dev/spidev0.*`.
