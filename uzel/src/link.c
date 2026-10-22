/**
 * @file link.c
 * @brief Secure radio link of the sensor node.
 *
 * Keys come from an X25519 exchange with the hub (pairing) and are stored in
 * EEPROM, so a battery change keeps the existing pairing. A paired node can only
 * be released by its own hub: an UNPAIR authenticated with the current key, sent
 * during the 10 min window after a battery re-insertion (= physical access).
 * Only an unpaired node accepts a new key (PAIR_ACK).
 *
 * Frame counter: 32 bit, strictly increasing over the whole life of the node.
 * EEPROM holds a reserved upper bound (ctr_base); after any reset counting
 * resumes from that bound, so a counter value is never reused (replay safety)
 * while EEPROM is written only once per CTR_RESERVE frames.
 */

#include "link.h"
#include "cc1101.h"
#include "crypto.h"
#include "proto.h"
#include "power.h"
#include "adc.h"
#include <avr/eeprom.h>
#include <avr/cpufunc.h>
#include <string.h>

#define STORE_MAGIC 0x314B5452UL       /* "RTK1" */

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t  paired;
    uint8_t  k_enc[16];
    uint8_t  k_mac[16];
    uint32_t ctr_base;
    uint8_t  pool[16];                 /* entropy pool carried over resets */
    uint16_t crc;
} store_t;

static store_t EEMEM ee_store;
static store_t g_st;
static uint16_t g_node_id;
static uint32_t g_ctr;
static bool g_keypair_ready;
static uint8_t g_priv[32];
static uint8_t g_pub[32];

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

static void store_save(void)
{
    g_st.magic = STORE_MAGIC;
    g_st.crc = crc16((const uint8_t *)&g_st, sizeof(g_st) - 2);
    wdt_kick();
    eeprom_update_block(&g_st, &ee_store, sizeof(g_st));
}

/* -------------------------------------------------------------- entropy */
/* No TRNG on the ATtiny1624: mix ADC noise of the internal temperature sensor,
 * jitter between the 32 kHz ULP oscillator and the CPU clock, the factory serial
 * number and the pool saved before the last reset, through AES-CMAC. */
static void entropy_fill(uint8_t out[16])
{
    uint8_t buf[48];
    uint8_t mac[16];

    memcpy(mac, g_st.pool, 16);
    for (uint8_t round = 0; round < 16; round++) {
        wdt_kick();
        /* ADC noise */
        ADC0.CTRLA = ADC_ENABLE_bm;
        ADC0.CTRLC = ADC_REFSEL_1024MV_gc | (uint8_t)(4 << ADC_TIMEBASE_gp);
        ADC0.CTRLF = 0;
        ADC0.MUXPOS = ADC_MUXPOS_TEMPSENSE_gc;
        for (uint8_t i = 0; i < 32; i++) {
            ADC0.COMMAND = ADC_MODE_SINGLE_12BIT_gc | ADC_START_IMMEDIATE_gc;
            uint16_t t = 0;
            while (!(ADC0.INTFLAGS & ADC_RESRDY_bm) && ++t) {
                ;
            }
            buf[i] = (uint8_t)ADC0.RESULT;
        }
        ADC0.CTRLA = 0;
        /* clock jitter: CPU loop count per RTC tick (RTC counter runs all the time
         * at 32.768 kHz, see rtc_pit_init – never switched off here) */
        for (uint8_t i = 0; i < 16; i++) {
            uint16_t c0 = RTC.CNT, n = 0;
            while (RTC.CNT == c0 && ++n) {
                ;
            }
            c0 = RTC.CNT;
            n = 0;
            while (RTC.CNT == c0 && ++n) {
                ;
            }
            buf[32 + i] = (uint8_t)n;
        }
        /* pool = CMAC(pool, pool || samples) */
        uint8_t tmp[16];
        aes_cmac(mac, buf, sizeof(buf), tmp);
        for (uint8_t i = 0; i < 16; i++) {
            mac[i] ^= tmp[i];
        }
    }
    memcpy(out, mac, 16);
    adc_init();                                 /* restore measurement configuration */
}

static bool g_fast_ok = false;

void link_set_fast_clock(bool ok)
{
    g_fast_ok = ok;
}

static void fast_clock(bool on)
{
    /* Faster clock for the X25519 computation only (same charge, shorter time).
     * tinyAVR speed grades: 5 MHz from 1.8 V, 10 MHz from 2.7 V, 20 MHz from 4.5 V.
     * 10 MHz (20/2) only when the battery was healthy at boot (VCC from the LDO
     * well above 2.7 V), otherwise 5 MHz (20/4), valid down to the 1.8 V BOD.
     * The rest of the time the MCU runs at 3.33 MHz (20/6, also the reset default). */
    uint8_t div = !on ? CLKCTRL_PDIV_6X_gc : (g_fast_ok ? CLKCTRL_PDIV_2X_gc : CLKCTRL_PDIV_4X_gc);
    ccp_write_io((void *)&CLKCTRL.MCLKCTRLB, div | CLKCTRL_PEN_bm);
}

static void progress(void)
{
    wdt_kick();
}

static void make_keypair(void)
{
    uint8_t seed[16];
    uint8_t blk[16];
    const volatile uint8_t *sn = &SIGROW.SERNUM0;

    entropy_fill(seed);
    for (uint8_t i = 0; i < 10; i++) {
        seed[i] ^= sn[i];
    }
    memset(blk, 0, 16);
    blk[0] = 1;
    aes128_encrypt(seed, blk, g_priv);
    blk[0] = 2;
    aes128_encrypt(seed, blk, g_priv + 16);
    blk[0] = 3;
    aes128_encrypt(seed, blk, g_st.pool);        /* new pool for the next boot */
    store_save();

    x25519_progress = progress;
    fast_clock(true);
    x25519_base(g_pub, g_priv);
    fast_clock(false);
    g_keypair_ready = true;
}

/* ------------------------------------------------------------------ API */
bool link_init(uint16_t node_id)
{
    g_node_id = node_id;
    eeprom_read_block(&g_st, &ee_store, sizeof(g_st));
    if (g_st.magic != STORE_MAGIC || g_st.crc != crc16((const uint8_t *)&g_st, sizeof(g_st) - 2)) {
        memset(&g_st, 0, sizeof(g_st));
    }
    /* resume the frame counter above everything that may have been used */
    g_ctr = g_st.ctr_base;
    g_st.ctr_base = g_ctr + CTR_RESERVE;
    store_save();
    return g_st.paired == 1;
}

bool link_paired(void)
{
    return g_st.paired == 1;
}

link_pair_t link_pair_attempt(void)
{
    uint8_t req[PKT_PAIR_REQ_LEN];
    uint8_t ack[40];

    if (!g_keypair_ready) {
        make_keypair();
    }
    req[0] = PKT_PAIR_REQ;
    req[1] = (uint8_t)g_node_id;
    req[2] = (uint8_t)(g_node_id >> 8);
    memcpy(&req[3], g_pub, 32);
    req[35] = link_paired() ? PAIR_REQ_PAIRED : 0U;

    wdt_kick();
    int8_t n = cc1101_send(req, sizeof(req), ack, sizeof(ack), PAIR_RX_WINDOW_MS);
    if (n <= 0) {
        return LINK_PAIR_NONE;
    }

    if (link_paired()) {
        /* only our own hub (holder of the current key) may release the node */
        if (!proto_check_unpair(ack, (uint8_t)n, g_st.k_mac, g_node_id, g_pub)) {
            return LINK_PAIR_NONE;
        }
        g_st.paired = 0;
        memset(g_st.k_enc, 0, sizeof(g_st.k_enc));
        memset(g_st.k_mac, 0, sizeof(g_st.k_mac));
        store_save();
        return LINK_PAIR_UNPAIRED;
    }

    if (n != (int8_t)PKT_PAIR_LEN || ack[0] != PKT_PAIR_ACK ||
        ack[1] != (uint8_t)g_node_id || ack[2] != (uint8_t)(g_node_id >> 8)) {
        return LINK_PAIR_NONE;
    }

    uint8_t s[32];
    x25519_progress = progress;
    fast_clock(true);
    x25519(s, g_priv, &ack[3]);
    fast_clock(false);

    if (!proto_derive_keys(s, g_st.k_enc, g_st.k_mac)) {
        return LINK_PAIR_NONE;
    }
    g_st.paired = 1;
    store_save();
    return LINK_PAIR_PAIRED;
}

static uint8_t g_last[PKT_DATA_LEN];
static bool g_last_ok = false;

bool link_send_data(const telemetry_t *t)
{
    uint8_t *p = g_last;
    _Static_assert(PKT_DATA_LEN == PROTO_DATA_LEN && sizeof(telemetry_t) == PROTO_TELEMETRY_LEN,
                   "config.h and proto.h disagree on the DATA layout");
    if (!link_paired()) {
        return false;
    }
    if (g_ctr >= g_st.ctr_base) {               /* reserve the next block of counters */
        g_st.ctr_base = g_ctr + CTR_RESERVE;
        store_save();
    }
    uint32_t ctr = g_ctr++;

    proto_build_data(p, g_st.k_enc, g_st.k_mac, g_node_id, ctr, (const uint8_t *)t);
    g_last_ok = true;

    return cc1101_send(p, PKT_DATA_LEN, NULL, 0, 0) >= 0;
}

bool link_resend_last(void)
{
    if (!g_last_ok) {
        return false;
    }
    return cc1101_send(g_last, PKT_DATA_LEN, NULL, 0, 0) >= 0;
}
