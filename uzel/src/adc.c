/**
 * @file adc.c
 * @brief Deterministic oversampled ADC0 driver for the ATtiny1624 (tinyAVR 2-series).
 *
 * The 2-series ADC differs from 0/1-series and AVR-Dx parts:
 *  - reference is selected in ADC0.CTRLC (REFSEL) together with TIMEBASE,
 *  - conversions are started via ADC0.COMMAND (MODE + START),
 *  - the accumulated result is a 32-bit ADC0.RESULT.
 *
 * Each "burst" is one BURST-mode conversion of 64 accumulated 12-bit samples
 * (max 64 * 4095 = 262080). We scale bursts to a 16x-oversampled count
 * (sum / 4, max 65520) which keeps all following math in 32 bits.
 */

#include "adc.h"
#include "power.h"
#include <util/delay.h>

#define ADC_TIMEOUT_LOOPS     60000UL      /* >> 64 samples * ~20 us */
#define ADC_TIMEBASE_VAL      ((F_CPU + 999999UL) / 1000000UL)   /* CLK_PER cycles per 1 us */
#define ADC_SAMPDUR_VAL       16U           /* extra sample time for high-Z dividers */
#define BURST_FULLSCALE_X16   65000UL       /* ~99.2 % of full scale -> clip flag */

/* Simple insertion sort for tiny arrays (n <= 16). */
static void sort_u32(uint32_t *a, uint8_t n)
{
    for (uint8_t i = 1; i < n; i++) {
        uint32_t key = a[i];
        int8_t j = (int8_t)(i - 1);
        while (j >= 0 && a[j] > key) {
            a[j + 1] = a[j];
            j--;
        }
        a[j + 1] = key;
    }
}

void adc_init(void)
{
    ADC0.CTRLA = 0;                              /* disabled until needed */
    ADC0.CTRLB = ADC_PRESC_DIV4_gc;              /* 3.333 MHz / 4 = 833 kHz */
    ADC0.CTRLE = ADC_SAMPDUR_VAL;
    ADC0.CTRLF = ADC_SAMPNUM_ACC64_gc;           /* 64 samples per burst */
    ADC0.MUXNEG = 0;
    ADC0.PGACTRL = 0;                            /* PGA off (saves current) */
}

static void adc_start(uint8_t refsel, uint8_t muxpos)
{
    ADC0.CTRLC = refsel | (uint8_t)(ADC_TIMEBASE_VAL << ADC_TIMEBASE_gp);
    ADC0.MUXPOS = muxpos;
    ADC0.CTRLA = ADC_ENABLE_bm;
    _delay_us(100);                              /* internal reference start-up */
}

static void adc_stop(void)
{
    ADC0.CTRLA = 0;
}

/** One burst (64 accumulated samples) -> 16x-oversampled count (0..65520). */
static bool adc_burst_x16(uint32_t *out_x16)
{
    ADC0.INTFLAGS = ADC_RESRDY_bm;
    ADC0.COMMAND = ADC_MODE_BURST_gc | ADC_START_IMMEDIATE_gc;

    uint32_t timeout = ADC_TIMEOUT_LOOPS;
    while (!(ADC0.INTFLAGS & ADC_RESRDY_bm)) {
        if (--timeout == 0) {
            ADC0.COMMAND = 0;                    /* stop */
            return false;
        }
    }
    uint32_t sum = ADC0.RESULT;                  /* reading RESULT clears RESRDY */
    *out_x16 = sum >> 2;                         /* 64 samples -> 16x scale */
    return true;
}

bool adc_measure_battery(uint16_t *vbat_mv)
{
    uint32_t b[4];
    bool ok = true;

    PORTA.OUTSET = PIN_EN_BAT_MEASURE;           /* divider on */
    power_sleep_ms(BAT_SETTLE_MS);               /* MCU sleeps while the divider settles */
    adc_start(ADC_REFSEL_1024MV_gc, ADC_MUXPOS_AIN5_gc);

    uint32_t dummy;
    (void)adc_burst_x16(&dummy);                 /* flush reference transient */
    for (uint8_t i = 0; i < 4 && ok; i++) {
        ok = adc_burst_x16(&b[i]);
    }

    adc_stop();
    PORTA.OUTCLR = PIN_EN_BAT_MEASURE;           /* divider off immediately */

    if (!ok) {
        return false;
    }

    sort_u32(b, 4);
    uint32_t x16 = (b[1] + b[2]) / 2U;           /* median of 4 */

    /* pin_uV = x16 * 1024000 / 65536 = x16 * 125 / 8   (max ~1.02e6)
     * vbat_mV = pin_uV * (RTOP + RBOT) / RBOT / 1000                 */
    uint32_t pin_uv = (x16 * 125UL) / 8UL;
    uint32_t mv = (pin_uv * (BAT_RTOP_K + BAT_RBOT_K)) / (BAT_RBOT_K * 1000UL);
    *vbat_mv = (mv > 65534UL) ? 65534U : (uint16_t)mv;
    return true;
}

bool adc_measure_probe(uint16_t *probe_100uv, bool *clipped)
{
    uint32_t b[PROBE_BURSTS];
    bool ok = true;

    *clipped = false;

    PORTB.OUTSET = PIN_EN_5V_PROBE;              /* 5 V boost on */
    power_sleep_ms(PROBE_SETTLE_MS);             /* MCU sleeps while the probe settles */
    adc_start(ADC_REFSEL_2500MV_gc, ADC_MUXPOS_AIN6_gc);

    uint32_t dummy;
    (void)adc_burst_x16(&dummy);

    for (uint8_t i = 0; i < PROBE_BURSTS && ok; i++) {
        ok = adc_burst_x16(&b[i]);
        if (!ok) {
            break;
        }
        if (b[i] >= BURST_FULLSCALE_X16) {
            *clipped = true;
        }
        if (i + 1U < PROBE_BURSTS) {
            _delay_ms(PROBE_BURST_GAP_MS);
        }
    }

    adc_stop();
    PORTB.OUTCLR = PIN_EN_5V_PROBE;              /* boost off as early as possible */

    if (!ok) {
        return false;
    }

    /* Trimmed mean: drop the lowest and highest (BURSTS - KEEP) / 2 bursts. */
    sort_u32(b, PROBE_BURSTS);
    const uint8_t first = (PROBE_BURSTS - PROBE_TRIM_KEEP) / 2U;
    uint32_t acc = 0;
    for (uint8_t i = 0; i < PROBE_TRIM_KEEP; i++) {
        acc += b[first + i];
    }
    uint32_t x16 = (acc + PROBE_TRIM_KEEP / 2U) / PROBE_TRIM_KEEP;

    /* pin [10 uV] = x16 * 250000 / 65536 = x16 * 15625 / 4096   (max 2.5e5)
     * probe [10 uV] = pin * (RTOP + RBOT) / RBOT
     * probe [0.1 mV] = probe[10 uV] / 10                                  */
    uint32_t pin_10uv = (x16 * 15625UL) / 4096UL;
    uint32_t probe_10uv = (pin_10uv * (PROBE_RTOP_K + PROBE_RBOT_K)) / PROBE_RBOT_K;
    uint32_t v = (probe_10uv + 5UL) / 10UL;
    *probe_100uv = (v > 65534UL) ? 65534U : (uint16_t)v;
    return true;
}
