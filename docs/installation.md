# Installation Guide – Raspberry Pi 3 B+ Webserver

This guide covers setup and deployment on **Raspberry Pi OS Lite** (Debian 12 Bookworm / 11 Bullseye).

---

## 1. Prerequisites & System Packages

Update your system and install Python 3 development tools and SPI libraries:
```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
```

Enable the SPI hardware interface:
```bash
sudo raspi-config
# Interface Options -> SPI -> Yes
```
Reboot if requested, then verify `/dev/spidev0.0` is present.

---

## 2. Deploying the Webserver

Clone or copy the repository to `/home/pi/Retencni_Nadrz_V2`:
```bash
cd /home/pi/Retencni_Nadrz_V2/webserver
```

Create an isolated Python virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install spidev gpiod
```

Test running the server directly:
```bash
python3 app.py
```
Open `http://<raspberry-pi-ip>:8080/` in your browser.

---

## 3. Installing Systemd Service

Deploy the systemd unit file with automatic memory limit governance (`MemoryMax=50M`):
```bash
sudo cp retencka.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable retencka.service
sudo systemctl start retencka.service
```

Check service status and memory usage:
```bash
sudo systemctl status retencka.service
```

Inspect logs:
```bash
journalctl -u retencka -f
```
