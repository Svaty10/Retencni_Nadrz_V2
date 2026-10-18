/**
 * @file sht40.c
 * @brief Deterministic SHT40 driver on TWI0 with timeouts and bus recovery.
 */

#include "sht40.h"
#include <util/delay.h>

#define TWI_TIMEOUT_LOOPS   20000UL
#define TWI_BAUD_100K       ((uint8_t)((F_CPU / (2UL * 100000UL)) - 5UL))

uint8_t sht40_calc_crc(const uint8_t *data, uint8_t len)
{
    uint8_t crc = 0xFF;
    for (uint8_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (uint8_t bit = 0; bit < 8; bit++) {
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
        }
    }
    return crc;
}

static void twi_enable(void)
{
    PORTB.DIRCLR = PIN_I2C_SCL | PIN_I2C_SDA;
    PORTB.PIN0CTRL = PORT_PULLUPEN_bm;
    PORTB.PIN1CTRL = PORT_PULLUPEN_bm;
    TWI0.MBAUD = TWI_BAUD_100K;
    TWI0.MCTRLA = TWI_ENABLE_bm;
    TWI0.MSTATUS = TWI_BUSSTATE_IDLE_gc;
}

static void twi_disable(void)
{
    TWI0.MCTRLA = 0;
}

/** Clock out up to 9 pulses on SCL to release a slave holding SDA, then STOP. */
static void twi_bus_recover(void)
{
    TWI0.MCTRLA = 0;
    PORTB.OUTSET = PIN_I2C_SCL;
    PORTB.DIRSET = PIN_I2C_SCL;
    for (uint8_t i = 0; i < 9; i++) {
        PORTB.OUTCLR = PIN_I2C_SCL;
        _delay_us(5);
        PORTB.OUTSET = PIN_I2C_SCL;
        _delay_us(5);
    }
    /* STOP: SDA low -> high while SCL high */
    PORTB.OUTCLR = PIN_I2C_SDA;
    PORTB.DIRSET = PIN_I2C_SDA;
    _delay_us(5);
    PORTB.DIRCLR = PIN_I2C_SDA;
    _delay_us(5);
    PORTB.DIRCLR = PIN_I2C_SCL;
}

static bool twi_wait(uint8_t flag)
{
    uint32_t t = TWI_TIMEOUT_LOOPS;
    while (!(TWI0.MSTATUS & flag)) {
        if (--t == 0) {
            return false;
        }
    }
    return !(TWI0.MSTATUS & (TWI_ARBLOST_bm | TWI_BUSERR_bm));
}

static bool twi_write_cmd(uint8_t cmd)
{
    TWI0.MADDR = (uint8_t)(SHT40_I2C_ADDR << 1);
    if (!twi_wait(TWI_WIF_bm) || (TWI0.MSTATUS & TWI_RXACK_bm)) {
        return false;
    }
    TWI0.MDATA = cmd;
    if (!twi_wait(TWI_WIF_bm) || (TWI0.MSTATUS & TWI_RXACK_bm)) {
        return false;
    }
    TWI0.MCTRLB = TWI_MCMD_STOP_gc;
    return true;
}

static bool twi_read6(uint8_t *buf)
{
    TWI0.MADDR = (uint8_t)((SHT40_I2C_ADDR << 1) | 0x01);
    for (uint8_t i = 0; i < 6; i++) {
        if (!twi_wait(TWI_RIF_bm)) {
            return false;
        }
        if (i == 0 && (TWI0.MSTATUS & TWI_RXACK_bm)) {
            return false;                       /* address NACK (still converting) */
        }
        buf[i] = TWI0.MDATA;
        TWI0.MCTRLB = (i == 5) ? (TWI_ACKACT_NACK_gc | TWI_MCMD_STOP_gc)
                               : (TWI_ACKACT_ACK_gc | TWI_MCMD_RECVTRANS_gc);
    }
    return true;
}

/** Send a command, wait, read the 6-byte response and verify both CRCs. */
static bool sht40_transaction(uint8_t cmd, uint8_t wait_ms, uint8_t *buf)
{
    bool ok;

    twi_enable();
    ok = twi_write_cmd(cmd);
    if (ok) {
        for (uint8_t i = 0; i < wait_ms; i++) {
            _delay_ms(1);
        }
        ok = twi_read6(buf);
    }
    if (!ok) {
        TWI0.MCTRLB = TWI_MCMD_STOP_gc;
        twi_bus_recover();
    }
    twi_disable();

    if (!ok) {
        return false;
    }
    return (sht40_calc_crc(&buf[0], 2) == buf[2]) && (sht40_calc_crc(&buf[3], 2) == buf[5]);
}

bool sht40_heater_pulse(void)
{
    bool ok;

    twi_enable();
    ok = twi_write_cmd(SHT40_CMD_HEATER_110MW_1S);
    if (!ok) {
        TWI0.MCTRLB = TWI_MCMD_STOP_gc;
        twi_bus_recover();
    }
    twi_disable();
    return ok;          /* the measurement taken at the end of the pulse is not read */
}

bool sht40_read_measurements(int16_t *temp_x100, uint16_t *hum_x100)
{
    uint8_t rx[6];
    if (!sht40_transaction(SHT40_CMD_MEASURE_HIGH_PREC, 10, rx)) {
        return false;
    }

    uint16_t raw_t = ((uint16_t)rx[0] << 8) | rx[1];
    uint16_t raw_h = ((uint16_t)rx[3] << 8) | rx[4];

    /* T = -45 + 175 * raw / 65535 ; RH = -6 + 125 * raw / 65535 */
    int32_t t = -4500L + (int32_t)((17500UL * raw_t) / 65535UL);
    int32_t h = -600L + (int32_t)((12500UL * raw_h) / 65535UL);
    if (h < 0) {
        h = 0;
    } else if (h > 10000) {
        h = 10000;
    }
    *temp_x100 = (int16_t)t;
    *hum_x100 = (uint16_t)h;
    return true;
}
