/**
 * @file sht40.h
 * @brief Sensirion SHT40 driver (TWI0, timeouts, bus recovery, CRC-8).
 */

#ifndef SHT40_H_
#define SHT40_H_

#include "config.h"

#define SHT40_I2C_ADDR              0x44
#define SHT40_CMD_MEASURE_HIGH_PREC 0xFD
#define SHT40_CMD_SOFT_RESET        0x94

/**
 * @brief High-precision measurement.
 * @param[out] temp_x100 Temperature [0.01 degC].
 * @param[out] hum_x100  Relative humidity [0.01 %], clamped to 0..10000.
 * @return false on bus error, timeout or CRC mismatch.
 */
bool sht40_read_measurements(int16_t *temp_x100, uint16_t *hum_x100);

uint8_t sht40_calc_crc(const uint8_t *data, uint8_t len);

#endif /* SHT40_H_ */
