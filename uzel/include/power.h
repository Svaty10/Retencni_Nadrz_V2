/**
 * @file power.h
 * @brief Power management, Clock control, Sleep modes, RTC-PIT, and Watchdog.
 * @author Matej
 * @date 2026-10-10
 */

#ifndef POWER_H_
#define POWER_H_

#include "config.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Initialize system clocks, GPIO low-power states, BOD, and RTC.
 */
void power_init(void);

/**
 * @brief Initialize and start the Watchdog Timer in normal mode (~8s timeout).
 */
void wdt_init(void);

/**
 * @brief Reset (kick) the Watchdog Timer.
 */
void wdt_kick(void);

/**
 * @brief Disable Watchdog Timer safely.
 */
void wdt_disable(void);

/**
 * @brief Configure and enable RTC Periodic Interrupt Timer (PIT) for periodic wakeups.
 */
void rtc_pit_init(void);

/**
 * @brief Put CPU to sleep until next PIT wake-up tick.
 */
void power_enter_sleep(void);

/**
 * @brief Check whether sleep timer has elapsed (e.g. 15 minutes).
 * @return true if target interval reached, false otherwise.
 */
bool power_interval_elapsed(void);

/**
 * @brief Reset the interval counter back to zero.
 */
void power_reset_interval_counter(void);

/**
 * @brief Emergency shutdown: disable RTC wakeups, disable peripherals, and enter permanent sleep.
 * Used when battery voltage drops below critical threshold to protect cells from destruction.
 */
void power_enter_permanent_sleep(void);

/**
 * @brief Configure pins for lowest possible leakage before sleep.
 */
void power_prepare_pins_for_sleep(void);

/**
 * @brief Restore pin configuration after wake-up.
 */
void power_restore_pins_after_wakeup(void);

#ifdef __cplusplus
}
#endif

#endif /* POWER_H_ */
