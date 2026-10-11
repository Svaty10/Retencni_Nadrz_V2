/**
 * @file adc.h
 * @brief Oversampled ADC0 driver (tinyAVR 2-series) for battery and hydrostatic probe.
 */

#ifndef ADC_H_
#define ADC_H_

#include "config.h"

/** Configure ADC0 (kept disabled between measurements). */
void adc_init(void);

/**
 * @brief Measure battery voltage (switches the divider on for the measurement only).
 * @param[out] vbat_mv Battery voltage in mV.
 * @return false on conversion timeout.
 */
bool adc_measure_battery(uint16_t *vbat_mv);

/**
 * @brief Power the probe, wait for settling and take a trimmed mean of
 *        PROBE_BURSTS x 64 samples spread over ~100 ms.
 * @param[out] probe_100uv Probe output voltage in 0.1 mV units.
 * @param[out] clipped     true if any burst hit ADC full scale.
 * @return false on conversion timeout.
 */
bool adc_measure_probe(uint16_t *probe_100uv, bool *clipped);

#endif /* ADC_H_ */
