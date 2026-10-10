/**
 * @file config.h
 * @brief Global hardware configuration and system parameters for ATtiny1624 ULP Sensor Node.
 * @author Matej Svatos
 * @date 2026-10-10
 */

#ifndef CONFIG_H_
#define CONFIG_H_

#include <avr/io.h>
#include <stdint.h>
#include <stdbool.h>

/* ========================================================================== */
/* CPU & System Clock Configuration                                           */
/* ========================================================================== */

/**
 * @brief CPU Operating Frequency: 3.333 MHz (OSC20M divided by 6).
 * Low voltage operation compatible (down to 1.8V), low active dynamic power.
 */
#ifndef F_CPU
#define F_CPU 3333333UL
#endif

/* ========================================================================== */
/* Pinout Definitions (ATtiny1624 SOIC-14)                                    */
/* ========================================================================== */

/* PORT A */
#define PIN_UPDI_PROG           PIN0_bm   /* PA0: UPDI Programming Interface */
#define PIN_SPI_MOSI            PIN1_bm   /* PA1: CC1101 SPI MOSI */
#define PIN_SPI_MISO            PIN2_bm   /* PA2: CC1101 SPI MISO */
#define PIN_SPI_SCK             PIN3_bm   /* PA3: CC1101 SPI SCK */
#define PIN_SPI_CSN             PIN4_bm   /* PA4: CC1101 SPI Chip Select (Active LOW) */
#define PIN_ADC_BAT             PIN5_bm   /* PA5: Battery Voltage Divider ADC Input (AIN5) */
#define PIN_ADC_PROBE           PIN6_bm   /* PA6: Hydrostatic Probe ADC Input (AIN6) */
#define PIN_EN_BAT_MEASURE      PIN7_bm   /* PA7: Battery Measurement Switch Control (Active HIGH) */

/* PORT B */
#define PIN_I2C_SCL             PIN0_bm   /* PB0: SHT40 TWI0 SCL */
#define PIN_I2C_SDA             PIN1_bm   /* PB1: SHT40 TWI0 SDA */
#define PIN_RF_GDO0             PIN2_bm   /* PB2: CC1101 GDO0 Status / Sync Interrupt */
#define PIN_EN_5V_PROBE         PIN3_bm   /* PB3: TPS63900 5V Step-Up Enable (Active HIGH) */

/* ========================================================================== */
/* Battery & Power Thresholds                                                 */
/* ========================================================================== */

/**
 * @brief Critical battery threshold (in mV).
 * 3x Li-FeS2 cells in series: 1.0V/cell cut-off cliff = 3000 mV.
 * Below this voltage, transmitter current draw causes brown-out loops.
 */
#define BATTERY_CRITICAL_MV     3000U

/**
 * @brief Nominal fresh battery voltage (in mV) ~ 4500-5100 mV.
 */
#define BATTERY_NOMINAL_MV      4500U

/* ========================================================================== */
/* Measurement Timing Parameters                                              */
/* ========================================================================== */

/**
 * @brief Battery voltage divider settling delay in milliseconds.
 * High impedance divider (1M + 220k with 22nF C0G capacitor): Tau = 3.96 ms.
 * Settling time 5*Tau ~ 20 ms.
 */
#define BAT_DIVIDER_SETTLE_MS   20U

/**
 * @brief Hydrostatic probe & 5V power supply settling delay in milliseconds.
 * TPS63900 soft start + AFE settling + hydrostatic ceramic sensor stabilization.
 */
#define PROBE_SETTLE_MS         40U

/**
 * @brief Measurement interval in seconds (15 minutes = 900 seconds).
 */
#define SLEEP_INTERVAL_SECONDS  900U

/* ========================================================================== */
/* Telemetry Packet Format (Compact 8 bytes Little-Endian for ULP RF)         */
/* Hardware CRC-16, Sync Word and Whitening handled by CC1101 Transceiver     */
/* ========================================================================== */

typedef struct __attribute__((packed)) {
    uint16_t vbat_raw_mv;   /**< Battery voltage in millivolts (e.g. 4450 = 4.45 V) */
    int16_t  temp_x100;     /**< Temperature in 0.01 deg C (e.g. 2150 = 21.50 C) */
    uint16_t hum_x100;      /**< Relative humidity in 0.01 % RH (e.g. 6540 = 65.40 % RH) */
    uint16_t level_mm;      /**< Water depth in millimeters (e.g. 1450 = 1450 mm) */
} telemetry_packet_t;

#endif /* CONFIG_H_ */
