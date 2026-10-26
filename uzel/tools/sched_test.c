/* Host test of the wake-up schedule and the quick-check trigger (include/sched.h).
 *   cc -I include tools/sched_test.c -o /tmp/st && /tmp/st
 * Prints "OK" when all cases pass. */
#include <stdio.h>
#include "sched.h"

/* values of include/config.h (needs the AVR headers, not usable on the host) */
#define QUICK_CHECK_S     150
#define QUICK_CONFIRM     1
#define FAST_DELTA_100UV  100

static int fails = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); fails++; } } while (0)

/* Walk one period, record the wake-up times; returns the number of quick checks. */
static int walk(uint16_t period, uint16_t step, uint16_t *t_full)
{
    uint16_t remaining = period, t = 0, last_q = 0;
    int checks = 0;
    bool quick = true;
    while (quick) {
        t += sched_next_sleep(&remaining, step, &quick);
        if (quick) {
            CHECK(t == last_q + step);      /* checks every step from the TX */
            last_q = t;
            checks++;
        }
    }
    CHECK(checks == 0 || t - last_q >= step / 2);   /* no check right before the full cycle */
    *t_full = t;
    return checks;
}

int main(void)
{
    uint16_t t;
    /* normal mode: 300 s +- 8 s jitter; fw 12 (150 s) -> 1 check, fw 11 (60 s) -> 4 checks;
     * the full cycle always exactly at the period */
    for (uint16_t p = 292; p <= 307; p++) {
        CHECK(walk(p, QUICK_CHECK_S, &t) == 1);
        CHECK(t == p);
        CHECK(walk(p, 60, &t) == 4);
        CHECK(t == p);
    }
    /* fast mode: 60 s +- 8 s -> no checks */
    for (uint16_t p = 52; p <= 67; p++) {
        CHECK(walk(p, QUICK_CHECK_S, &t) == 0);
        CHECK(t == p);
    }

    /* fw 12 trigger: one check over the threshold is enough (QUICK_CONFIRM = 1) */
    uint8_t h1 = 0;
    CHECK(sched_quick_trigger(&h1, true, 1100, 1000, FAST_DELTA_100UV, QUICK_CONFIRM));
    h1 = 0;
    CHECK(!sched_quick_trigger(&h1, true, 1099, 1000, FAST_DELTA_100UV, QUICK_CONFIRM));
    CHECK(!sched_quick_trigger(&h1, false, 1500, 1000, FAST_DELTA_100UV, QUICK_CONFIRM));

    /* trigger: 2 checks in a row with |delta| >= 100 */
    uint8_t h = 0;
    CHECK(!sched_quick_trigger(&h, true, 1100, 1000, 100, 2));   /* +10 mV: 1st hit */
    CHECK(sched_quick_trigger(&h, true, 900, 1000, 100, 2));     /* -10 mV: 2nd -> fire */
    h = 0;
    CHECK(!sched_quick_trigger(&h, true, 1100, 1000, 100, 2));
    CHECK(!sched_quick_trigger(&h, true, 1099, 1000, 100, 2));   /* below: series broken */
    CHECK(h == 0);
    CHECK(!sched_quick_trigger(&h, true, 1100, 1000, 100, 2));
    CHECK(!sched_quick_trigger(&h, false, 1200, 1000, 100, 2));  /* invalid breaks it too */
    CHECK(!sched_quick_trigger(&h, true, 1200, 1000, 100, 2));
    CHECK(sched_quick_trigger(&h, true, 1200, 1000, 100, 2));
    h = 0;                                                       /* single spike never fires */
    for (int i = 0; i < 10; i++) {
        CHECK(!sched_quick_trigger(&h, true, (i & 1) ? 1500 : 1000, 1000, 100, 2));
    }

    printf(fails ? "FAILED\n" : "OK\n");
    return fails != 0;
}
