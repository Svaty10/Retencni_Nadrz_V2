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

    /* Initial measurement and boot notification */
    uint16_t vbat_mv = 0;
    uint16_t probe_raw = 0;
    uint16_t probe_mv = 0;
    int16_t  temp_c_x10 = 0;
    uint16_t hum_rh_x10 = 0;

    /* Primary application loop */
    while (1) {
        wdt_kick();

        uint8_t status_flags = 0;

        /* ------------------------------------------------------------------ */
        /* Phase A: Battery Voltage Measurement & Failsafe Check              */
        /* ------------------------------------------------------------------ */
        bool bat_ok = adc_measure_battery(&vbat_mv);
        if (bat_ok) {
            status_flags |= STATUS_FLAG_ADC_OK;
        } else {
            vbat_mv = 0;
        }

        /* Li-FeS2 Battery Protection Failsafe:
         * Below 3000 mV (1.0 V/cell), cells are at the end of their discharge knee.
         * Firing the transmitter under this condition causes severe voltage sag,
         * resulting in repetitive brown-out reset loops. */
        if (bat_ok && (vbat_mv < BATTERY_CRITICAL_MV)) {
            /* Assemble emergency message */
            telemetry_packet_t emg_pkt;
            memset(&emg_pkt, 0, sizeof(emg_pkt));
            emg_pkt.magic       = TELEMETRY_MAGIC_BYTE;
            emg_pkt.node_id     = NODE_ID_DEFAULT;
            emg_pkt.packet_type = PACKET_TYPE_BAT_LOW;
            emg_pkt.seq_id      = g_seq_counter++;
            emg_pkt.vbat_mv     = vbat_mv;
            emg_pkt.status_flags = status_flags;
            emg_pkt.crc16       = cc1101_calc_crc16((const uint8_t *)&emg_pkt,
                                                    sizeof(telemetry_packet_t) - 2);

            /* Attempt single emergency transmission */
            (void)cc1101_transmit_packet(&emg_pkt, sizeof(emg_pkt));

            /* Shut down permanently to prevent cell destruction */
            power_enter_permanent_sleep();
        }

        /* ------------------------------------------------------------------ */
        /* Phase B: Hydrostatic Water Level Measurement                       */
        /* ------------------------------------------------------------------ */
        wdt_kick();
        bool probe_ok = adc_measure_probe(&probe_raw, &probe_mv);
        if (probe_ok) {
            status_flags |= STATUS_FLAG_PROBE_OK;
        } else {
            probe_raw = 0;
            probe_mv = 0;
        }

        /* ------------------------------------------------------------------ */
        /* Phase C: SHT40 Temperature and Humidity Measurement                */
        /* ------------------------------------------------------------------ */
        wdt_kick();
        bool sht_ok = sht40_read_measurements(&temp_c_x10, &hum_rh_x10);
        if (sht_ok) {
            status_flags |= STATUS_FLAG_SHT40_OK;
        } else {
            temp_c_x10 = -999;
            hum_rh_x10 = 0;
        }

        /* ------------------------------------------------------------------ */
        /* Phase D: Telemetry Packet Assembly & RF Transmission               */
        /* ------------------------------------------------------------------ */
        telemetry_packet_t packet;
        packet.magic            = TELEMETRY_MAGIC_BYTE;
        packet.node_id          = NODE_ID_DEFAULT;
        packet.packet_type      = PACKET_TYPE_REGULAR;
        packet.seq_id           = g_seq_counter++;
        packet.vbat_mv          = vbat_mv;
        packet.water_level_raw  = probe_raw;
        packet.temperature_c_x10 = temp_c_x10;
        packet.humidity_rh_x10   = hum_rh_x10;
        packet.status_flags     = status_flags;
        packet.crc16            = cc1101_calc_crc16((const uint8_t *)&packet,
                                                    sizeof(telemetry_packet_t) - 2);

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
