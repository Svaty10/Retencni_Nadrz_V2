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
#include "power.h"
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

bool cc1101_sleep_ok = true;

/** IDLE + SPWD. If a strobe times out (SO never low) the radio may still be awake
 *  (mA), so the failure is recorded in cc1101_sleep_ok for the caller to re-init. */
static bool enter_sleep(void)
{
    bool ok = strobe(CC1101_SIDLE);
    ok = strobe(CC1101_SPWD) && ok;              /* SLEEP once CSn goes high */
    spi_close();
    cc1101_sleep_ok = ok;
    return ok;
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

    return enter_sleep();
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

void (*cc1101_tx_hook)(void) = NULL;
uint8_t cc1101_lbt_backoffs = 0;

/** Status register read twice until stable (CC1101 errata: SPI read synchronisation). */
static uint8_t read_status_stable(uint8_t reg)
{
    uint8_t a = read_status(reg);
    for (uint8_t i = 0; i < 4; i++) {
        uint8_t b = read_status(reg);
        if (a == b) {
            break;
        }
        a = b;
    }
    return a;
}

/** End of transmission: the MCU sleeps in 4 ms steps until the radio is back in IDLE
 *  (MCSM1: TX -> IDLE) and the TX FIFO is empty (the frame really went out). */
/* Diagnostic build only (-DCC1101_DIAG, diag.c): the normal firmware stays unchanged. */
#ifdef CC1101_DIAG
bool cc1101_gdo0_seen = false;
#define NOTE_GDO0()  do { if (PORTB.IN & PIN_RF_GDO0) { cc1101_gdo0_seen = true; } } while (0)
#define CLEAR_GDO0() do { cc1101_gdo0_seen = false; } while (0)
#else
#define NOTE_GDO0()  do { } while (0)
#define CLEAR_GDO0() do { } while (0)
#endif

static bool wait_tx_done(void)
{
    for (uint8_t i = 0; i < TX_TIMEOUT_MS / 4U; i++) {
        power_sleep_ms(4);
        NOTE_GDO0();                             /* sync word sent (IOCFG0 = 0x06) */
        if ((read_status_stable(CC1101_MARCSTATE) & 0x1F) == 0x01) {
            return (read_status_stable(CC1101_TXBYTES) & 0x7F) == 0;
        }
    }
    return false;
}

/** Listen briefly (radio in IDLE with the TX FIFO loaded): true if the channel is free. */
static bool channel_clear(void)
{
    (void)strobe(CC1101_SRX);                    /* auto-calibrates IDLE -> RX */
    _delay_us(2500);                             /* calibration + RSSI valid */
    int8_t raw = (int8_t)read_status(CC1101_RSSI);
    (void)strobe(CC1101_SIDLE);
    _delay_us(50);                               /* SFRX is only valid in IDLE */
    (void)strobe(CC1101_SFRX);
    int16_t dbm = (int16_t)raw / 2 - 74;
    return dbm < LBT_BUSY_DBM;
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
    /* listen before talk (TX FIFO survives IDLE/RX); STX is issued from IDLE, so the
     * hardware CCA never blocks it – after LBT_MAX_TRIES we send anyway */
    cc1101_lbt_backoffs = 0;
    while (cc1101_lbt_backoffs < LBT_MAX_TRIES && !channel_clear()) {
        cc1101_lbt_backoffs++;
        power_sleep_ms((uint16_t)(LBT_BACKOFF_MIN_MS + (rng_next() & (LBT_BACKOFF_SPAN_MS - 1U))));
    }
    CLEAR_GDO0();
    (void)strobe(CC1101_STX);                    /* calibrates, then sends */
    if (cc1101_tx_hook != NULL) {
        cc1101_tx_hook();                        /* runs while the frame is on air */
    }
    if (!wait_tx_done()) {
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
    (void)enter_sleep();                         /* failure -> cc1101_sleep_ok = false */
    return result;
}

/* ======================================================================== */
/* Diagnostics (diagnostic firmware diag.c only, built with -DCC1101_DIAG)   */
/* ======================================================================== */
#ifdef CC1101_DIAG

/** RSSI register / appended status byte -> dBm (datasheet 17.3, offset 74 at 4.8 kBaud). */
static int8_t rssi_dbm(uint8_t raw)
{
    int16_t dbm = (int16_t)(int8_t)raw / 2 - 74;
    return (int8_t)(dbm < -128 ? -128 : dbm);
}

static uint8_t read_reg(uint8_t reg)
{
    if (!cs_select_wait_ready()) {
        return 0xFF;
    }
    (void)spi_xfer((reg & 0x3F) | 0x80);         /* single read of a config register */
    uint8_t v = spi_xfer(0x00);
    cs_high();
    return v;
}

void cc1101_probe(cc1101_probe_t *p)
{
    p->so_ready = false;
    p->spi_rw = false;
    p->pll_lock = false;
    p->partnum = 0xFF;
    p->version = 0xFF;
    p->marcstate = 0xFF;
    p->fscal1 = 0xFF;

    spi_open();
    cs_high();                                   /* manual power-on reset, as in cc1101_init */
    _delay_us(5);
    cs_low();
    _delay_us(10);
    cs_high();
    _delay_us(45);
    if (!strobe(CC1101_SRES)) {                  /* SO never went low */
        spi_close();
        return;
    }
    p->so_ready = true;
    _delay_ms(1);
    p->partnum = read_status(0x30);              /* PARTNUM */
    p->version = read_status(CC1101_VERSION);

    /* bus test: two complementary patterns written and read back */
    write_reg(CC1101_ADDR, 0xA5);
    uint8_t a = read_reg(CC1101_ADDR);
    write_reg(CC1101_ADDR, 0x5A);
    uint8_t b = read_reg(CC1101_ADDR);
    p->spi_rw = (a == 0xA5) && (b == 0x5A);

    for (uint8_t i = 0; i < sizeof(k_cfg) / sizeof(k_cfg[0]); i++) {
        write_reg(k_cfg[i][0], k_cfg[i][1]);
    }
    restore_volatile_regs();

    /* manual calibration of the synthesizer (~0.8 ms), then back in IDLE */
    (void)strobe(CC1101_SIDLE);
    (void)strobe(CC1101_SCAL);
    for (uint8_t i = 0; i < 20U; i++) {
        _delay_ms(1);
        if ((read_status_stable(CC1101_MARCSTATE) & 0x1F) == 0x01) {
            break;
        }
    }
    p->marcstate = read_status_stable(CC1101_MARCSTATE) & 0x1F;
    p->fscal1 = read_reg(CC1101_FSCAL1);
    p->pll_lock = p->marcstate == 0x01 && p->fscal1 != 0x3F;
    (void)enter_sleep();
}

#define CC1101_IOCFG1       0x01

static bool miso_high(void) { return (PORTA.IN & PIN_SPI_MISO) != 0; }

/** Level changes on GDO0 in ~3 ms of polling (a clock of 135 kHz gives dozens). */
static uint8_t gdo0_edges(void)
{
    uint8_t n = 0;
    uint8_t last = PORTB.IN & PIN_RF_GDO0;
    for (uint16_t i = 0; i < 1500U; i++) {
        uint8_t v = PORTB.IN & PIN_RF_GDO0;
        if (v != last) {
            last = v;
            if (n < 255U) {
                n++;
            }
        }
    }
    return n;
}

static bool gdo0_steady(bool high)
{
    _delay_us(50);
    for (uint16_t i = 0; i < 500U; i++) {
        if (((PORTB.IN & PIN_RF_GDO0) != 0) != high) {
            return false;
        }
    }
    return true;
}

void cc1101_lines(cc1101_lines_t *l)
{
    uint8_t f = 0, g = 0;

    /* 1) the pins as plain I/O: SPI off, MISO with the internal pull-up, CSn high
     *    (the CC1101 keeps SO in high impedance while CSn is high) */
    SPI0.CTRLA = 0;
    PORTA.DIRSET = PIN_SPI_MOSI | PIN_SPI_SCK | PIN_SPI_CSN;
    PORTA.DIRCLR = PIN_SPI_MISO;
    PORTA.PIN2CTRL = PORT_PULLUPEN_bm;
    cs_high();
    PORTA.OUTSET = PIN_SPI_MOSI | PIN_SPI_SCK;
    _delay_us(200);
    bool idle = miso_high();
    if (idle) {
        f |= CL_MISO_IDLE_HI;
    }
    PORTA.OUTCLR = PIN_SPI_MOSI;
    _delay_us(100);
    if (idle && !miso_high()) {
        f |= CL_MISO_MOSI;
    }
    PORTA.OUTSET = PIN_SPI_MOSI;
    PORTA.OUTCLR = PIN_SPI_SCK;
    _delay_us(100);
    if (idle && !miso_high()) {
        f |= CL_MISO_SCK;
    }
    PORTA.OUTCLR = PIN_SPI_MOSI;

    /* 2) CSn low: the chip wakes up and pulls SO low once its crystal runs */
    cs_low();
    for (uint16_t t = 0; t < 3000U; t++) {
        if (!miso_high()) {
            f |= CL_SO_LOW;
            break;
        }
        _delay_us(1);
    }
    _delay_us(50);
    cs_high();
    _delay_us(200);
    if (miso_high()) {
        f |= CL_SO_RELEASE;
    }

    /* 3) does the radio take commands? Checked on GDO0 and on SO driven as GDO1,
     *    not by reading registers back over MISO. */
    spi_open();
    PORTB.PIN2CTRL = 0;                          /* GDO0 input buffer on */
    (void)strobe(CC1101_SRES);
    _delay_ms(1);
    l->clk_edges = gdo0_edges();
    if (l->clk_edges >= 8U) {
        f |= CL_GDO0_CLK;
    }
    write_reg(CC1101_IOCFG0, 0x2F);              /* GDO0: constant 0 */
    if (gdo0_steady(false)) {
        f |= CL_GDO0_LO;
    }
    write_reg(CC1101_IOCFG0, 0x6F);              /* GDO0: constant 0, inverted = 1 */
    if (gdo0_steady(true)) {
        f |= CL_GDO0_HI;
    }
    write_reg(CC1101_IOCFG1, 0x2F);              /* SO/GDO1 while CSn high: constant 0 */
    PORTA.PIN2CTRL = PORT_PULLUPEN_bm;
    _delay_us(200);
    if (!miso_high()) {
        g |= CL2_SO_DRIVE_LO;
    }
    PORTA.PIN2CTRL = 0;
    write_reg(CC1101_IOCFG1, 0x6F);              /* ... constant 1 (pull-up off: the chip must drive it) */
    _delay_us(200);
    if (miso_high()) {
        g |= CL2_SO_DRIVE_HI;
    }
    write_reg(CC1101_IOCFG1, 0x2E);
    (void)strobe(CC1101_SRES);
    PORTB.PIN2CTRL = PORT_ISC_INPUT_DISABLE_gc;
    spi_close();

    l->flags = f;
    l->flags2 = g;
}

void cc1101_listen(uint16_t ms, cc1101_heard_t *h)
{
    uint8_t buf[64];

    h->count = 0;
    h->errors = 0;
    h->best_dbm = -128;
    h->floor_dbm = -128;
    h->last_type = 0;
    h->last_id = 0;

    spi_open();
    PORTB.PIN2CTRL = 0;                          /* GDO0 input buffer on */
    if (!cs_select_wait_ready()) {
        goto out;
    }
    cs_high();
    _delay_us(20);
    restore_volatile_regs();
    (void)strobe(CC1101_SIDLE);
    (void)strobe(CC1101_SFRX);
    (void)strobe(CC1101_SRX);
    _delay_us(2500);                             /* calibration + RSSI valid */
    h->floor_dbm = rssi_dbm(read_status(CC1101_RSSI));

    while (ms > 0) {
        if (!(PORTB.IN & PIN_RF_GDO0)) {         /* no sync word yet */
            _delay_ms(1);
            ms--;
            if ((ms & 0xFFU) == 0U) {
                wdt_kick();
            }
            continue;
        }
        uint8_t t = 0;                           /* receiving: wait for the end of the packet */
        while ((PORTB.IN & PIN_RF_GDO0) && t < TX_TIMEOUT_MS) {
            _delay_ms(1);
            t++;
        }
        ms = (ms > t) ? (uint16_t)(ms - t) : 0U;
        uint8_t n = read_status_stable(CC1101_RXBYTES);
        uint8_t len = 0;
        bool good = false;
        if (!(n & 0x80) && (n & 0x7F) >= 3U) {   /* no overflow, length + status at least */
            (void)read_burst(CC1101_FIFO, &len, 1);
            if (len > 0 && len <= sizeof(buf) - 2U && (uint8_t)(len + 3U) <= (n & 0x7F)) {
                uint8_t st[2];
                (void)read_burst(CC1101_FIFO, buf, len);
                (void)read_burst(CC1101_FIFO, st, 2);
                if (st[1] & 0x80) {              /* CRC_OK */
                    good = true;
                    int8_t dbm = rssi_dbm(st[0]);
                    if (h->count < 255U) {
                        h->count++;
                    }
                    if (dbm > h->best_dbm) {
                        h->best_dbm = dbm;
                    }
                    h->last_type = buf[0];
                    h->last_id = (len >= 3U) ? (uint16_t)(buf[1] | ((uint16_t)buf[2] << 8)) : 0U;
                }
            }
        }
        if (!good && h->errors < 255U) {
            h->errors++;
        }
        (void)strobe(CC1101_SIDLE);              /* MCSM1: RX -> IDLE after a packet */
        (void)strobe(CC1101_SFRX);
        (void)strobe(CC1101_SRX);
        wdt_kick();
    }
    (void)strobe(CC1101_SIDLE);
    (void)strobe(CC1101_SFRX);
out:
    PORTB.PIN2CTRL = PORT_ISC_INPUT_DISABLE_gc;
    (void)enter_sleep();
}

#endif /* CC1101_DIAG */
