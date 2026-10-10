/**
 * @file sht40.c
 * @brief Deterministic, non-blocking Sensirion SHT40 TWI0 driver for ATtiny1624.
 * @author Matej
 * @date 2026-10-10
 */

#include "sht40.h"
#include <util/delay.h>

#define TWI_TIMEOUT_CYCLES  20000UL

/**
 * @brief Sensirion 8-bit CRC calculation (Polynomial 0x31: x^8 + x^5 + x^4 + 1).
 */
uint8_t sht40_calc_crc(const uint8_t *data, uint8_t len)
{
    uint8_t crc = 0xFF;
    for (uint8_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (uint8_t bit = 8; bit > 0; --bit) {
            if (crc & 0x80) {
                crc = (crc << 1) ^ 0x31;
            } else {
                crc = (crc << 1);
            }
        }
    }
    return crc;
}

void sht40_bus_recover(void)
{
    /* 1. Disable TWI peripheral */
    TWI0.MCTRLA = 0;

    /* 2. Configure SCL (PB0) as output and SDA (PB1) as input pull-up */
    PORTB.PIN0CTRL = PORT_PULLUPEN_bm;
    PORTB.PIN1CTRL = PORT_PULLUPEN_bm;
    PORTB.DIRSET = PIN_I2C_SCL;
    PORTB.DIRCLR = PIN_I2C_SDA;

    /* 3. Clock SCL 9 times to release any slave stuck in ACK/DATA phase */
    for (uint8_t i = 0; i < 9; i++) {
        PORTB.OUTCLR = PIN_I2C_SCL;
        _delay_us(5);
        PORTB.OUTSET = PIN_I2C_SCL;
        _delay_us(5);
    }

    /* 4. Generate STOP condition */
    PORTB.DIRSET = PIN_I2C_SDA;
    PORTB.OUTCLR = PIN_I2C_SDA;
    _delay_us(5);
    PORTB.OUTSET = PIN_I2C_SCL;
    _delay_us(5);
    PORTB.OUTSET = PIN_I2C_SDA;
    _delay_us(5);

    /* 5. Reinitialize TWI controller */
    sht40_init();
}

void sht40_init(void)
{
    /* PB0 (SCL) and PB1 (SDA) input mode with pull-ups for I2C */
    PORTB.DIRCLR = PIN_I2C_SCL | PIN_I2C_SDA;
    PORTB.PIN0CTRL = PORT_PULLUPEN_bm;
    PORTB.PIN1CTRL = PORT_PULLUPEN_bm;

    /* Baudrate: (3.333 MHz / (2 * 100 kHz)) - 5 = 16.6 - 5 = 11 */
    TWI0.MBAUD = 11;

    /* Enable Master mode */
    TWI0.MCTRLA = TWI_ENABLE_bm;

    /* Force bus state to IDLE */
    TWI0.MSTATUS = TWI_BUSSTATE_IDLE_gc;
}

void sht40_deinit(void)
{
    /* Disable TWI to cut off internal bias currents during deep sleep */
    TWI0.MCTRLA = 0;
}

static bool twi_wait_flag(uint8_t flag)
{
    uint32_t timeout = TWI_TIMEOUT_CYCLES;
    while (!(TWI0.MSTATUS & flag)) {
        if (--timeout == 0) {
            return false;
        }
    }
    return true;
}

static bool twi_start_write(uint8_t address)
{
    /* Clear flags and initiate START with SLA+W */
    TWI0.MADDR = (address << 1) | 0x00;

    if (!twi_wait_flag(TWI_WIF_bm)) {
        return false;
    }

    /* Check for ACK and arbitration errors */
    if (TWI0.MSTATUS & (TWI_RXACK_bm | TWI_ARBLOST_bm | TWI_BUSERR_bm)) {
        return false;
    }

    return true;
}

static bool twi_start_read(uint8_t address)
{
    /* Initiate START with SLA+R */
    TWI0.MADDR = (address << 1) | 0x01;

    if (!twi_wait_flag(TWI_RIF_bm)) {
        return false;
    }

    /* Check for ACK and arbitration errors */
    if (TWI0.MSTATUS & (TWI_RXACK_bm | TWI_ARBLOST_bm | TWI_BUSERR_bm)) {
        return false;
    }

    return true;
}

static bool twi_write_byte(uint8_t data)
{
    TWI0.MDATA = data;

    if (!twi_wait_flag(TWI_WIF_bm)) {
        return false;
    }

    if (TWI0.MSTATUS & (TWI_RXACK_bm | TWI_ARBLOST_bm | TWI_BUSERR_bm)) {
        return false;
    }

    return true;
}

static bool twi_read_byte(uint8_t *data, bool is_last)
{
    if (!twi_wait_flag(TWI_RIF_bm)) {
        return false;
    }

    *data = TWI0.MDATA;

    if (is_last) {
        /* NACK + STOP */
        TWI0.MCTRLB = TWI_ACKACT_NACK_gc | TWI_MCMD_STOP_gc;
    } else {
        /* ACK + prepare next byte */
        TWI0.MCTRLB = TWI_ACKACT_ACK_gc | TWI_MCMD_RECVTRANS_gc;
    }

    return true;
}

static void twi_stop(void)
{
    TWI0.MCTRLB = TWI_MCMD_STOP_gc;
}

bool sht40_read_measurements(int16_t *temp_c_x10, uint16_t *hum_rh_x10)
{
    if (temp_c_x10 == NULL || hum_rh_x10 == NULL) {
        return false;
    }

    sht40_init();

    /* 1. Send High-Precision Measurement command (0xFD) */
    if (!twi_start_write(SHT40_I2C_ADDR)) {
        twi_stop();
        sht40_bus_recover();
        sht40_deinit();
        return false;
    }

    if (!twi_write_byte(SHT40_CMD_MEASURE_HIGH_PREC)) {
        twi_stop();
        sht40_bus_recover();
        sht40_deinit();
        return false;
    }

    twi_stop();

    /* 2. Wait for measurement conversion (Max 8.2 ms according to SHT40 datasheet) */
    _delay_ms(9);

    /* 3. Read 6 response bytes: [T_MSB, T_LSB, T_CRC, RH_MSB, RH_LSB, RH_CRC] */
    if (!twi_start_read(SHT40_I2C_ADDR)) {
        twi_stop();
        sht40_bus_recover();
        sht40_deinit();
        return false;
    }

    uint8_t rx_buf[6];
    for (uint8_t i = 0; i < 6; i++) {
        if (!twi_read_byte(&rx_buf[i], (i == 5))) {
            twi_stop();
            sht40_bus_recover();
            sht40_deinit();
            return false;
        }
    }

    /* Shut down TWI peripheral to save power */
    sht40_deinit();

    /* 4. Validate CRC on Temperature and Humidity packets */
    if (sht40_calc_crc(&rx_buf[0], 2) != rx_buf[2]) {
        return false; /* Temperature CRC mismatch */
    }

    if (sht40_calc_crc(&rx_buf[3], 2) != rx_buf[5]) {
        return false; /* Humidity CRC mismatch */
    }

    /* 5. Calculate physical values using integer arithmetic without precision loss */
    uint16_t raw_temp = ((uint16_t)rx_buf[0] << 8) | rx_buf[1];
    uint16_t raw_hum  = ((uint16_t)rx_buf[3] << 8) | rx_buf[4];

    /*
     * Sensirion formula:
     * T [deg C] = -45 + 175 * (raw_temp / 65535)
     * T_x10 [0.1 deg C] = -450 + (1750 * raw_temp) / 65535
     */
    int32_t t_calc = -450L + (int32_t)(((uint32_t)1750UL * raw_temp) / 65535UL);
    *temp_c_x10 = (int16_t)t_calc;

    /*
     * RH [%] = -6 + 125 * (raw_hum / 65535)
     * RH_x10 [0.1 %] = -60 + (1250 * raw_hum) / 65535
     */
    int32_t rh_calc = -60L + (int32_t)(((uint32_t)1250UL * raw_hum) / 65535UL);
    if (rh_calc < 0) {
        rh_calc = 0;
    } else if (rh_calc > 1000) {
        rh_calc = 1000;
    }
    *hum_rh_x10 = (uint16_t)rh_calc;

    return true;
}
