/**
 * @file test_spanku.c
 * @brief Test firmware for measuring the node's sleep current: it only sleeps.
 *
 * Same state as between two cycles of the real firmware (power.c, cc1101.c are
 * shared): pins as in power_init(), CC1101 in SLEEP, SHT40 idle, boost and battery
 * divider off, MCU in power-down with the 1 s RTC PIT and the watchdog running.
 * It never measures or transmits, and it leaves the EEPROM (pairing keys) alone.
 *
 * Build: make spanek   Flash: 5-test-spanku.cmd   Back: 3-aktualizace-firmwaru.cmd
 */

#include "config.h"
#include "power.h"
#include "cc1101.h"

#define RADIO_INIT_TRIES    3U      /* a radio left awake would mask the sleep current */

int main(void)
{
    (void)power_take_reset_flags();

    power_init();
    wdt_init();
    rtc_pit_init();

    for (uint8_t i = 0; i < RADIO_INIT_TRIES; i++) {
        wdt_kick();
        if (cc1101_init()) {                   /* also puts the radio to SLEEP */
            break;
        }
        power_sleep_ms(100);
    }

    for (;;) {
        power_sleep_seconds(3600);
    }
}
