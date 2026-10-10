/**
 * @file adc.h
 * @brief High-precision, oversampled ADC0 driver for Battery and Hydrostatic Probe.
 * @author Matej
 * @date 2026-10-10
 */

#ifndef ADC_H_
#define ADC_H_

#include "config.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Initialize ADC0 peripheral in low-power single-ended mode.
 */
void adc_init(void);

/**
 * @brief Measure battery voltage with hardware settling delay and 16x oversampling.
 * Controls high-side P-MOSFET via PA7 (EN_BAT_MEASURE).
 *
 * @param[out] vbat_mv Pointer to store measured voltage in millivolts.
 * @return true if measurement succeeded, false on timeout.
 */
bool adc_measure_battery(uint16_t *vbat_mv);

/**
 * @brief Measure hydrostatic water level probe with 5V boost enable and 16x oversampling.
 * Controls TPS63900 boost regulator via PB3 (EN_5V_PROBE).
 *
 * @param[out] probe_raw_acc Pointer to store 16x accumulated raw 12-bit ADC value.
 * @param[out] probe_mv Pointer to store calculated probe signal in millivolts (0-5000 mV).
 * @return true if measurement succeeded, false on timeout.
 */
bool adc_measure_probe(uint16_t *probe_raw_acc, uint16_t *probe_mv);

/**
 * @brief Convert accumulated probe reading to water column depth in millimeters.
 *
 * @param probe_mv Measured probe voltage in millivolts.
 * @param v_zero_mv Output voltage at 0 mm water depth (e.g., 500 mV for 0.5-4.5V probe).
 * @param v_span_mv Voltage span for full range (e.g., 4000 mV).
 * @param depth_max_mm Maximum probe depth range (e.g., 2000 mm for a 2-meter tank).
 * @return Calculated depth in millimeters (0 to depth_max_mm).
 */
uint16_t adc_probe_mv_to_depth_mm(uint16_t probe_mv,
                                  uint16_t v_zero_mv,
                                  uint16_t v_span_mv,
                                  uint16_t depth_max_mm);

#ifdef __cplusplus
}
#endif

#endif /* ADC_H_ */
