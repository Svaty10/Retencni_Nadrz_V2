/**
 * @file main.c
 * @brief Production ULP Sensor Node firmware for ATtiny1624.
 * @author Matej
 * @date 2026-10-10
 */

#include "config.h"
#include "power.h"
#include "adc.h"
#include "sht40.h"
#include "cc1101.h"
#include <string.h>

/* Global monotonic transmission sequence counter */
static uint16_t g_seq_counter = 0;

int main(void)
{
    /* 1. Low-power hardware initialization */
    power_init();
    wdt_init();
    wdt_kick();

    adc_init();

    /* 2. Configure RF transceiver once, then immediately put into SPWD */
    bool rf_ok = cc1101_init();

    /* 3. Configure RTC Periodic Interrupt Timer */
    rtc_pit_init();

    /* Initial measurement variables */
    uint16_t vbat_mv = 0;
    uint16_t probe_raw = 0;
    uint16_t probe_mv = 0;
    int16_t  temp_x100 = 0;
    uint16_t hum_x100 = 0;
    uint16_t level_mm = 0;

    /* Primary application loop */
    while (1) {
        wdt_kick();

        /* ------------------------------------------------------------------ */
        /* Phase A: Battery Voltage Measurement & Failsafe Check              */
        /* ------------------------------------------------------------------ */
        bool bat_ok = adc_measure_battery(&vbat_mv);
        if (!bat_ok) {
            vbat_mv = 0;
        }

        /* Li-FeS2 Battery Protection Failsafe:
         * Below 3000 mV (1.0 V/cell), cells are at the end of their discharge knee.
         * Firing transmitter under this condition causes severe voltage sag and brown-out loop. */
        if (bat_ok && (vbat_mv < BATTERY_CRITICAL_MV)) {
            telemetry_packet_t emg_pkt;
            emg_pkt.vbat_raw_mv = vbat_mv;
            emg_pkt.temp_x100   = 0;
            emg_pkt.hum_x100    = 0;
            emg_pkt.level_mm    = 0xFFFF; /* Special marker for critical battery */

            /* Attempt single emergency transmission */
            (void)cc1101_transmit_packet(&emg_pkt, sizeof(emg_pkt));

            /* Shut down permanently to protect battery cells */
            power_enter_permanent_sleep();
        }

        /* ------------------------------------------------------------------ */
        /* Phase B: Hydrostatic Water Level Measurement                       */
        /* ------------------------------------------------------------------ */
        wdt_kick();
        bool probe_ok = adc_measure_probe(&probe_raw, &probe_mv);
        if (probe_ok) {
            /* Standard hydrostatic level sensor: 0-2000 mm tank depth (zero at 500 mV, span 4000 mV) */
            level_mm = adc_probe_mv_to_depth_mm(probe_mv, 500, 4000, 2000);
        } else {
            probe_mv = 0;
            level_mm = 0;
        }

        /* ------------------------------------------------------------------ */
        /* Phase C: SHT40 Temperature and Humidity Measurement                */
        /* ------------------------------------------------------------------ */
        wdt_kick();
        bool sht_ok = sht40_read_measurements(&temp_x100, &hum_x100);
        if (!sht_ok) {
            temp_x100 = -9999;
            hum_x100  = 0;
        }

        /* ------------------------------------------------------------------ */
        /* Phase D: Telemetry Packet Assembly & RF Transmission (8 Bytes)     */
        /* ------------------------------------------------------------------ */
        telemetry_packet_t packet;
        packet.vbat_raw_mv = vbat_mv;
        packet.temp_x100   = temp_x100;
        packet.hum_x100    = hum_x100;
        packet.level_mm    = level_mm;

        wdt_kick();
        (void)cc1101_transmit_packet(&packet, sizeof(packet));

        /* ------------------------------------------------------------------ */
        /* Phase E: Deep Power-down Sleep (15 minutes = 900 seconds)          */
        /* ------------------------------------------------------------------ */
        power_reset_interval_counter();

        while (!power_interval_elapsed()) {
            wdt_kick();
            power_enter_sleep();
        }
    }

    return 0;
}
