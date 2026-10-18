/**
 * @file main.c
 * @brief ATtiny1624 tank sensor node – measure, send one encrypted packet, sleep.
 *
 * Cycle:
 *   [battery, every 12th cycle ≈ 1 h, ≈25 ms] -> probe (≈250 ms incl. settling)
 *   -> SHT40 (≈10 ms) -> encrypted TX (≈55 ms) -> power-down until the next cycle.
 *   The battery is measured inside the normal wake-up, never in a separate one.
 *
 * Interval: 300 s, 60 s while the level changes fast (adaptive) and for the first
 * BOOT_FAST_CYCLES after a reset (installation feedback).
 *
 * Pairing: after a battery change (power-on reset) the node sends a PAIR_REQ
 * every 5 s for PAIR_WINDOW_S and listens 0.6 s for the hub's answer.
 *  - paired node: keeps measuring with its key; accepts only an UNPAIR signed with
 *    that key (its own hub, the user asked for unpair / re-pair). Without it the
 *    node simply stays paired – a foreign hub cannot take it over.
 *  - unpaired node (new, or just released): accepts a PAIR_ACK. After the window
 *    it keeps offering pairing – every minute for the first day, then every 5 min –
 *    until some hub pairs it.
 *
 * Battery: below BATTERY_LOW_MV the packets carry PKT_FLAG_BAT_LOW; below
 * BATTERY_CUTOFF_MV (1.0 V/cell, confirmed over several hours, not in frost) the
 * node sends a last packet and switches itself off for good (cell reversal
 * protection) – a new battery starts it again.
 *
 * SHT40 heater: a short pulse after the packet when the box stays near 100 % RH
 * (condensation / sensor creep), rate- and battery-limited (see config.h).
 */

#include "config.h"
#include "power.h"
#include "adc.h"
#include "sht40.h"
#include "cc1101.h"
#include "link.h"
#include <stddef.h>

#define BOOT_FAST_CYCLES    10U     /* 10 x 60 s right after power-up */

typedef struct {
    uint16_t node_id;
    uint8_t  sticky_flags;          /* BOOT / WDT_RESET, sent once */
    bool     rf_ok;

    /* battery (measured once per BATTERY_PERIOD_S, inside a normal cycle) */
    uint16_t vbat_mv;
    bool     vbat_valid;
    uint32_t since_bat_s;

    /* adaptive interval */
    bool     have_last_probe;
    uint16_t last_probe;
    uint8_t  calm_cycles;
    uint8_t  fast_run;
    uint8_t  boot_fast_left;
    uint16_t interval_s;

    /* battery under TX load (measured hourly during the transmission) */
    uint16_t vbat_load_mv;
    bool     load_ok;
    uint8_t  sag;                   /* rest - load [SAG_UNIT_MV], sent in the next packets */

    /* battery end of life */
    uint8_t  low_count;             /* hourly readings below BATTERY_CUTOFF_MV in a row */
    bool     cutoff;

    /* diagnostics for the next packet (DIAG_*) */
    uint8_t  diag;

    /* SHT40 heater */
    uint32_t humid_s;               /* how long RH has been above HEAT_RH_X100 */
    uint32_t since_heat_s;
} node_state_t;

static node_state_t g;

static uint16_t abs_diff_u16(uint16_t a, uint16_t b)
{
    return (a > b) ? (uint16_t)(a - b) : (uint16_t)(b - a);
}

static bool radio_ready(void)
{
    if (!g.rf_ok) {
        g.rf_ok = cc1101_init();             /* retry radio init every cycle */
    }
    return g.rf_ok;
}

static void sleep_s(uint16_t s)
{
    power_sleep_seconds(s);
    g.since_bat_s += s;
    g.since_heat_s += s;
}

/** Heater pulse when the box has been saturated for a long time (rate-limited). */
static void maybe_heat(bool sht_ok, int16_t temp, uint16_t hum, uint16_t elapsed_s)
{
    if (!sht_ok || hum < HEAT_RH_X100) {
        g.humid_s = 0;
        return;
    }
    g.humid_s += elapsed_s;
    if (g.humid_s < HEAT_HUMID_S || g.since_heat_s < HEAT_MIN_GAP_S) {
        return;
    }
    if (!g.vbat_valid || g.vbat_mv < BATTERY_LOW_MV || temp >= HEAT_MAX_TEMP_X100) {
        return;
    }
    for (uint8_t i = 0; i < HEAT_PULSES; i++) {
        wdt_kick();
        if (!sht40_heater_pulse()) {
            break;
        }
        g.diag |= DIAG_HEATED;
        power_sleep_ms(HEAT_PULSE_WAIT_MS);   /* MCU sleeps while the sensor heats */
    }
    g.since_heat_s = 0;                  /* also after a failed attempt: retry in 6 h */
    g.humid_s = 0;
}

/** Decide the next interval from the probe change rate. Returns true if fast. */
static bool update_interval(bool probe_ok, uint16_t probe)
{
    bool changed = false;

    if (probe_ok) {
        if (g.have_last_probe) {
            /* Normalise the change to a 300 s window so the decision does not
             * depend on the interval that was just used. */
            uint32_t d = abs_diff_u16(probe, g.last_probe);
            d = d * INTERVAL_NORMAL_S / g.interval_s;
            changed = (d > FAST_DELTA_100UV);
        }
        g.last_probe = probe;
        g.have_last_probe = true;
    }

    if (changed) {
        g.calm_cycles = 0;
    } else if (g.calm_cycles < 255U) {
        g.calm_cycles++;
    }

    bool fast = false;
    if (g.calm_cycles < FAST_CALM_CYCLES) {
        if (g.fast_run < FAST_MAX_CYCLES) {  /* battery guard against a noisy probe */
            g.fast_run++;
            fast = true;
        }
    } else {
        g.fast_run = 0;
    }

    if (g.boot_fast_left > 0U) {
        g.boot_fast_left--;
        fast = true;
    }

    g.interval_s = fast ? INTERVAL_FAST_S : INTERVAL_NORMAL_S;
    return fast;
}

/** Battery under load: runs while the frame is on air (~30 mA from the cells). */
static void tx_battery_hook(void)
{
    uint16_t v = 0;
    g.load_ok = adc_measure_battery(&v);
    g.vbat_load_mv = v;
}

/** Measure everything and send one encrypted DATA packet. */
static void measure_and_send(void)
{
    telemetry_t t = {0};
    t.fw = FW_VERSION;
    bool bat_now = false;

    wdt_kick();
    /* ---------------- battery (hourly, inside this wake-up) ---------------- */
    if (g.since_bat_s >= BATTERY_PERIOD_S || !g.vbat_valid) {
        uint16_t v = 0;
        g.vbat_valid = adc_measure_battery(&v);
        if (g.vbat_valid) {
            g.vbat_mv = v;
        }
        g.since_bat_s = 0;
        bat_now = g.vbat_valid;
    }
    if (g.vbat_valid) {
        t.vbat_mv = g.vbat_mv;
        if (g.vbat_mv < BATTERY_LOW_MV) {
            t.flags |= PKT_FLAG_BAT_LOW;     /* warning only, keep working */
        }
    } else {
        t.flags |= PKT_FLAG_BAT_ERR;
        t.vbat_mv = PKT_U16_INVALID;
    }

    /* ---------------- hydrostatic probe ---------------- */
    wdt_kick();
    uint16_t probe = PKT_U16_INVALID;
    bool clipped = false;
    bool probe_ok = adc_measure_probe(&probe, &clipped);
    if (!probe_ok) {
        t.flags |= PKT_FLAG_PROBE_ERR;
        probe = PKT_U16_INVALID;
    } else if (clipped) {
        t.flags |= PKT_FLAG_PROBE_CLIP;
    }
    t.probe_100uv = probe;

    /* ---------------- SHT40 (every cycle) ---------------- */
    wdt_kick();
    int16_t temp = PKT_TEMP_INVALID;
    uint16_t hum = PKT_U16_INVALID;
    bool sht_ok = sht40_read_measurements(&temp, &hum);
    if (!sht_ok) {
        t.flags |= PKT_FLAG_SHT_ERR;
        temp = PKT_TEMP_INVALID;
        hum = PKT_U16_INVALID;
    }
    t.temp_x100 = temp;
    t.hum_x100 = hum;

    /* ---------------- interval decision & transmit ---------------- */
    uint16_t elapsed_s = g.interval_s;       /* time since the previous measurement */
    if (update_interval(probe_ok && !clipped, probe)) {
        t.flags |= PKT_FLAG_FAST;
    }
    t.flags |= g.sticky_flags;
    t.diag = (uint8_t)(g.diag | (bat_now ? DIAG_BAT_FRESH : 0U));
    t.vbat_sag = g.sag;
    bool important = (t.flags & PKT_FLAG_BOOT) || g.cutoff;
    g.load_ok = false;
    if (radio_ready()) {
        wdt_kick();
        cc1101_tx_hook = bat_now ? tx_battery_hook : NULL;   /* battery under TX load */
        bool sent = link_send_data(&t);
        cc1101_tx_hook = NULL;
        if (sent) {
            g.sticky_flags = 0;              /* delivered to the air at least once */
            g.diag = (cc1101_lbt_backoffs > 3U) ? 3U : cc1101_lbt_backoffs;
            if (important) {
                power_sleep_ms((uint16_t)(REPEAT_GAP_MIN_MS + (rng_next() & (REPEAT_GAP_SPAN_MS - 1U))));
                wdt_kick();
                (void)link_resend_last();
            }
        } else {
            g.rf_ok = false;                 /* force a full re-init next time */
            g.diag |= DIAG_RADIO_REINIT;
        }
    }

    /* ---------------- battery end of life (hourly, the worse of rest / TX load) ---- */
    if (bat_now) {
        uint16_t worst = g.vbat_mv;
        if (g.load_ok) {
            uint16_t drop = (g.vbat_mv > g.vbat_load_mv) ? (uint16_t)(g.vbat_mv - g.vbat_load_mv) : 0U;
            drop = (uint16_t)((drop + SAG_UNIT_MV / 2U) / SAG_UNIT_MV);
            g.sag = (drop > 254U) ? 254U : (uint8_t)drop;
            if (g.vbat_load_mv < worst) {
                worst = g.vbat_load_mv;
            }
        }
        if (worst < BATTERY_CUTOFF_MV) {
            if (g.low_count < 255U) {
                g.low_count++;
            }
        } else {
            g.low_count = 0;
        }
        if (g.low_count >= CUTOFF_CONFIRM && (!sht_ok || temp >= CUTOFF_MIN_TEMP_X100)) {
            g.cutoff = true;                 /* the next packet is the last one */
        }
    }

    /* ---------------- SHT40 heater (after the packet, never on the last one) ---- */
    if (!g.cutoff) {
        maybe_heat(sht_ok, temp, hum, elapsed_s);
    }
}

/** Batteries already empty when inserted: two readings ~1-2 s apart, no frost.
 *  *vbat = first reading (0 if it failed). */
static bool battery_dead_at_boot(uint16_t *vbat)
{
    uint16_t v1 = 0, v2 = 0;
    *vbat = 0;
    if (!adc_measure_battery(&v1)) {
        return false;
    }
    *vbat = v1;
    if (v1 >= BATTERY_CUTOFF_MV) {
        return false;
    }
    power_sleep_seconds(2);
    if (!adc_measure_battery(&v2) || v2 >= BATTERY_CUTOFF_MV) {
        return false;
    }
    int16_t temp = 0;
    uint16_t hum = 0;
    return !sht40_read_measurements(&temp, &hum) || temp >= CUTOFF_MIN_TEMP_X100;
}

int main(void)
{
    uint8_t rst = power_take_reset_flags();

    power_init();
    wdt_init();
    adc_init();
    rtc_pit_init();

    g.node_id = power_node_id();
    rng_seed(g.node_id);                       /* unique per chip -> nodes drift apart */
    g.sticky_flags = PKT_FLAG_BOOT | ((rst & RSTCTRL_WDRF_bm) ? PKT_FLAG_WDT_RESET : 0);
    g.calm_cycles = FAST_CALM_CYCLES;
    g.boot_fast_left = BOOT_FAST_CYCLES;
    g.interval_s = INTERVAL_FAST_S;
    g.since_bat_s = BATTERY_PERIOD_S;          /* measure in the first cycle */
    g.since_heat_s = HEAT_MIN_GAP_S;           /* heater allowed right away if needed */
    g.sag = PKT_SAG_UNKNOWN;
    if (rst & RSTCTRL_BORF_bm) {
        g.diag |= DIAG_BOD_RESET;
    }

    wdt_kick();
    g.rf_ok = cc1101_init();                   /* also puts the radio to SLEEP */
    uint16_t vbat_boot;
    if (battery_dead_at_boot(&vbat_boot)) {
        power_off_forever();                   /* no pairing on empty cells */
    }
    link_set_fast_clock(vbat_boot >= PAIR_10MHZ_MIN_MV);
    bool paired = link_init(g.node_id);

    /* ---------------- pairing window after a battery change ---------------- */
    bool power_on = (rst & (RSTCTRL_PORF_bm | RSTCTRL_BORF_bm | RSTCTRL_UPDIRF_bm)) != 0;
    if (power_on || !paired) {
        uint16_t left = PAIR_WINDOW_S;
        uint16_t since_data = INTERVAL_FAST_S;   /* paired node: report right away */
        while (left > 0) {
            if (link_paired() && since_data >= INTERVAL_FAST_S) {
                measure_and_send();              /* keep reporting with the old keys */
                since_data = 0;
                if (g.cutoff) {
                    measure_and_send();          /* last packet (sent twice) */
                    power_off_forever();
                }
            }
            if (radio_ready()) {
                link_pair_t r = link_pair_attempt();
                if (r == LINK_PAIR_PAIRED) {
                    g.sticky_flags |= PKT_FLAG_BOOT; /* first packet with the new keys */
                    break;                       /* paired – done */
                }
                /* LINK_PAIR_UNPAIRED: released by the hub, keep offering pairing in
                 * this window (re-pair to the same hub, or to a new one) */
            }
            sleep_s(PAIR_PERIOD_S);
            since_data += PAIR_PERIOD_S + 1U;    /* + time spent sending/listening */
            left = (left > PAIR_PERIOD_S + 1U) ? (uint16_t)(left - PAIR_PERIOD_S - 1U) : 0U;
        }
    }

    uint32_t unpaired_s = 0;
    for (;;) {
        if (!link_paired()) {
            /* unpaired: keep offering pairing until a hub accepts, nothing else to do */
            if (radio_ready() && link_pair_attempt() == LINK_PAIR_PAIRED) {
                g.sticky_flags |= PKT_FLAG_BOOT;
                continue;                        /* report right away */
            }
            uint16_t s = (unpaired_s < UNPAIRED_FAST_TIME_S) ? UNPAIRED_FAST_S : INTERVAL_NORMAL_S;
            unpaired_s += s;
            sleep_s(s);
            continue;
        }
        measure_and_send();
        if (g.cutoff) {
            measure_and_send();                  /* last packet (sent twice), then off */
            power_off_forever();
        }
        /* random jitter around the interval (average unchanged) */
        sleep_s((uint16_t)(g.interval_s - JITTER_S / 2U + (rng_next() % JITTER_S)));
    }
}
