/**
 * @file diag.c
 * @brief Diagnostic firmware of the tank sensor node (separate build: make diag,
 *        7-diagnostika.cmd). Tests the board and the radio over and over and keeps
 *        the last result in the EEPROM (0xC0.., outside the pairing keys);
 *        8-precist-diagnostiku.cmd reads it over UPDI and explains it
 *        (docs/diagnostika_uzlu.md). Works even with a dead radio; nothing on the hub.
 *
 * Cycle (~6 s): line test of the SPI pins (cc1101_lines) -> radio probe (SPI bus,
 * crystal, PLL) -> battery, SHT40, probe (5 V boost on for the measurement)
 * -> 2 s listening on the channel (any packet: other tank sensors, anemometers)
 * -> one test transmission (plain DIAG packet 0x07; the hub counts it as a foreign
 * packet and drops it) -> EEPROM record -> 4 s sleep.
 *
 * The pairing keys in the EEPROM are never written, only read (is a key there?).
 * Back to normal: flash the normal firmware (3-aktualizace-firmwaru.cmd), the
 * pairing is kept.
 */

#include "config.h"
#include "power.h"
#include "adc.h"
#include "sht40.h"
#include "cc1101.h"
#include <avr/eeprom.h>
#include <string.h>

#define DIAG_VERSION        1U
#define DIAG_PERIOD_S       4U
#define DIAG_LISTEN_MS      2000U

/* DIAG packet (diagnostic firmware only, nobody answers it), 25 B, little endian:
 *  0 type 0x07 | 1 id u16 | 3 diag version | 4 seq | 5 RSTCTRL flags | 6 vbat mV u16
 *  8 SHT40 temp x100 i16 | 10 SHT40 RH % | 11 probe 0.1 mV u16 | 13 PARTNUM | 14 VERSION
 * 15 flags | 16 MARCSTATE | 17 FSCAL1 | 18 heard | 19 bad packets | 20 best dBm
 * 21 noise dBm | 22 last type | 23 last id u16 */
#define PKT_DIAG            0x07
#define PKT_DIAG_LEN        25U

/* EEPROM: the normal firmware keeps two copies of its key store (2 x 59 B) from 0 */
#define KEY_STORE_SIZE      59U
#define KEY_STORE_MAGIC     0x314B5452UL     /* "RTK1", see link.c */
#define DIAG_EE_ADDR        0xC0U
#define DIAG_EE_MAGIC       0x47414944UL     /* "DIAG" */

/* flags byte of the DIAG packet */
#define DF_RF_SO            0x01U   /* radio answers (SO low after CSn), crystal runs */
#define DF_RF_SPI           0x02U   /* register write/read back ok */
#define DF_RF_PLL           0x04U   /* PLL locked after calibration */
#define DF_RF_GDO0          0x08U   /* GDO0 rose during the transmission */
#define DF_TX_OK            0x10U   /* transmission finished (TX -> IDLE, FIFO empty) */
#define DF_PROBE_CLIP       0x20U   /* probe ADC at full scale */
#define DF_PAIRED           0x40U   /* a pairing key is stored in the EEPROM */
#define DF_SHT_OK           0x80U   /* SHT40 answered with a valid CRC */

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t cycles;
    uint16_t heard_total;             /* packets heard on the channel since the start */
    int8_t   heard_best;              /* strongest of them [dBm], -128 = none */
    uint8_t  pkt[PKT_DIAG_LEN];
    uint8_t  lines;                   /* cc1101_lines: CL_* */
    uint8_t  lines2;                  /* CL2_* */
    uint8_t  clk_edges;               /* GDO0 clock edges after SRES */
    uint16_t crc;
} diag_ee_t;

static uint16_t crc16(const uint8_t *d, uint8_t n)
{
    uint16_t crc = 0xFFFF;
    for (uint8_t i = 0; i < n; i++) {
        crc ^= (uint16_t)d[i] << 8;
        for (uint8_t b = 0; b < 8; b++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

/** A pairing key in either copy of the normal firmware's store (magic + paired flag). */
static bool key_stored(void)
{
    for (uint8_t i = 0; i < 2U; i++) {
        uint16_t a = (uint16_t)(i * KEY_STORE_SIZE);
        if (eeprom_read_dword((const uint32_t *)a) == KEY_STORE_MAGIC
                && eeprom_read_byte((const uint8_t *)(a + 4U)) == 1U) {
            return true;
        }
    }
    return false;
}

static void put16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

int main(void)
{
    uint8_t rst = power_take_reset_flags();

    power_init();
    wdt_init();
    adc_init();
    rtc_pit_init();
    uint16_t id = power_node_id();
    rng_seed(id);
    bool paired = key_stored();

    diag_ee_t rec;
    uint8_t *pkt = rec.pkt;
    uint8_t seq = 0;
    uint16_t cycles = 0;
    rec.heard_total = 0;
    rec.heard_best = -128;

    for (;;) {
        wdt_kick();
        cc1101_lines_t ln;
        cc1101_lines(&ln);
        rec.lines = ln.flags;
        rec.lines2 = ln.flags2;
        rec.clk_edges = ln.clk_edges;
        wdt_kick();
        cc1101_probe_t pr;
        cc1101_probe(&pr);
        wdt_kick();

        uint16_t vbat = PKT_U16_INVALID;
        if (!adc_measure_battery(&vbat)) {
            vbat = PKT_U16_INVALID;
        }
        int16_t temp = PKT_TEMP_INVALID;
        uint16_t hum = 0;
        bool sht = sht40_read_measurements(&temp, &hum);
        if (!sht) {
            temp = PKT_TEMP_INVALID;
        }
        wdt_kick();
        uint16_t probe = PKT_U16_INVALID;
        bool clipped = false;
        if (!adc_measure_probe(&probe, &clipped)) {
            probe = PKT_U16_INVALID;
        }
        wdt_kick();

        cc1101_heard_t h;
        memset(&h, 0, sizeof(h));
        h.best_dbm = -128;
        h.floor_dbm = -128;
        if (pr.so_ready) {
            cc1101_listen(DIAG_LISTEN_MS, &h);
        }
        if ((uint16_t)(rec.heard_total + h.count) >= rec.heard_total) {
            rec.heard_total = (uint16_t)(rec.heard_total + h.count);
        }
        if (h.count && h.best_dbm > rec.heard_best) {
            rec.heard_best = h.best_dbm;
        }

        uint8_t flags = 0;
        flags |= pr.so_ready ? DF_RF_SO : 0U;
        flags |= pr.spi_rw ? DF_RF_SPI : 0U;
        flags |= pr.pll_lock ? DF_RF_PLL : 0U;
        flags |= clipped ? DF_PROBE_CLIP : 0U;
        flags |= paired ? DF_PAIRED : 0U;
        flags |= sht ? DF_SHT_OK : 0U;

        pkt[0] = PKT_DIAG;
        put16(&pkt[1], id);
        pkt[3] = DIAG_VERSION;
        pkt[4] = seq;
        pkt[5] = rst;
        put16(&pkt[6], vbat);
        put16(&pkt[8], (uint16_t)temp);
        pkt[10] = sht ? (uint8_t)((hum + 50U) / 100U) : 0xFFU;
        put16(&pkt[11], probe);
        pkt[13] = pr.partnum;
        pkt[14] = pr.version;
        pkt[15] = flags;
        pkt[16] = pr.marcstate;
        pkt[17] = pr.fscal1;
        pkt[18] = h.count;
        pkt[19] = h.errors;
        pkt[20] = (uint8_t)h.best_dbm;
        pkt[21] = (uint8_t)h.floor_dbm;
        pkt[22] = h.last_type;
        put16(&pkt[23], h.last_id);

        /* ---------------- test transmission (its result goes to this record) ---------------- */
        if (pr.so_ready) {
            wdt_kick();
            int8_t r = cc1101_send(pkt, PKT_DIAG_LEN, NULL, 0, 0);
            pkt[15] |= (r >= 0) ? DF_TX_OK : 0U;
            pkt[15] |= cc1101_gdo0_seen ? DF_RF_GDO0 : 0U;
        }

        /* ---------------- EEPROM record ---------------- */
        if (cycles < 15U || (cycles % 15U) == 0U) {      /* first ~2 min, then once a ~2 min */
            rec.magic = DIAG_EE_MAGIC;
            rec.cycles = cycles;
            rec.crc = crc16((const uint8_t *)&rec, (uint8_t)(sizeof(rec) - 2U));
            wdt_kick();
            eeprom_update_block(&rec, (void *)DIAG_EE_ADDR, sizeof(rec));
        }
        if (cycles < 0xFFFFU) {
            cycles++;
        }
        seq++;
        power_sleep_seconds(DIAG_PERIOD_S);
    }
}
