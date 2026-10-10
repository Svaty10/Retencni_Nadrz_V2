"""
CC1101 RF Receiver driver for Raspberry Pi 3 B+ via SPI0 and GPIO.
Decodes 8-byte Little-Endian telemetry packets and handles automatic bus resets.
"""

import time
import struct
import logging
import threading
from typing import Optional, Callable

logger = logging.getLogger("cc1101_receiver")

# CC1101 Command Strobes
CC1101_SRES      = 0x30
CC1101_SFSTXON   = 0x31
CC1101_SXOFF     = 0x32
CC1101_SCAL      = 0x33
CC1101_SRX       = 0x34
CC1101_STX       = 0x35
CC1101_SIDLE     = 0x36
CC1101_SFRX      = 0x3A
CC1101_SFTX      = 0x3B

# CC1101 Configuration Registers
CC1101_IOCFG0    = 0x02
CC1101_FIFOTHR   = 0x03
CC1101_PKTLEN    = 0x06
CC1101_PKTCTRL1  = 0x07
CC1101_PKTCTRL0  = 0x08
CC1101_ADDR      = 0x09
CC1101_CHANNR    = 0x0A
CC1101_FSCTRL1   = 0x0B
CC1101_FSCTRL0   = 0x0C
CC1101_FREQ2     = 0x0D
CC1101_FREQ1     = 0x0E
CC1101_FREQ0     = 0x0F
CC1101_MDMCFG4   = 0x10
CC1101_MDMCFG3   = 0x11
CC1101_MDMCFG2   = 0x12
CC1101_MDMCFG1   = 0x13
CC1101_MDMCFG0   = 0x14
CC1101_DEVIATN   = 0x15
CC1101_MCSM0     = 0x18
CC1101_FOCCFG    = 0x19
CC1101_AGCCTRL2  = 0x1B
CC1101_AGCCTRL1  = 0x1C
CC1101_AGCCTRL0  = 0x1D
CC1101_FSCAL3    = 0x23
CC1101_FSCAL2    = 0x24
CC1101_FSCAL1    = 0x25
CC1101_FSCAL0    = 0x26
CC1101_TEST2     = 0x2C
CC1101_TEST1     = 0x2D
CC1101_TEST0     = 0x2E
CC1101_PATABLE   = 0x3E
CC1101_FIFO      = 0x3F

# Status Registers
CC1101_PARTNUM   = 0x30 | 0xC0
CC1101_VERSION   = 0x31 | 0xC0
CC1101_MARCSTATE = 0x35 | 0xC0
CC1101_RXBYTES   = 0x3B | 0xC0

PACKET_SIZE      = 8  # Fixed 8-byte payload

class CC1101Receiver:
    def __init__(self, spi_bus: int = 0, spi_device: int = 0,
                 spi_speed_hz: int = 5000000, gdo0_pin: int = 25,
                 on_packet_callback: Optional[Callable[[dict], None]] = None):
        self.spi_bus = spi_bus
        self.spi_device = spi_device
        self.spi_speed_hz = spi_speed_hz
        self.gdo0_pin = gdo0_pin
        self.on_packet_callback = on_packet_callback

        self.spi = None
        self.running = False
        self.worker_thread = None
        self.lock = threading.Lock()
        self.is_hardware_available = False

    def _write_reg(self, reg: int, val: int):
        self.spi.xfer2([reg & 0x3F, val])

    def _read_reg(self, reg: int) -> int:
        res = self.spi.xfer2([(reg & 0x3F) | 0x80, 0x00])
        return res[1]

    def _strobe(self, strobe: int) -> int:
        res = self.spi.xfer2([strobe & 0x3F])
        return res[0]

    def _read_burst(self, reg: int, length: int) -> list:
        tx = [(reg & 0x3F) | 0xC0] + [0x00] * length
        rx = self.spi.xfer2(tx)
        return rx[1:]

    def init_hardware(self) -> bool:
        """Initialize SPI and CC1101 RF transceiver registers."""
        try:
            import spidev
            self.spi = spidev.SpiDev()
            self.spi.open(self.spi_bus, self.spi_device)
            self.spi.max_speed_hz = self.spi_speed_hz
            self.spi.mode = 0
            self.is_hardware_available = True
        except Exception as e:
            logger.warning(f"SPI hardware not available (running in simulation/fallback mode): {e}")
            self.is_hardware_available = False
            return False

        return self.reset_and_configure()

    def reset_and_configure(self) -> bool:
        """Perform soft reset and configure CC1101 registers for 433.92 MHz 4.8 kBaud."""
        if not self.is_hardware_available:
            return False

        with self.lock:
            try:
                # Soft reset strobe
                self._strobe(CC1101_SRES)
                time.sleep(0.01)

                # Check version
                version = self._read_reg(CC1101_VERSION)
                if version in (0x00, 0xFF):
                    logger.error(f"CC1101 not detected (VERSION returned 0x{version:02X})")
                    return False

                logger.info(f"CC1101 detected: Chip VERSION 0x{version:02X}")

                # Register configuration (matched to ATtiny1624 node)
                # GDO0 asserts on sync, deasserts on packet completion
                self._write_reg(CC1101_IOCFG0,   0x06)
                self._write_reg(CC1101_FIFOTHR,  0x47)
                self._write_reg(CC1101_PKTLEN,   PACKET_SIZE)
                self._write_reg(CC1101_PKTCTRL1, 0x04) # Append status bytes (RSSI, LQI/CRC)
                self._write_reg(CC1101_PKTCTRL0, 0x44) # Fixed packet length, Whitening, CRC enable
                self._write_reg(CC1101_ADDR,     0x00)
                self._write_reg(CC1101_CHANNR,   0x00)
                self._write_reg(CC1101_FSCTRL1,  0x06)
                self._write_reg(CC1101_FSCTRL0,  0x00)

                # Carrier frequency 433.92 MHz (26 MHz crystal)
                self._write_reg(CC1101_FREQ2,    0x10)
                self._write_reg(CC1101_FREQ1,    0xB1)
                self._write_reg(CC1101_FREQ0,    0x3B)

                # 4.8 kBaud, 2-FSK, ChBW 101 kHz
                self._write_reg(CC1101_MDMCFG4,  0xC7)
                self._write_reg(CC1101_MDMCFG3,  0x83)
                self._write_reg(CC1101_MDMCFG2,  0x03)
                self._write_reg(CC1101_MDMCFG1,  0x22)
                self._write_reg(CC1101_MDMCFG0,  0xF8)
                self._write_reg(CC1101_DEVIATN,  0x40)

                self._write_reg(CC1101_MCSM0,    0x18)
                self._write_reg(CC1101_FOCCFG,   0x16)
                self._write_reg(CC1101_AGCCTRL2, 0x43)
                self._write_reg(CC1101_AGCCTRL1, 0x40)
                self._write_reg(CC1101_AGCCTRL0, 0x91)

                self._write_reg(CC1101_FSCAL3,   0xE9)
                self._write_reg(CC1101_FSCAL2,   0x2A)
                self._write_reg(CC1101_FSCAL1,   0x00)
                self._write_reg(CC1101_FSCAL0,   0x1F)

                self._write_reg(CC1101_TEST2,    0x81)
                self._write_reg(CC1101_TEST1,    0x35)
                self._write_reg(CC1101_TEST0,    0x09)

                # PA Table
                self.spi.xfer2([(CC1101_PATABLE & 0x3F) | 0x40, 0xC0])

                # Flush RX FIFO and enter RX mode
                self._strobe(CC1101_SIDLE)
                self._strobe(CC1101_SFRX)
                self._strobe(CC1101_SRX)

                logger.info("CC1101 successfully configured and listening in RX mode")
                return True
            except Exception as e:
                logger.error(f"Error configuring CC1101: {e}")
                return False

    def check_and_recover_rx(self):
        """Check receiver state machine and recover from FIFO overflow or freeze."""
        if not self.is_hardware_available:
            return

        with self.lock:
            try:
                state = self._read_reg(CC1101_MARCSTATE) & 0x1F
                # 0x11 = RXFIFO_OVERFLOW, 0x16 = TXFIFO_UNDERFLOW
                if state == 0x11:
                    logger.warning("CC1101 RX FIFO overflow detected, flushing and restarting RX")
                    self._strobe(CC1101_SIDLE)
                    self._strobe(CC1101_SFRX)
                    self._strobe(CC1101_SRX)
                elif state == 0x01: # IDLE unexpectedly
                    self._strobe(CC1101_SRX)
            except Exception as e:
                logger.error(f"Error during CC1101 state check: {e}")
                self.reset_and_configure()

    def read_packet(self) -> Optional[dict]:
        """Read 8-byte payload plus 2 status bytes (RSSI, LQI) from RX FIFO."""
        if not self.is_hardware_available:
            return None

        with self.lock:
            try:
                rx_bytes = self._read_reg(CC1101_RXBYTES) & 0x7F
                # We expect 8 bytes payload + 2 append status bytes = 10 bytes
                if rx_bytes < (PACKET_SIZE + 2):
                    return None

                raw_data = self._read_burst(CC1101_FIFO, PACKET_SIZE + 2)
                self._strobe(CC1101_SFRX)
                self._strobe(CC1101_SRX)

                payload = bytes(raw_data[:PACKET_SIZE])
                rssi_raw = raw_data[PACKET_SIZE]
                lqi_raw = raw_data[PACKET_SIZE + 1]

                # Bit 7 of LQI byte is CRC_OK flag
                crc_ok = bool(lqi_raw & 0x80)
                if not crc_ok:
                    logger.warning("Packet received but CRC check failed, discarding")
                    return None

                # Calculate RSSI in dBm
                if rssi_raw >= 128:
                    rssi_dbm = ((rssi_raw - 256) / 2.0) - 74.0
                else:
                    rssi_dbm = (rssi_raw / 2.0) - 74.0

                # Decode 8-byte Little-Endian structure: uint16, int16, uint16, uint16
                vbat_raw_mv, temp_x100, hum_x100, level_mm = struct.unpack('<HhhH', payload)

                return {
                    "vbat_raw_mv": vbat_raw_mv,
                    "temp_x100": temp_x100,
                    "hum_x100": hum_x100,
                    "level_mm": level_mm,
                    "rssi_dbm": round(rssi_dbm, 1),
                    "lqi": lqi_raw & 0x7F
                }
            except Exception as e:
                logger.error(f"Error reading packet from FIFO: {e}")
                self._strobe(CC1101_SIDLE)
                self._strobe(CC1101_SFRX)
                self._strobe(CC1101_SRX)
                return None

    def _worker(self):
        """Background reception polling worker with bus recovery watchdog."""
        logger.info("CC1101 reception worker thread started")
        last_watchdog_time = time.time()

        while self.running:
            try:
                packet = self.read_packet()
                if packet:
                    logger.info(f"Valid RF packet received: {packet}")
                    if self.on_packet_callback:
                        self.on_packet_callback(packet)

                # Periodic health check every 10 seconds
                now = time.time()
                if now - last_watchdog_time > 10.0:
                    self.check_and_recover_rx()
                    last_watchdog_time = now

                time.sleep(0.05)
            except Exception as e:
                logger.error(f"Unexpected worker exception: {e}")
                time.sleep(1.0)

    def start(self):
        """Start listening for incoming packets."""
        if not self.running:
            self.running = True
            self.init_hardware()
            self.worker_thread = threading.Thread(target=self._worker, daemon=True, name="cc1101_rx")
            self.worker_thread.start()

    def stop(self):
        """Stop receiver and close SPI port."""
        self.running = False
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)
        if self.spi:
            try:
                self._strobe(CC1101_SIDLE)
                self.spi.close()
            except Exception:
                pass
