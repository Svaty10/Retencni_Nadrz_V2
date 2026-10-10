/**
 * @file cc1101.c
 * @brief Deterministic, low-power CC1101 transceiver implementation.
 * @author Matej
 * @date 2026-10-10
 */

#include "cc1101.h"
#include <util/delay.h>

#define SPI_TIMEOUT_CYCLES          15000UL
#define CC1101_OSC_WAIT_TIMEOUT     40000UL
#define CC1101_TX_TIMEOUT_MS        100U

/* Forward declarations */
static uint8_t spi_transfer(uint8_t data);
static uint8_t cc1101_strobe(uint8_t strobe);
static void cc1101_write_reg(uint8_t reg, uint8_t value);
static uint8_t cc1101_read_reg(uint8_t reg);
static void cc1101_write_burst(uint8_t reg, const uint8_t *buffer, uint8_t len);

/* Helper macro for Chip Select */
static inline void cc1101_cs_low(void)  { PORTA.OUTCLR = PIN_SPI_CSN; }
static inline void cc1101_cs_high(void) { PORTA.OUTSET = PIN_SPI_CSN; }

uint16_t cc1101_calc_crc16(const uint8_t *data, uint16_t len)
{
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < len; i++) {
        crc ^= ((uint16_t)data[i] << 8);
        for (uint8_t bit = 0; bit < 8; bit++) {
            if (crc & 0x8000) {
                crc = (crc << 1) ^ 0x1021; /* CCITT polynomial */
            } else {
                crc = (crc << 1);
            }
        }
    }
    return crc;
}

static void spi_init(void)
{
    /* PA1: MOSI (OUT), PA2: MISO (IN), PA3: SCK (OUT), PA4: CSN (OUT) */
    PORTA.DIRSET = PIN_SPI_MOSI | PIN_SPI_SCK | PIN_SPI_CSN;
    PORTA.DIRCLR = PIN_SPI_MISO;

    PORTA.OUTSET = PIN_SPI_CSN; /* Inactive HIGH */
    PORTA.OUTCLR = PIN_SPI_MOSI | PIN_SPI_SCK;

    /* SPI Master, Prescaler DIV4 (3.333 MHz / 4 = ~833 kHz), Enable */
    SPI0.CTRLA = SPI_MASTER_bm | SPI_ENABLE_bm | SPI_PRESC_DIV4_gc;
    SPI0.CTRLB = SPI_SSD_bm; /* Disable multi-master / SS auto handling */
}

static void spi_deinit(void)
{
    /* Disable SPI peripheral and return lines to low-power idle */
    SPI0.CTRLA = 0;
    PORTA.OUTCLR = PIN_SPI_MOSI | PIN_SPI_SCK;
    PORTA.OUTSET = PIN_SPI_CSN;
}

static uint8_t spi_transfer(uint8_t data)
{
    SPI0.DATA = data;
    uint32_t timeout = SPI_TIMEOUT_CYCLES;
    while (!(SPI0.INTFLAGS & SPI_IF_bm)) {
        if (--timeout == 0) {
            return 0x00;
        }
    }
    return SPI0.DATA;
}

static uint8_t cc1101_strobe(uint8_t strobe)
{
    cc1101_cs_low();
    uint8_t status = spi_transfer(strobe);
    cc1101_cs_high();
    return status;
}

static void cc1101_write_reg(uint8_t reg, uint8_t value)
{
    cc1101_cs_low();
    spi_transfer(reg & 0x3F);
    spi_transfer(value);
    cc1101_cs_high();
}

static uint8_t cc1101_read_reg(uint8_t reg)
{
    cc1101_cs_low();
    spi_transfer((reg & 0x3F) | 0x80);
    uint8_t val = spi_transfer(0x00);
    cc1101_cs_high();
    return val;
}

static void cc1101_write_burst(uint8_t reg, const uint8_t *buffer, uint8_t len)
{
    cc1101_cs_low();
    spi_transfer((reg & 0x3F) | 0x40);
    for (uint8_t i = 0; i < len; i++) {
        spi_transfer(buffer[i]);
    }
    cc1101_cs_high();
}

bool cc1101_wakeup(void)
{
    /* 1. Pull CSn LOW to request CC1101 wake-up */
    cc1101_cs_low();

    /* 2. Wait until MISO pin goes LOW (Crystal oscillator stabilized) */
    uint32_t timeout = CC1101_OSC_WAIT_TIMEOUT;
    while (PORTA.IN & PIN_SPI_MISO) {
        if (--timeout == 0) {
            cc1101_cs_high();
            return false; /* Oscillator startup failure */
        }
    }

    /* 3. Pull CSn HIGH to complete wakeup handshake */
    cc1101_cs_high();
    _delay_us(15);

    /* 4. Force Idle state */
    cc1101_strobe(CC1101_SIDLE);
    return true;
}

void cc1101_power_down(void)
{
    /* Place CC1101 in Idle, then strobe SPWD */
    cc1101_strobe(CC1101_SIDLE);
    _delay_us(10);
    cc1101_strobe(CC1101_SPWD);

    /* Deinitialize SPI to eliminate leakage current */
    spi_deinit();
}

bool cc1101_init(void)
{
    spi_init();

    /* Perform manual reset strobe sequence */
    cc1101_cs_high();
    _delay_us(5);
    cc1101_cs_low();
    _delay_us(10);
    cc1101_cs_high();
    _delay_us(45);

    if (!cc1101_wakeup()) {
        spi_deinit();
        return false;
    }

    cc1101_strobe(CC1101_SRES);
    _delay_ms(1);

    if (!cc1101_wakeup()) {
        spi_deinit();
        return false;
    }

    /* Verify communication by reading chip version */
    uint8_t version = cc1101_read_reg(CC1101_VERSION);
    if (version == 0x00 || version == 0xFF) {
        spi_deinit();
        return false; /* Device missing or SPI fault */
    }

    /* ====================================================================== */
    /* RF Configuration: 433.92 MHz, 4.8 kBaud, 2-FSK, Fixed Packet, Whitening */
    /* ====================================================================== */
    cc1101_write_reg(CC1101_IOCFG0,   0x06); /* GDO0: Asserts when sync sent, de-asserts at end of packet */
    cc1101_write_reg(CC1101_FIFOTHR,  0x47); /* 33 bytes TX FIFO threshold */
    cc1101_write_reg(CC1101_PKTLEN,   sizeof(telemetry_packet_t)); /* Fixed length */
    cc1101_write_reg(CC1101_PKTCTRL1, 0x04); /* Append status, no address check */
    cc1101_write_reg(CC1101_PKTCTRL0, 0x44); /* Whitening ON, CRC ON, Fixed length mode */
    cc1101_write_reg(CC1101_ADDR,     0x00);
    cc1101_write_reg(CC1101_CHANNR,   0x00);
    cc1101_write_reg(CC1101_FSCTRL1,  0x06); /* IF freq = 152.3 kHz */
    cc1101_write_reg(CC1101_FSCTRL0,  0x00);

    /* Carrier frequency: 433.92 MHz (26 MHz crystal) -> 0x10B13B */
    cc1101_write_reg(CC1101_FREQ2,    0x10);
    cc1101_write_reg(CC1101_FREQ1,    0xB1);
    cc1101_write_reg(CC1101_FREQ0,    0x3B);

    /* Modem config: Data rate 4.8 kBaud, ChBW 101 kHz */
    cc1101_write_reg(CC1101_MDMCFG4,  0xC7);
    cc1101_write_reg(CC1101_MDMCFG3,  0x83);
    cc1101_write_reg(CC1101_MDMCFG2,  0x03); /* 2-FSK, 30/32 sync word bits */
    cc1101_write_reg(CC1101_MDMCFG1,  0x22); /* 4 bytes preamble */
    cc1101_write_reg(CC1101_MDMCFG0,  0xF8);

    /* Deviation: ~25.4 kHz */
    cc1101_write_reg(CC1101_DEVIATN,  0x40);

    /* State Machine control: Auto-calibrate when going from IDLE to TX */
    cc1101_write_reg(CC1101_MCSM0,    0x18);
    cc1101_write_reg(CC1101_FOCCFG,   0x16);
    cc1101_write_reg(CC1101_AGCCTRL2, 0x43);
    cc1101_write_reg(CC1101_AGCCTRL1, 0x40);
    cc1101_write_reg(CC1101_AGCCTRL0, 0x91);

    /* Frequency synthesizer calibration */
    cc1101_write_reg(CC1101_FSCAL3,   0xE9);
    cc1101_write_reg(CC1101_FSCAL2,   0x2A);
    cc1101_write_reg(CC1101_FSCAL1,   0x00);
    cc1101_write_reg(CC1101_FSCAL0,   0x1F);

    cc1101_write_reg(CC1101_TEST2,    0x81);
    cc1101_write_reg(CC1101_TEST1,    0x35);
    cc1101_write_reg(CC1101_TEST0,    0x09);

    /* Output Power: +10 dBm (0xC0 for 433 MHz band) */
    uint8_t pa_val = 0xC0;
    cc1101_write_burst(CC1101_PATABLE, &pa_val, 1);

    /* Put radio into deep power-down sleep immediately after initialization */
    cc1101_power_down();

    return true;
}

bool cc1101_transmit_packet(const void *packet, uint8_t len)
{
    if (packet == NULL || len == 0) {
        return false;
    }

    /* 1. Wake up SPI and CC1101 */
    spi_init();
    if (!cc1101_wakeup()) {
        cc1101_power_down();
        return false;
    }

    /* 2. Flush TX FIFO */
    cc1101_strobe(CC1101_SIDLE);
    cc1101_strobe(CC1101_SFTX);

    /* 3. Load packet payload into TX FIFO */
    cc1101_write_burst(CC1101_FIFO, (const uint8_t *)packet, len);

    /* 4. Trigger TX strobe */
    cc1101_strobe(CC1101_STX);

    /* 5. Wait for transmission start (GDO0 goes HIGH when sync word is sent) */
    uint16_t timeout_ms = CC1101_TX_TIMEOUT_MS;
    while (!(PORTB.IN & PIN_RF_GDO0)) {
        _delay_ms(1);
        if (--timeout_ms == 0) {
            /* Transmission failed to start */
            cc1101_power_down();
            return false;
        }
    }

    /* 6. Wait for transmission finish (GDO0 goes LOW when packet + CRC completed) */
    while (PORTB.IN & PIN_RF_GDO0) {
        _delay_ms(1);
        if (--timeout_ms == 0) {
            /* Transmission hung */
            cc1101_power_down();
            return false;
        }
    }

    /* 7. Enter deep power-down sleep immediately */
    cc1101_power_down();

    return true;
}
