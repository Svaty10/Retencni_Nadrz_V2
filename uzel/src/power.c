/**
 * @file power.c
 * @brief Ultra-low-power management for the ATtiny1624.
 *
 * Sleep budget (power-down, RTC PIT + WDT on OSCULP32K, CC1101 in SLEEP,
 * SHT40 idle, all switched loads off): ~1-1.5 uA.
 */

#include "power.h"
#include <avr/interrupt.h>
#include <avr/sleep.h>
#include <avr/wdt.h>
#include <avr/cpufunc.h>

static volatile uint16_t g_pit_seconds = 0;

ISR(RTC_PIT_vect)
{
    RTC.PITINTFLAGS = RTC_PI_bm;
    g_pit_seconds++;
}

void power_init(void)
{
    /* CPU clock: 20 MHz / 6 = 3.333 MHz */
    ccp_write_io((void *)&CLKCTRL.MCLKCTRLB, CLKCTRL_PDIV_6X_gc | CLKCTRL_PEN_bm);

    /* BOD: active mode comes from the BODCFG fuse, disable it in sleep
     * (saves ~20 uA; only the SLEEP bits are writable at run time). */
    ccp_write_io((void *)&BOD.CTRLA, (uint8_t)((BOD.CTRLA & ~BOD_SLEEP_gm) | BOD_SLEEP_DIS_gc));

    SLPCTRL.CTRLA = SLPCTRL_SMODE_PDOWN_gc | SLPCTRL_SEN_bm;

    /* PORT A: SPI master pins, CSn high, battery switch off */
    PORTA.DIRSET = PIN_SPI_MOSI | PIN_SPI_SCK | PIN_SPI_CSN | PIN_EN_BAT_MEASURE;
    PORTA.DIRCLR = PIN_SPI_MISO | PIN_ADC_BAT | PIN_ADC_PROBE;
    PORTA.OUTSET = PIN_SPI_CSN;
    PORTA.OUTCLR = PIN_SPI_MOSI | PIN_SPI_SCK | PIN_EN_BAT_MEASURE;

    /* Analog inputs: digital buffers permanently off (no leakage at mid-level) */
    PORTA.PIN5CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTA.PIN6CTRL = PORT_ISC_INPUT_DISABLE_gc;
    /* MISO floats while CC1101 sleeps -> buffer off, re-enabled by the SPI driver */
    PORTA.PIN2CTRL = PORT_ISC_INPUT_DISABLE_gc;

    /* PORT B: boost off, I2C idle high with pull-ups, GDO0 buffer off in sleep */
    PORTB.DIRSET = PIN_EN_5V_PROBE;
    PORTB.OUTCLR = PIN_EN_5V_PROBE;
    PORTB.DIRCLR = PIN_I2C_SCL | PIN_I2C_SDA | PIN_RF_GDO0;
    PORTB.PIN0CTRL = PORT_PULLUPEN_bm;
    PORTB.PIN1CTRL = PORT_PULLUPEN_bm;
    PORTB.PIN2CTRL = PORT_ISC_INPUT_DISABLE_gc;

    sei();
}

void wdt_init(void)
{
    ccp_write_io((void *)&WDT.CTRLA, WDT_PERIOD_8KCLK_gc);
}

void wdt_kick(void)
{
    wdt_reset();
}

void rtc_pit_init(void)
{
    RTC.CLKSEL = RTC_CLKSEL_INT32K_gc;
    while (RTC.PITSTATUS & RTC_CTRLBUSY_bm) {
        ;
    }
    RTC.PITCTRLA = RTC_PERIOD_CYC32768_gc | RTC_PITEN_bm;   /* 1.000 s */
    RTC.PITINTCTRL = RTC_PI_bm;
}

void power_sleep_seconds(uint16_t seconds)
{
    cli();
    g_pit_seconds = 0;
    sei();

    for (;;) {
        cli();
        uint16_t elapsed = g_pit_seconds;
        if (elapsed >= seconds) {
            sei();
            break;
        }
        /* Atomic "enable interrupts and sleep": SEI executes the next
         * instruction (SLEEP) before any pending interrupt is serviced. */
        sei();
        sleep_cpu();
        wdt_kick();
    }
}

void power_off_forever(void)
{
    cli();
    PORTB.OUTCLR = PIN_EN_5V_PROBE;          /* all switched loads off */
    PORTA.OUTCLR = PIN_EN_BAT_MEASURE;
    RTC.PITINTCTRL = 0;                      /* no periodic wake-up */
    RTC.PITCTRLA = 0;
    while (WDT.STATUS & WDT_SYNCBUSY_bm) {
        ;
    }
    ccp_write_io((void *)&WDT.CTRLA, 0);     /* watchdog off (not locked by fuse) */
    SLPCTRL.CTRLA = SLPCTRL_SMODE_PDOWN_gc | SLPCTRL_SEN_bm;
    for (;;) {
        sleep_cpu();                         /* interrupts disabled: only a reset wakes us */
    }
}

uint8_t power_take_reset_flags(void)
{
    uint8_t f = RSTCTRL.RSTFR;
    RSTCTRL.RSTFR = f;          /* write 1 to clear */
    return f;
}

uint16_t power_node_id(void)
{
    const volatile uint8_t *sn = &SIGROW.SERNUM0;
    uint16_t crc = 0xFFFF;
    for (uint8_t i = 0; i < 10; i++) {
        crc ^= (uint16_t)sn[i] << 8;
        for (uint8_t b = 0; b < 8; b++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    if (crc == 0x0000 || crc == 0xFFFF) {
        crc = 0x5A5A;
    }
    return crc;
}
