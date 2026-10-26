/**
 * @file sched.h
 * @brief Wake-up schedule and quick-check trigger – pure functions, no hardware,
 *        so the decisions can be unit-tested on the host (tools/sched_test.c).
 *
 * Timeline (normal mode, one scheduler for both kinds of wake-up):
 *
 *   TX ---150--- Q ---142..157--- TX        (QUICK_CHECK_S = 150 s)
 *
 *   After every transmission the node draws the next full-cycle period
 *   (interval +-JITTER_S/2) and walks towards it in QUICK_CHECK_S steps. Each
 *   step that does not end at the full cycle is a quick check (Q: probe only).
 *   The last step takes whatever is left, so a quick check and a full cycle
 *   never coincide and the jitter only moves the full cycle. In the fast mode
 *   (60 s period) the first step already is the full cycle – no quick checks.
 *
 *   A quick check compares the probe with the last transmitted value. A change
 *   of at least the threshold on QUICK_CONFIRM checks in a row cuts the wait
 *   short: full cycle + transmission right away. The full cycle's own reading
 *   then decides about the fast mode (a noisy check costs one packet at most).
 */

#ifndef SCHED_H_
#define SCHED_H_

#include <stdint.h>
#include <stdbool.h>

/**
 * @brief Next sleep towards the full cycle.
 * @param[in,out] remaining_s seconds left until the full cycle, reduced by the sleep
 * @param[in]     step_s      quick-check spacing (QUICK_CHECK_S)
 * @param[out]    quick       true: a quick check follows, false: the full cycle
 * @return seconds to sleep
 *
 * A quick check is only placed when at least half a step stays before the full
 * cycle (no check a few seconds before a transmission).
 */
static inline uint16_t sched_next_sleep(uint16_t *remaining_s, uint16_t step_s, bool *quick)
{
    uint16_t r = *remaining_s;
    if (r > step_s + step_s / 2U) {
        *remaining_s = (uint16_t)(r - step_s);
        *quick = true;
        return step_s;
    }
    *remaining_s = 0;
    *quick = false;
    return r;
}

/**
 * @brief Quick-check decision.
 * @param[in,out] hits     consecutive checks over the threshold (reset by the caller after a TX)
 * @param[in]     ok       probe reading valid and not clipped, reference available
 * @param[in]     probe    probe now [0.1 mV]
 * @param[in]     ref      last transmitted probe [0.1 mV]
 * @param[in]     delta    threshold [0.1 mV] (FAST_DELTA_100UV, |change| >= delta)
 * @param[in]     confirm  checks in a row needed (QUICK_CONFIRM)
 * @return true: run the full cycle and transmit now
 */
static inline bool sched_quick_trigger(uint8_t *hits, bool ok, uint16_t probe, uint16_t ref,
                                       uint16_t delta, uint8_t confirm)
{
    uint16_t d = (probe > ref) ? (uint16_t)(probe - ref) : (uint16_t)(ref - probe);
    if (!ok || d < delta) {
        *hits = 0;                           /* one calm / invalid reading breaks the series */
        return false;
    }
    if (*hits < 255U) {
        (*hits)++;
    }
    return *hits >= confirm;
}

#endif /* SCHED_H_ */
