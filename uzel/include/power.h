/**
 * @file power.h
 * @brief Clocks, low-leakage pin states, RTC-PIT sleep, watchdog, reset cause, node ID.
 */

#ifndef POWER_H_
#define POWER_H_

#include "config.h"

/** Clocks, BOD-in-sleep off, sleep controller, default pin states, interrupts on. */
void power_init(void);

/** Watchdog in normal mode, ~8.2 s timeout (kicked every 1 s PIT wake-up). */
void wdt_init(void);
void wdt_kick(void);

/** RTC PIT 1 s periodic interrupt from the internal 32.768 kHz ULP oscillator. */
void rtc_pit_init(void);

/** Sleep in power-down for the given number of seconds (WDT kicked every second). */
void power_sleep_seconds(uint16_t seconds);

/** Sleep (standby, RTC compare) for a short time – e.g. while the probe settles.
 *  Up to ~2 s; the PIT keeps ticking. */
void power_sleep_ms(uint16_t ms);

/** Small PRNG (xorshift16) for jitter and back-off; seed with something unique. */
void rng_seed(uint16_t seed);
uint16_t rng_next(void);

/** Switch off for good: wake-up sources and watchdog off, power-down forever.
 *  Only a reset (new battery = power-on reset) starts the node again. */
void power_off_forever(void) __attribute__((noreturn));

/** Read and clear RSTCTRL.RSTFR. Returns the raw flags (RSTCTRL_*RF_bm). */
uint8_t power_take_reset_flags(void);

/** Stable 16-bit node ID: CRC-16/CCITT of SIGROW.SERNUM0..9 (never 0 or 0xFFFF). */
uint16_t power_node_id(void);

#endif /* POWER_H_ */
