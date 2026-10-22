/**
 * @file sht40.h
 * @brief Sensirion SHT40 driver (TWI0, timeouts, bus recovery, CRC-8).
 */

#ifndef SHT40_H_
#define SHT40_H_

#include "config.h"

/* I2C address depends on the variant: SHT40-AD1B = 0x44, SHT40-BD1B = 0x45.
 * The driver tries both and keeps the one that answers. */
#define SHT40_I2C_ADDR_A            0x44
#define SHT40_I2C_ADDR_B            0x45
#define SHT40_CMD_MEASURE_HIGH_PREC 0xFD
#define SHT40_CMD_SOFT_RESET        0x94
#define SHT40_CMD_HEATER_110MW_1S   0x2F    /**< heater 110 mW for 1 s (+ measurement at its end) */

/**
 * @brief High-precision measurement.
 * @param[out] temp_x100 Temperature [0.01 degC].
 * @param[out] hum_x100  Relative humidity [0.01 %], clamped to 0..10000.
 * @return false on bus error, timeout or CRC mismatch.
 */
bool sht40_read_measurements(int16_t *temp_x100, uint16_t *hum_x100);

/**
 * @brief Start one heater pulse (110 mW, 1 s) and return immediately.
 *        The sensor is busy for ~1.1 s – do not use it during that time.
 * @return false if the sensor did not acknowledge the command.
 */
bool sht40_heater_pulse(void);

uint8_t sht40_calc_crc(const uint8_t *data, uint8_t len);

#endif /* SHT40_H_ */
