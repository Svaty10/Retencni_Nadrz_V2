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
/* Telemetry Packet Format (Fixed size for robust CC1101 transmission)        */
/* ========================================================================== */

#define TELEMETRY_MAGIC_BYTE    0xA5
#define NODE_ID_DEFAULT         0x01

typedef enum {
    PACKET_TYPE_REGULAR   = 0x01,
    PACKET_TYPE_BAT_LOW   = 0x02,
    PACKET_TYPE_BOOT      = 0x03,
    PACKET_TYPE_ERROR     = 0xEE
} packet_type_t;

/**
 * @brief Packed telemetry structure (16 bytes).
 */
typedef struct __attribute__((packed)) {
    uint8_t  magic;             /**< Fixed header 0xA5 */
    uint8_t  node_id;           /**< Sensor Node ID */
    uint8_t  packet_type;       /**< packet_type_t */
    uint16_t seq_id;            /**< Incremental message counter */
    uint16_t vbat_mv;           /**< Battery voltage in millivolts */
    uint16_t water_level_raw;   /**< Oversampled ADC raw reading from probe */
    int16_t  temperature_c_x10; /**< SHT40 temperature in 0.1 deg C */
    uint16_t humidity_rh_x10;   /**< SHT40 relative humidity in 0.1 % RH */
    uint8_t  status_flags;      /**< Bit 0: SHT40 OK, Bit 1: ADC OK, Bit 2: Probe 5V OK */
    uint16_t crc16;             /**< Payload CRC16-CCITT */
} telemetry_packet_t;

/* Status Flag Bits */
#define STATUS_FLAG_SHT40_OK    (1 << 0)
#define STATUS_FLAG_ADC_OK      (1 << 1)
#define STATUS_FLAG_PROBE_OK    (1 << 2)

#endif /* CONFIG_H_ */
