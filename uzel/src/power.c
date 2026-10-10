/**
 * @file power.c
 * @brief Ultra-low-power management implementation for ATtiny1624.
 * @author Matej
 * @date 2026-10-10
 */

#include "power.h"
#include <avr/interrupt.h>
#include <avr/sleep.h>
#include <avr/wdt.h>
#include <avr/cpufunc.h>

/* Volatile counter for PIT wakeups (seconds) */
static volatile uint16_t g_pit_seconds = 0;

/**
 * @brief RTC PIT Interrupt Service Routine.
 * Triggered every 1 second by the internal 32.768 kHz ULP oscillator.
 */
ISR(RTC_PIT_vect)
{
    /* Clear interrupt flag by writing 1 */
    RTC.PITINTFLAGS = RTC_PI_bm;
    g_pit_seconds++;
}

void power_init(void)
{
    /* 1. Set main clock prescaler to 6x -> F_CPU = 20 MHz / 6 = 3.333 MHz */
    ccp_write_io((uint8_t *)&CLKCTRL.MCLKCTRLB, (CLKCTRL_PDIV_6X_gc | CLKCTRL_PEN_bm));

    /* 2. Configure Brown-Out Detector (BOD):
     * Active Mode: ENABLED (prevents flash corruption at low voltage)
     * Sleep Mode: DISABLED (saves ~20-30 uA, essential for < 1.5 uA sleep budget)
     */
    ccp_write_io((uint8_t *)&BOD.CTRLA, (BOD_SLEEP_DIS_gc | BOD_ACTIVE_ENABLED_gc));

    /* 3. Configure Sleep Controller: Power-down mode */
    SLPCTRL.CTRLA = SLPCTRL_SMODE_PDOWN_gc | SLPCTRL_SEN_bm;

    /* 4. Configure Initial Pin Directions and States */
    /* PORT A */
    PORTA.DIRSET = PIN_SPI_MOSI | PIN_SPI_SCK | PIN_SPI_CSN | PIN_EN_BAT_MEASURE;
    PORTA.DIRCLR = PIN_SPI_MISO | PIN_ADC_BAT | PIN_ADC_PROBE | PIN_UPDI_PROG;

    /* Safe default outputs */
    PORTA.OUTSET = PIN_SPI_CSN;             /* CC1101 CSN inactive (HIGH) */
    PORTA.OUTCLR = PIN_SPI_MOSI | PIN_SPI_SCK | PIN_EN_BAT_MEASURE; /* Battery divider off */

    /* Disable digital input buffers on analog pins to prevent intermediate-level leakage */
    PORTA.PIN5CTRL = PORT_ISC_INPUT_DISABLE_gc; /* ADC_BAT */
    PORTA.PIN6CTRL = PORT_ISC_INPUT_DISABLE_gc; /* ADC_PROBE */

    /* PORT B */
    PORTB.DIRSET = PIN_EN_5V_PROBE;
    PORTB.DIRCLR = PIN_I2C_SCL | PIN_I2C_SDA | PIN_RF_GDO0;

    /* 5V boost off by default */
    PORTB.OUTCLR = PIN_EN_5V_PROBE;

    /* Enable global interrupts */
    sei();
}

void wdt_init(void)
{
    /* Configure Watchdog for ~8 seconds timeout (ULP 1K oscillator) */
    ccp_write_io((uint8_t *)&WDT.CTRLA, WDT_PERIOD_8KCLK_gc);
}

void wdt_kick(void)
{
    wdt_reset();
}

void wdt_disable(void)
{
    ccp_write_io((uint8_t *)&WDT.CTRLA, 0);
}

void rtc_pit_init(void)
{
    /* Select internal 32.768 kHz ultra-low-power oscillator */
    RTC.CLKSEL = RTC_CLKSEL_INT32K_gc;

    /* Wait for RTC synchronization */
    while (RTC.PITSTATUS & RTC_CTRLABUSY_bm) {
        ;
    }

    /* Configure PIT for 1-second period: 32768 cycles / 32.768 kHz = 1.000 s */
    RTC.PITCTRLA = RTC_PERIOD_CYC32768_gc | RTC_PITEN_bm;

    /* Enable Periodic Interrupt */
    RTC.PITINTCTRL = RTC_PI_bm;
}

void power_prepare_pins_for_sleep(void)
{
    /* Assure all high-power loads are powered down */
    PORTA.OUTCLR = PIN_EN_BAT_MEASURE;
    PORTB.OUTCLR = PIN_EN_5V_PROBE;

    /* Ensure CC1101 Chip Select remains high during sleep */
    PORTA.OUTSET = PIN_SPI_CSN;

    /* Disable digital input buffer on RF_GDO0 to avoid floating gate leakage */
    PORTB.PIN2CTRL = PORT_ISC_INPUT_DISABLE_gc;
}

void power_restore_pins_after_wakeup(void)
{
    /* Restore digital input capability on RF_GDO0 */
    PORTB.PIN2CTRL = PORT_ISC_BOTHEDGES_gc;
}

void power_enter_sleep(void)
{
    power_prepare_pins_for_sleep();

    /* Put MCU into sleep mode */
    sleep_cpu();

    power_restore_pins_after_wakeup();
}

bool power_interval_elapsed(void)
{
    cli();
    uint16_t secs = g_pit_seconds;
    sei();

    return (secs >= SLEEP_INTERVAL_SECONDS);
}

void power_reset_interval_counter(void)
{
    cli();
    g_pit_seconds = 0;
    sei();
}

void power_enter_permanent_sleep(void)
{
    /* 1. Disable Watchdog to avoid unwanted resets during deep sleep */
    wdt_disable();

    /* 2. Disable RTC interrupt to eliminate future wakeups */
    RTC.PITINTCTRL = 0;
    RTC.PITCTRLA = 0;

    /* 3. Turn off all switched power domains */
    PORTA.OUTCLR = PIN_EN_BAT_MEASURE;
    PORTB.OUTCLR = PIN_EN_5V_PROBE;
    PORTA.OUTSET = PIN_SPI_CSN;

    /* 4. Disable all digital input buffers across all pins for absolute minimum quiescent draw */
    PORTA.PIN0CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTA.PIN1CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTA.PIN2CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTA.PIN3CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTA.PIN4CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTA.PIN5CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTA.PIN6CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTA.PIN7CTRL = PORT_ISC_INPUT_DISABLE_gc;

    PORTB.PIN0CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTB.PIN1CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTB.PIN2CTRL = PORT_ISC_INPUT_DISABLE_gc;
    PORTB.PIN3CTRL = PORT_ISC_INPUT_DISABLE_gc;

    /* 5. Disable Brown-out detector in sleep */
    ccp_write_io((uint8_t *)&BOD.CTRLA, BOD_SLEEP_DIS_gc);

    /* 6. Enter Power-down mode and halt forever */
    SLPCTRL.CTRLA = SLPCTRL_SMODE_PDOWN_gc | SLPCTRL_SEN_bm;

    cli(); /* Disable interrupts completely */
    while (1) {
        sleep_cpu();
    }
}
