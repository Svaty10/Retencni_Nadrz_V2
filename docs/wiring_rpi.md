# Raspberry Pi 3 B+ & CC1101 Wiring Specification

This document defines the hardware pin interconnection between the **Raspberry Pi 3 B+** (40-pin GPIO header) and the **CC1101 433 MHz SPI RF Transceiver module**.

---

## 1. Physical Pinout Connection Table

| CC1101 Pin | Signal Name | Raspberry Pi Physical Pin | BCM GPIO Number | Function / Notes |
| :---: | :--- | :---: | :---: | :--- |
| **1** | `VCC` | **Pin 1** or **Pin 17** | `3.3V Power` | Regulated 3.3V power supply (Do NOT use 5V!) |
| **2** | `GND` | **Pin 6**, **9**, **14**, **20** or **25** | `GND` | Ground reference |
| **3** | `CSN` / `SS` | **Pin 24** | `GPIO 8 (CE0)` | SPI Chip Select 0 (Active LOW) |
| **4** | `SCLK` | **Pin 23** | `GPIO 11 (SCLK)` | SPI Clock (Up to 5 MHz) |
| **5** | `MOSI` / `SI` | **Pin 19** | `GPIO 10 (MOSI)` | Master Out Slave In |
| **6** | `MISO` / `SO` | **Pin 21** | `GPIO 9 (MISO)` | Master In Slave Out |
| **7** | `GDO0` | **Pin 22** | `GPIO 25` | Packet Sync & Completion Interrupt |
| **8** | `GDO2` | **Pin 18** *(Optional)* | `GPIO 24` | Optional carrier sense / FIFO threshold |

---

## 2. Raspberry Pi 40-Pin Header Layout (ASCII)

```text
Raspberry Pi 3 B+ (GPIO Header)
                 3.3V [ 1] [ 2]  5V
                GPIO2 [ 3] [ 4]  5V
                GPIO3 [ 5] [ 6]  GND ------------ CC1101 GND (Pin 2)
                GPIO4 [ 7] [ 8]  GPIO14
                  GND [ 9] [10]  GPIO15
               GPIO17 [11] [12]  GPIO18
               GPIO27 [13] [14]  GND
               GPIO22 [15] [16]  GPIO23
                 3.3V [17] [18]  GPIO24 --------- CC1101 GDO2 (Volitelně)
 (SI)  MOSI -- GPIO10 [19] [20]  GND
 (SO)  MISO --- GPIO9 [21] [22]  GPIO25 --------- CC1101 GDO0 (Interrupt)
(SCLK) SCLK -- GPIO11 [23] [24]  GPIO8 ---------- CC1101 CSN  (SPI CE0)
                  GND [25] [26]  GPIO7
                      ...
```

---

## 3. Raspberry Pi Linux System Configuration

### 3.1. Enable SPI Hardware
Run on Raspberry Pi OS:
```bash
sudo raspi-config
# Navigate to: Interface Options -> SPI -> Enable -> Yes
```
Or append to `/boot/firmware/config.txt` (or `/boot/config.txt`):
```ini
dtparam=spi=on
```

Verify SPI device is present:
```bash
ls -l /dev/spidev0.*
# Expected output: /dev/spidev0.0 and /dev/spidev0.1
```
