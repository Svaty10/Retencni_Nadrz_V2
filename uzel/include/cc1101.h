/**
 * @file cc1101.h
 * @brief Low-power transmit-only CC1101 driver (433.92 MHz, 2-FSK, 4.8 kBaud).
 *
 * The register set MUST match server/retencka/radio.py.
 */

#ifndef CC1101_H_
#define CC1101_H_

#include "config.h"

/* Command strobes */
#define CC1101_SRES         0x30
#define CC1101_SCAL         0x33
#define CC1101_STX          0x35
#define CC1101_SIDLE        0x36
#define CC1101_SPWD         0x39
#define CC1101_SRX          0x34
#define CC1101_SFRX         0x3A
#define CC1101_SFTX         0x3B
#define CC1101_SNOP         0x3D

/* Configuration registers */
#define CC1101_IOCFG2       0x00
#define CC1101_IOCFG0       0x02
#define CC1101_FIFOTHR      0x03
#define CC1101_SYNC1        0x04
#define CC1101_SYNC0        0x05
#define CC1101_PKTLEN       0x06
#define CC1101_PKTCTRL1     0x07
#define CC1101_PKTCTRL0     0x08
#define CC1101_ADDR         0x09
#define CC1101_CHANNR       0x0A
#define CC1101_FSCTRL1      0x0B
#define CC1101_FSCTRL0      0x0C
#define CC1101_FREQ2        0x0D
#define CC1101_FREQ1        0x0E
#define CC1101_FREQ0        0x0F
#define CC1101_MDMCFG4      0x10
#define CC1101_MDMCFG3      0x11
#define CC1101_MDMCFG2      0x12
#define CC1101_MDMCFG1      0x13
#define CC1101_MDMCFG0      0x14
#define CC1101_DEVIATN      0x15
#define CC1101_MCSM1        0x17
#define CC1101_MCSM0        0x18
#define CC1101_FOCCFG       0x19
#define CC1101_BSCFG        0x1A
#define CC1101_AGCCTRL2     0x1B
#define CC1101_AGCCTRL1     0x1C
#define CC1101_AGCCTRL0     0x1D
#define CC1101_FREND1       0x21
#define CC1101_FREND0       0x22
#define CC1101_FSCAL3       0x23
#define CC1101_FSCAL2       0x24
#define CC1101_FSCAL1       0x25
#define CC1101_FSCAL0       0x26
#define CC1101_TEST2        0x2C
#define CC1101_TEST1        0x2D
#define CC1101_TEST0        0x2E
#define CC1101_PATABLE      0x3E
#define CC1101_FIFO         0x3F

/* Status registers (burst bit set) */
#define CC1101_VERSION      (0x31 | 0xC0)
#define CC1101_RSSI         (0x34 | 0xC0)
#define CC1101_MARCSTATE    (0x35 | 0xC0)
#define CC1101_TXBYTES      (0x3A | 0xC0)
#define CC1101_RXBYTES      (0x3B | 0xC0)

/** Reset, detect and configure the CC1101, then put it into SLEEP. */
bool cc1101_init(void);

/**
 * @brief Wake up, transmit one packet (variable length) and optionally listen for a reply.
 * @param rx_window_ms 0 = no reply expected
 * @return -1 on error, 0 = sent (no reply), >0 = length of a CRC-valid reply in rx
 */
/** Called once right after the transmission started (e.g. battery under load);
 *  must finish within ~45 ms. NULL = none. */
extern void (*cc1101_tx_hook)(void);

/** Listen-before-talk back-offs of the last cc1101_send(). */
extern uint8_t cc1101_lbt_backoffs;

/** false when the last attempt to put the radio to SLEEP timed out (it may still be
 *  awake drawing mA): the caller must treat the radio as not ready and re-init it. */
extern bool cc1101_sleep_ok;

int8_t cc1101_send(const uint8_t *tx, uint8_t txlen, uint8_t *rx, uint8_t rxmax, uint16_t rx_window_ms);

/** Diagnostic build only (-DCC1101_DIAG): true when GDO0 (PB2) went high during the last transmission of cc1101_send –
 *  the radio signals "sync word sent", so the GDO0 line to the MCU works. */
extern bool cc1101_gdo0_seen;

/* -------------------- diagnostics (diag.c only, built with -DCC1101_DIAG) */
typedef struct {
    bool    so_ready;       /**< SO went low after CSn: chip answers, crystal runs */
    bool    spi_rw;         /**< register write + read back matched (MOSI/MISO/SCK/CSn ok) */
    bool    pll_lock;       /**< synthesizer locked after a manual calibration */
    uint8_t partnum;        /**< 0x00 for a CC1101 */
    uint8_t version;        /**< 0x14 (or 0x04 on old chips) */
    uint8_t marcstate;      /**< state after the calibration (0x01 = IDLE) */
    uint8_t fscal1;         /**< 0x3F = no lock */
} cc1101_probe_t;

typedef struct {
    uint8_t  count;         /**< CRC-valid packets received */
    uint8_t  errors;        /**< packets with a bad CRC or length */
    int8_t   best_dbm;      /**< strongest packet, -128 = none */
    int8_t   floor_dbm;     /**< channel noise at the start of the listen */
    uint8_t  last_type;     /**< first byte of the last good packet */
    uint16_t last_id;       /**< bytes 1..2 (node ID in all our packet types) */
} cc1101_heard_t;

/** Reset, identify and fully configure the radio, test the SPI bus and the PLL,
 *  then leave the radio configured in SLEEP (ready for cc1101_send). */
void cc1101_probe(cc1101_probe_t *p);

/* Line test (cc1101_lines): the SPI pins checked one by one as plain I/O, and the
 * radio told to drive GDO0 / SO to fixed levels – tells which side is broken even
 * when nothing can be read back over SPI. */
#define CL_MISO_IDLE_HI     0x01U   /**< MISO high with CSn high (internal pull-up): line free */
#define CL_MISO_MOSI        0x02U   /**< MISO follows MOSI: the two lines are joined */
#define CL_MISO_SCK         0x04U   /**< MISO follows SCLK: the two lines are joined */
#define CL_SO_LOW           0x08U   /**< SO pulled low after CSn low (chip selected, crystal runs) */
#define CL_SO_RELEASE       0x10U   /**< SO released again after CSn high */
#define CL_GDO0_CLK         0x20U   /**< clock on GDO0 after SRES (default CLK_XOSC/192) */
#define CL_GDO0_LO          0x40U   /**< GDO0 low after writing IOCFG0 = 0x2F (radio took a write) */
#define CL_GDO0_HI          0x80U   /**< GDO0 high after writing IOCFG0 = 0x6F */
#define CL2_SO_DRIVE_LO     0x01U   /**< SO pin driven low by IOCFG1 = 0x2F (CSn high, pull-up on) */
#define CL2_SO_DRIVE_HI     0x02U   /**< SO pin driven high by IOCFG1 = 0x6F (CSn high, pull-up off) */

typedef struct {
    uint8_t flags;          /**< CL_* */
    uint8_t flags2;         /**< CL2_* */
    uint8_t clk_edges;      /**< GDO0 level changes counted after SRES (255 = many) */
} cc1101_lines_t;

/** Line test, run before cc1101_probe (leaves the radio reset, SPI closed). */
void cc1101_lines(cc1101_lines_t *l);

/** Listen `ms` milliseconds and count every packet on the channel (any sender). */
void cc1101_listen(uint16_t ms, cc1101_heard_t *h);

#endif /* CC1101_H_ */
