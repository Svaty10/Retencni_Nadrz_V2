# Retenční nádrž V3 – bezdrátové měření hladiny

Bezúdržbový systém pro retenční nádrž: bateriové čidlo v šachtě měří každých 5 minut hladinu,
teplotu a vlhkost a posílá je rádiem (433 MHz) na Raspberry Pi Zero 2 W. Pi data ukládá,
zobrazuje na webu (i přes Cloudflare), poskytuje je Home Assistantu a umí řídit dopouštění
přes Hunter Hydrawise.

```
 ┌──────────── šachta ───────────┐          ┌──────── dům ─────────────────────────────┐
 │ ATtiny1624 + SHT40 + sonda    │  433 MHz │ Pi Zero 2 W + CC1101                      │
 │ 3× Li-FeS2, ~12 µA průměr     │ ───────► │  :80   web (login, grafy)  ◄── Cloudflare │
 │ 21 B šifr. / 5 min (1 min při │          │  :8081 Home Assistant (LAN, token)        │
 │ rychlé změně hladiny)         │          │  Wi-Fi hotspot + portál, mDNS, OTA         │
 └───────────────────────────────┘          └──────────────────────────────────────────┘
```

| Složka | Obsah |
|---|---|
| [`uzel/`](uzel/) | firmware čidla (C, avr-gcc ≥ 12) |
| [`server/`](server/) | aplikace na Pi (Python 3.11+, **jen standardní knihovna** + spidev) |
| [`system/`](system/) | instalace, systemd služby, Wi-Fi hotspot/portál, mDNS, aktualizace |
| [`docs/`](docs/) | [zadání](docs/ZADANI.md), [instalace a provoz](docs/instalace.md), [zapojení](docs/zapojeni_rpi.md), [API](docs/api.md), [Home Assistant](docs/home_assistant.yaml) |
| `old/` | původní verze (vzor vzhledu) |

## Rychlý start
```bash
git clone https://github.com/Svaty10/Retencni_Nadrz_V2.git && cd Retencni_Nadrz_V2
sudo ./system/install.sh && sudo reboot
```
Pak `http://retencka.local` → první přihlášení `admin` + heslo ze štítku (`/boot/firmware/retencka.txt`)
→ vytvoření vlastního účtu. Funguje na Pi Zero 2 W i Pi 3 B/3 B+/4.
Podrobně v [docs/instalace.md](docs/instalace.md).

## Firmware čidla
```bash
cd uzel
make                 # build/firmware.hex  (~8,8 kB flash)
make test            # self-test kryptografie proti oficiálním vektorům (na PC)
make fuses PORT=COM3 # jednou na čip
make flash PORT=COM3 # SerialUPDI
```
Data jsou šifrovaná (AES-128) a podepsaná (CMAC), klíč vzniká při párování (X25519).
Fuse `EESAVE` zachová klíč i při přehrání firmwaru. Ladicí konstanty jsou v
[`uzel/include/config.h`](uzel/include/config.h). Kalibrace hladiny se dělá na serveru.

### Energetický rozpočet (odhad)
| Fáze | Proud | Doba | Náboj / cyklus |
|---|---|---|---|
| Spánek (PIT + WDT, CC1101 SLEEP) | ~1,5 µA | trvale | – |
| Sonda (boost 5 V + sonda) | ~5–6 mA | ~0,25 s | ~1,4 mAs |
| Šifrované vysílání +10 dBm (53 ms + kalibrace) | ~30 mA | ~58 ms | ~1,7 mAs |
| SHT40, CPU, baterie (1×/h) | – | – | ~0,1 mAs |

Při 5min intervalu ≈ 3,3 mAs/cyklus → ~12 µA průměrně → výdrž 3× AA Li-FeS2 (3000 mAh) omezuje hlavně
samovybíjení (10+ let). Skutečnou spotřebu sondy ověřte měřením.

## Paměť a výkon serveru
Proces ~28–35 MB RSS (limit v systemd 64 MB), CPU v klidu ≈ 0 %. Grafy kreslí prohlížeč,
server posílá max. ~600 předem zprůměrovaných bodů. Databáze ~4 MB za rok.
