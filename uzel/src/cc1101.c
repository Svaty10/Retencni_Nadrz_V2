/**
 * @file cc1101.c
 * @brief Deterministic, low-power, transmit-only CC1101 driver.
 *
 * RF settings (identical on the receiver):
 *   433.92 MHz (26 MHz XTAL, FREQ = 0x10B071), 2-FSK, 4.8 kBaud, deviation 25.4 kHz,
 *   RX filter 101.6 kHz, 4 byte preamble, 30/32 sync (0xD391), whitening, CRC-16,
 *   variable packet length (first byte = length, max 61).
 * Air time of a DATA packet: (4 + 4 + 1 + 21 + 2) * 8 / 4800 = 53 ms.
 */

#include "cc1101.h"
#include <util/delay.h>
#include <stddef.h>

#define SPI_TIMEOUT_LOOPS       2000U
#define OSC_TIMEOUT_LOOPS       20000U   /* XOSC start-up is ~150 us typ. */
#define TX_TIMEOUT_MS           150U

/* Register table written at init. TEST0..2 are not retained in SLEEP and are
 * re-written before every transmission (see cc1101_restore_test_regs). */
static const uint8_t k_cfg[][2] = {
    { CC1101_IOCFG2,   0x2E },   /* GDO2: high impedance */
    { CC1101_IOCFG0,   0x06 },   /* GDO0: asserts on sync, de-asserts at end of packet */
    { CC1101_FIFOTHR,  0x47 },
    { CC1101_SYNC1,    0xD3 },
    { CC1101_SYNC0,    0x91 },
    { CC1101_PKTLEN,   61 },     /* max length accepted in RX */
    { CC1101_PKTCTRL1, 0x04 },   /* append RSSI/LQI status, no address check */
    { CC1101_PKTCTRL0, 0x45 },   /* whitening, CRC, variable length */
    { CC1101_ADDR,     0x00 },
    { CC1101_CHANNR,   0x00 },
    { CC1101_FSCTRL1,  0x06 },   /* IF 152 kHz */
    { CC1101_FSCTRL0,  0x00 },
    { CC1101_FREQ2,    0x10 },   /* 433.92 MHz */
    { CC1101_FREQ1,    0xB0 },
    { CC1101_FREQ0,    0x71 },
    { CC1101_MDMCFG4,  0xC7 },   /* BW 101.6 kHz, DRATE_E 7 */
    { CC1101_MDMCFG3,  0x83 },   /* 4797 Bd */
    { CC1101_MDMCFG2,  0x03 },   /* 2-FSK, DC filter on, 30/32 sync */
    { CC1101_MDMCFG1,  0x22 },   /* 4 preamble bytes */
    { CC1101_MDMCFG0,  0xF8 },
    { CC1101_DEVIATN,  0x40 },   /* 25.4 kHz */
    { CC1101_MCSM1,    0x30 },   /* after TX and after RX -> IDLE */
    { CC1101_MCSM0,    0x18 },   /* auto-calibrate IDLE -> RX/TX */
    { CC1101_FOCCFG,   0x16 },
    { CC1101_BSCFG,    0x6C },
    { CC1101_AGCCTRL2, 0x43 },
    { CC1101_AGCCTRL1, 0x40 },
    { CC1101_AGCCTRL0, 0x91 },
    { CC1101_FREND1,   0x56 },
    { CC1101_FREND0,   0x10 },   /* PA_POWER index 0 */
    { CC1101_FSCAL3,   0xE9 },
    { CC1101_FSCAL2,   0x2A },
    { CC1101_FSCAL1,   0x00 },
    { CC1101_FSCAL0,   0x1F },
};

static inline void cs_low(void)  { PORTA.OUTCLR = PIN_SPI_CSN; }
static inline void cs_high(void) { PORTA.OUTSET = PIN_SPI_CSN; }

static void spi_open(void)
{
    PORTA.PIN2CTRL = 0;                          /* MISO input buffer on */
    PORTA.DIRSET = PIN_SPI_MOSI | PIN_SPI_SCK | PIN_SPI_CSN;
    PORTA.DIRCLR = PIN_SPI_MISO;
    cs_high();
    SPI0.CTRLB = SPI_SSD_bm;                     /* no SS pin handling, mode 0 */
    SPI0.CTRLA = SPI_MASTER_bm | SPI_PRESC_DIV4_gc | SPI_ENABLE_bm;   /* 833 kHz */
}

static void spi_close(void)
{
    SPI0.CTRLA = 0;
    PORTA.OUTCLR = PIN_SPI_MOSI | PIN_SPI_SCK;
    cs_high();
    PORTA.PIN2CTRL = PORT_ISC_INPUT_DISABLE_gc;  /* MISO floats in SLEEP */
}

static uint8_t spi_xfer(uint8_t d)
{
    SPI0.DATA = d;
    uint16_t t = SPI_TIMEOUT_LOOPS;
    while (!(SPI0.INTFLAGS & SPI_IF_bm)) {
        if (--t == 0) {
            return 0xFF;
        }
    }
    return SPI0.DATA;
}

/** CSn low and wait for SO low (chip ready / crystal running). */
static bool cs_select_wait_ready(void)
{
    cs_low();
    uint16_t t = OSC_TIMEOUT_LOOPS;
    while (PORTA.IN & PIN_SPI_MISO) {
        if (--t == 0) {
            cs_high();
            return false;
        }
    }
    return true;
}

static bool strobe(uint8_t s)
{
    if (!cs_select_wait_ready()) {
        return false;
    }
    (void)spi_xfer(s);
    cs_high();
    return true;
}

static void write_reg(uint8_t reg, uint8_t val)
{
    if (!cs_select_wait_ready()) {
        return;
    }
    (void)spi_xfer(reg & 0x3F);
    (void)spi_xfer(val);
    cs_high();
}

static uint8_t read_status(uint8_t reg)
{
    if (!cs_select_wait_ready()) {
        return 0xFF;
    }
    (void)spi_xfer(reg | 0xC0);
    uint8_t v = spi_xfer(0x00);
    cs_high();
    return v;
}

static void write_burst(uint8_t reg, const uint8_t *buf, uint8_t len)
{
    if (!cs_select_wait_ready()) {
        return;
    }
    (void)spi_xfer((reg & 0x3F) | 0x40);
    for (uint8_t i = 0; i < len; i++) {
        (void)spi_xfer(buf[i]);
    }
    cs_high();
}

static void restore_volatile_regs(void)
{
    /* Not retained in SLEEP: TEST2..0 and PATABLE (except index 0 on some revs) */
    write_reg(CC1101_TEST2, 0x81);
    write_reg(CC1101_TEST1, 0x35);
    write_reg(CC1101_TEST0, 0x09);
    const uint8_t pa = RF_PA_VALUE;
    write_burst(CC1101_PATABLE, &pa, 1);
}

static void enter_sleep(void)
{
    (void)strobe(CC1101_SIDLE);
    (void)strobe(CC1101_SPWD);                   /* SLEEP once CSn goes high */
    spi_close();
}

bool cc1101_init(void)
{
    spi_open();

    /* Manual power-on reset sequence (datasheet 19.1.2) */
    cs_high();
    _delay_us(5);
    cs_low();
    _delay_us(10);
    cs_high();
    _delay_us(45);

    if (!strobe(CC1101_SRES)) {
        spi_close();
        return false;
    }
    _delay_ms(1);

    uint8_t ver = read_status(CC1101_VERSION);
    if (ver == 0x00 || ver == 0xFF) {
        spi_close();
        return false;
    }

    for (uint8_t i = 0; i < sizeof(k_cfg) / sizeof(k_cfg[0]); i++) {
        write_reg(k_cfg[i][0], k_cfg[i][1]);
    }
    restore_volatile_regs();

    enter_sleep();
    return true;
}

static uint8_t read_burst(uint8_t reg, uint8_t *buf, uint8_t len)
{
    if (!cs_select_wait_ready()) {
        return 0;
    }
    (void)spi_xfer((reg & 0x3F) | 0xC0);
    for (uint8_t i = 0; i < len; i++) {
        buf[i] = spi_xfer(0x00);
    }
    cs_high();
    return len;
}

/* Wait for GDO0 high (sync) then low (end of packet). Returns false on timeout. */
static bool wait_packet(uint16_t start_ms, uint16_t total_ms)
{
    uint16_t ms = start_ms;
    while (!(PORTB.IN & PIN_RF_GDO0)) {
        _delay_ms(1);
        if (--ms == 0) {
            return false;
        }
    }
    ms = total_ms;
    while (PORTB.IN & PIN_RF_GDO0) {
        _delay_ms(1);
        if (--ms == 0) {
            return false;
        }
    }
    return true;
}

int8_t cc1101_send(const uint8_t *tx, uint8_t txlen, uint8_t *rx, uint8_t rxmax, uint16_t rx_window_ms)
{
    int8_t result = -1;

    if (tx == NULL || txlen == 0 || txlen > 60) {
        return -1;
    }
    spi_open();
    PORTB.PIN2CTRL = 0;                          /* GDO0 input buffer on */

    if (!cs_select_wait_ready()) {               /* wake-up: crystal running */
        goto out;
    }
    cs_high();
    _delay_us(20);

    restore_volatile_regs();
    (void)strobe(CC1101_SIDLE);
    (void)strobe(CC1101_SFTX);
    write_reg(CC1101_FIFO, txlen);               /* length byte */
    write_burst(CC1101_FIFO, tx, txlen);
    (void)strobe(CC1101_STX);                    /* calibrates, then sends */

    if (!wait_packet(TX_TIMEOUT_MS, TX_TIMEOUT_MS)) {
        goto out;
    }
    result = 0;

    if (rx != NULL && rxmax > 0 && rx_window_ms > 0) {
        /* radio is IDLE after TX: listen for a reply */
        (void)strobe(CC1101_SFRX);
        (void)strobe(CC1101_SRX);
        if (wait_packet(rx_window_ms, TX_TIMEOUT_MS)) {
            uint8_t n = read_status(CC1101_RXBYTES) & 0x7F;
            uint8_t len = 0;
            if (n >= 3) {
                (void)read_burst(CC1101_FIFO, &len, 1);
                if (len > 0 && len <= rxmax && (uint8_t)(len + 3) <= n) {
                    uint8_t st[2];
                    (void)read_burst(CC1101_FIFO, rx, len);
                    (void)read_burst(CC1101_FIFO, st, 2);
                    if (st[1] & 0x80) {          /* CRC_OK */
                        result = (int8_t)len;
                    }
                }
            }
        }
        (void)strobe(CC1101_SIDLE);
        (void)strobe(CC1101_SFRX);
    }

out:
    PORTB.PIN2CTRL = PORT_ISC_INPUT_DISABLE_gc;
    enter_sleep();
    return result;
}
