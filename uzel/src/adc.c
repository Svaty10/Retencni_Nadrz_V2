/**
 * @file adc.c
 * @brief Deterministic, oversampled ADC0 implementation for ATtiny1624.
 * @author Matej
 * @date 2026-10-10
 */

#include "adc.h"
#include <util/delay.h>

/* Maximum loop iterations before timing out on an ADC conversion */
#define ADC_TIMEOUT_CYCLES  30000UL

void adc_init(void)
{
    /* Keep ADC disabled until an active measurement sequence begins */
    ADC0.CTRLA = 0;

    /* Set clock prescaler to DIV4 (3.333 MHz / 4 = ~833 kHz ADC clock) */
    ADC0.CTRLB = ADC_PRESC_DIV4_gc;

    /* Sample duration: extend sampling period by 32 ADC clock cycles
     * to properly charge ADC S/H capacitance through high impedance RC divider (180k / 68k) */
    ADC0.CTRLE = 32;

    /* Enable 16x sample accumulation for noise filtering and increased resolution */
    ADC0.CTRLF = ADC_SAMPNUM_ACC16_gc;
}

static bool adc_convert_channel(uint8_t muxpos, uint16_t *result)
{
    /* Select input channel */
    ADC0.MUXPOS = muxpos;

    /* Clear any pending result flag */
    ADC0.INTFLAGS = ADC_RESRDY_bm;

    /* Start conversion */
    ADC0.COMMAND = ADC_STCONV_bm;

    /* Wait for result ready with software timeout to prevent deadlocks */
    uint32_t timeout = ADC_TIMEOUT_CYCLES;
    while (!(ADC0.INTFLAGS & ADC_RESRDY_bm)) {
        if (--timeout == 0) {
            return false; /* Timeout error */
        }
    }

    /* Read 16-bit accumulated result */
    *result = ADC0.RES;

    /* Clear interrupt flag by writing 1 */
    ADC0.INTFLAGS = ADC_RESRDY_bm;

    return true;
}

bool adc_measure_battery(uint16_t *vbat_mv)
{
    if (vbat_mv == NULL) {
        return false;
    }

    /* 1. Set internal reference to 1.024 V */
    VREF.ADC0REF = VREF_REFSEL_1V024_gc;

    /* 2. Switch on battery measurement high-side P-MOSFET via Q3 (PA7 HIGH) */
    PORTA.OUTSET = PIN_EN_BAT_MEASURE;

    /* 3. Hardware settling delay for RC divider (Tau ~ 3.96 ms, wait 20 ms ~ 5*Tau) */
    _delay_ms(BAT_DIVIDER_SETTLE_MS);

    /* 4. Enable ADC */
    ADC0.CTRLA = ADC_ENABLE_bm;

    /* 5. Perform dummy conversion to flush reference settling transient */
    uint16_t dummy = 0;
    (void)adc_convert_channel(ADC_MUXPOS_AIN5_gc, &dummy);

    /* 6. Perform actual 16x oversampled conversion on AIN5 (PA5) */
    uint16_t raw_acc = 0;
    bool success = adc_convert_channel(ADC_MUXPOS_AIN5_gc, &raw_acc);

    /* 7. Disable ADC and immediately disconnect divider to conserve battery */
    ADC0.CTRLA = 0;
    PORTA.OUTCLR = PIN_EN_BAT_MEASURE;

    if (!success) {
        return false;
    }

    /*
     * Calculation derivation:
     * - Resistors: R_top = 1000 kOhm (1%), R_bot = 220 kOhm (1%)
     * - Divider ratio: V_pin = V_bat * (220 / (1000 + 220)) = V_bat * (11 / 61)
     * - V_REF = 1024 mV
     * - 12-bit ADC full-scale = 4096 counts
     * - 16x Accumulation: raw_acc = 16 * count_single
     * - count_single = raw_acc / 16
     * - V_pin_mv = (count_single * 1024) / 4096 = count_single / 4 = raw_acc / 64
     * - V_bat_mv = V_pin_mv * (61 / 11) = (raw_acc * 61) / (64 * 11) = (raw_acc * 61) / 704
     */
    uint32_t calc = ((uint32_t)raw_acc * 61UL) / 704UL;
    *vbat_mv = (uint16_t)calc;

    return true;
}

bool adc_measure_probe(uint16_t *probe_raw_acc, uint16_t *probe_mv)
{
    if (probe_raw_acc == NULL || probe_mv == NULL) {
        return false;
    }

    /* 1. Set internal reference to 2.500 V */
    VREF.ADC0REF = VREF_REFSEL_2V500_gc;

    /* 2. Turn on 5V boost regulator (TPS63900 via PB3 HIGH) */
    PORTB.OUTSET = PIN_EN_5V_PROBE;

    /* 3. Hardware settling delay: TPS63900 startup + probe internal circuitry stabilization */
    _delay_ms(PROBE_SETTLE_MS);

    /* 4. Enable ADC */
    ADC0.CTRLA = ADC_ENABLE_bm;

    /* 5. Dummy conversion to stabilize VREF switched state */
    uint16_t dummy = 0;
    (void)adc_convert_channel(ADC_MUXPOS_AIN6_gc, &dummy);

    /* 6. Perform 16x oversampled conversion on AIN6 (PA6) */
    uint16_t raw_acc = 0;
    bool success = adc_convert_channel(ADC_MUXPOS_AIN6_gc, &raw_acc);

    /* 7. Immediately shut down 5V regulator and ADC to minimize peak current duration */
    ADC0.CTRLA = 0;
    PORTB.OUTCLR = PIN_EN_5V_PROBE;

    if (!success) {
        return false;
    }

    *probe_raw_acc = raw_acc;

    /*
     * Calculation derivation:
     * - Probe output divider: R_top = 100 kOhm (1%), R_bot = 220 kOhm (1%)
     * - Divider ratio: V_pin = V_probe * (220 / (100 + 220)) = V_probe * (11 / 16)
     * - V_REF = 2500 mV
     * - 12-bit ADC full-scale = 4096 counts
     * - 16x Accumulation: raw_acc = 16 * count_single
     * - V_pin_mv = (raw_acc * 2500) / (16 * 4096) = (raw_acc * 2500) / 65536
     * - V_probe_mv = V_pin_mv * (16 / 11)
     *             = (raw_acc * 2500 * 16) / (65536 * 11)
     *             = (raw_acc * 2500) / (4096 * 11)
     *             = (raw_acc * 625) / (1024 * 11)
     *             = (raw_acc * 625) / 11264
     */
    uint32_t calc = ((uint32_t)raw_acc * 625UL) / 11264UL;
    *probe_mv = (uint16_t)calc;

    return true;
}

uint16_t adc_probe_mv_to_depth_mm(uint16_t probe_mv,
                                  uint16_t v_zero_mv,
                                  uint16_t v_span_mv,
                                  uint16_t depth_max_mm)
{
    if (probe_mv <= v_zero_mv) {
        return 0;
    }

    uint32_t delta_v = (uint32_t)(probe_mv - v_zero_mv);
    if (delta_v >= v_span_mv) {
        return depth_max_mm;
    }

    uint32_t depth = (delta_v * (uint32_t)depth_max_mm) / (uint32_t)v_span_mv;
    return (uint16_t)depth;
}
