/**
 * @file sht40.h
 * @brief Sensirion SHT40 Temperature and Humidity sensor driver via TWI0 (I2C).
 * @author Matej
 * @date 2026-10-10
 */

#ifndef SHT40_H_
#define SHT40_H_

#include "config.h"

#ifdef __cplusplus
extern "C" {
#endif

/** SHT40 7-bit I2C Address */
#define SHT40_I2C_ADDR              0x44

/** SHT40 Measurement Commands */
#define SHT40_CMD_MEASURE_HIGH_PREC 0xFD
#define SHT40_CMD_MEASURE_MED_PREC  0xF6
#define SHT40_CMD_MEASURE_LOW_PREC  0xE0
#define SHT40_CMD_READ_SERIAL       0x89
#define SHT40_CMD_SOFT_RESET        0x94

/**
 * @brief Initialize TWI0 master peripheral at 100 kHz with hardware timeouts.
 */
void sht40_init(void);

/**
 * @brief Deinitialize TWI0 to eliminate peripheral leakage in deep sleep.
 */
void sht40_deinit(void);

/**
 * @brief Perform a high-precision measurement on SHT40 with timeout and CRC validation.
 *
 * @param[out] temp_c_x10 Pointer to store temperature in 0.1 deg C (e.g. 215 = 21.5 C).
 * @param[out] hum_rh_x10 Pointer to store relative humidity in 0.1 % RH (e.g. 652 = 65.2 %).
 * @return true if measurement and CRC-8 checks succeeded, false on bus/timeout/CRC error.
 */
bool sht40_read_measurements(int16_t *temp_c_x10, uint16_t *hum_rh_x10);

/**
 * @brief Calculate Sensirion 8-bit CRC.
 *
 * @param data Pointer to input data buffer.
 * @param len Number of bytes.
 * @return Calculated 8-bit CRC.
 */
uint8_t sht40_calc_crc(const uint8_t *data, uint8_t len);

/**
 * @brief Perform TWI hardware/software bus recovery in case SDA line gets held low.
 */
void sht40_bus_recover(void);

#ifdef __cplusplus
}
#endif

#endif /* SHT40_H_ */
