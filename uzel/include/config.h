/**
 * @file config.h
 * @brief Global hardware configuration, tuning constants and the v2 radio packet
 *        for the ATtiny1624 ultra-low-power tank sensor node.
 *
 * Every constant that may need tuning on real hardware lives in this file.
 */

#ifndef CONFIG_H_
#define CONFIG_H_

#include <avr/io.h>
#include <stdint.h>
#include <stdbool.h>

/* ========================================================================== */
/* CPU clock: OSC20M / 6 = 3.333 MHz (safe down to 1.8 V, low active current) */
/* ========================================================================== */
#ifndef F_CPU
#define F_CPU 3333333UL
#endif

/* ========================================================================== */
/* Pinout (ATtiny1624, SOIC-14)                                               */
/* ========================================================================== */
/* PORT A */
#define PIN_UPDI_PROG           PIN0_bm   /* PA0: UPDI */
#define PIN_SPI_MOSI            PIN1_bm   /* PA1: CC1101 SI   */
#define PIN_SPI_MISO            PIN2_bm   /* PA2: CC1101 SO   */
#define PIN_SPI_SCK             PIN3_bm   /* PA3: CC1101 SCLK */
#define PIN_SPI_CSN             PIN4_bm   /* PA4: CC1101 CSn (active low) */
#define PIN_ADC_BAT             PIN5_bm   /* PA5: battery divider  (AIN5) */
#define PIN_ADC_PROBE           PIN6_bm   /* PA6: probe divider    (AIN6) */
#define PIN_EN_BAT_MEASURE      PIN7_bm   /* PA7: battery divider switch (active high) */
/* PORT B */
#define PIN_I2C_SCL             PIN0_bm   /* PB0: SHT40 SCL */
#define PIN_I2C_SDA             PIN1_bm   /* PB1: SHT40 SDA */
#define PIN_RF_GDO0             PIN2_bm   /* PB2: CC1101 GDO0 */
#define PIN_EN_5V_PROBE         PIN3_bm   /* PB3: TPS63900 5 V boost enable (active high) */

/* ========================================================================== */
/* Timing                                                                     */
/* ========================================================================== */
#define INTERVAL_NORMAL_S       300U    /**< Normal measure + transmit interval (5 min) */
#define INTERVAL_FAST_S         60U     /**< Interval while the level is changing fast */
#define BATTERY_PERIOD_S        3600U   /**< Battery is measured once per hour (value repeated in between) */

/** Probe change (0.1 mV units) that switches to the fast interval.
 *  100 = 10 mV ~ 6 mm on a 0-3.3 V / 0-2 m probe (1.65 V/m). */
#define FAST_DELTA_100UV        100U
#define FAST_CALM_CYCLES        3U      /**< Calm measurements before returning to normal */
#define FAST_MAX_CYCLES         90U     /**< Safety cap of consecutive fast cycles */

/* ========================================================================== */
/* Battery (3x Li-FeS2 in series)                                             */
/* ========================================================================== */
/** Below this the packet carries PKT_FLAG_BAT_LOW (warning only). The node never
 *  switches itself off – the electronics work down to ~1.8 V, so it simply keeps
 *  running until the cells can no longer deliver the transmit current. */
#define BATTERY_LOW_MV          3300U

/** Battery divider (high side switched): R_top / R_bot in kOhm.
 *  Measured against the internal 1.024 V reference (tinyAVR 2-series has no 1.1 V):
 *  full scale = 1.024 * 1220 / 220 = 5.68 V (fresh 3x Li-FeS2 ~5.4 V open circuit). */
#define BAT_RTOP_K              1000UL
#define BAT_RBOT_K              220UL
#define BAT_SETTLE_MS           20U     /**< 5 tau of divider + 22 nF C0G */

/* ========================================================================== */
/* Hydrostatic probe                                                          */
/* ========================================================================== */
/** Probe output divider in kOhm (pin = probe * RBOT / (RTOP + RBOT)).
 *  Probe output 0-3.3 V -> pin 0-2.269 V, measured against the 2.500 V reference
 *  (~10 % headroom, saturation only above 3.64 V probe output). */
#define PROBE_RTOP_K            100UL
#define PROBE_RBOT_K            220UL
/** Boost soft start + probe electronics start-up. Check the probe datasheet! */
#define PROBE_SETTLE_MS         150U
#define PROBE_BURSTS            16U     /**< Bursts spread over ~70 ms (wave / mains noise filtering) */
#define PROBE_BURST_GAP_MS      3U
#define PROBE_TRIM_KEEP         8U      /**< Middle bursts averaged (trimmed mean) */

/* ========================================================================== */
/* Radio                                                                      */
/* ========================================================================== */
/** PATABLE value: 0xC0 = +10 dBm, 0x60 = 0 dBm (433 MHz band). */
#define RF_PA_VALUE             0xC0

/* ========================================================================== */
/* Radio protocol v3 – encrypted + authenticated, CC1101 variable length mode  */
/* (length byte, whitening and CRC-16 added in hardware). Little endian.      */
/*                                                                            */
/* DATA     21 B  [0x03][node_id 2][ctr 4][ciphertext 10][mac 4]              */
/*                plaintext = telemetry_t (10 B), keystream = AES(Kenc,       */
/*                node_id|ctr|0..), mac = CMAC(Kmac, bytes 0..16)[0..3]       */
/* PAIR_REQ 36 B  [0x04][node_id 2][node X25519 public key 32][state 1]      */
/*                state bit0 = node holds a key (paired)          (cleartext) */
/* PAIR_ACK 35 B  [0x05][node_id 2][hub  X25519 public key 32]   (hub -> node)*/
/*                accepted ONLY by an unpaired node                           */
/* UNPAIR   19 B  [0x06][node_id 2][CMAC(Kmac, 0x06|node_id|node pub) 16]     */
/*                the only thing a paired node accepts: forget the key        */
/*                shared S = X25519; K0 = S[0..15] ^ S[16..31];               */
/*                Kenc = AES(K0,"RTK-ENC-KEY"), Kmac = AES(K0,"RTK-MAC-KEY")   */
/* ========================================================================== */
#define PKT_DATA                0x03
#define PKT_PAIR_REQ            0x04
#define PKT_PAIR_ACK            0x05
#define PKT_UNPAIR              0x06
#define PKT_DATA_LEN            21U
#define PKT_PAIR_LEN            35U     /**< PAIR_ACK */
#define PKT_PAIR_REQ_LEN        36U
#define PKT_UNPAIR_LEN          19U
#define PAIR_REQ_PAIRED         0x01
#define PKT_MAC_LEN             4U
#define FW_VERSION              33U     /**< 3.3 – reported in every packet */

#define PAIR_WINDOW_S           600U    /**< Pairing offered for 10 min after a battery change */
#define PAIR_PERIOD_S           5U      /**< One PAIR_REQ every 5 s inside the window */
#define PAIR_RX_WINDOW_MS       600U    /**< Listen for PAIR_ACK / UNPAIR after each request */
#define UNPAIRED_FAST_S         60U     /**< Unpaired node: one PAIR_REQ per minute ... */
#define UNPAIRED_FAST_TIME_S    86400UL /**< ... for the first day, then every INTERVAL_NORMAL_S */
#define CTR_RESERVE             1024UL  /**< Frame counter persisted in EEPROM in steps of 1024 */

#define PKT_FLAG_BAT_ERR        0x01    /**< Battery ADC conversion failed */
#define PKT_FLAG_SHT_ERR        0x02    /**< SHT40 bus / CRC error */
#define PKT_FLAG_PROBE_ERR      0x04    /**< Probe ADC conversion failed */
#define PKT_FLAG_PROBE_CLIP     0x08    /**< Probe ADC saturated (reading is a lower bound) */
#define PKT_FLAG_BAT_LOW        0x10    /**< Battery below BATTERY_LOW_MV (warning only) */
#define PKT_FLAG_BOOT           0x20    /**< First packet after a reset */
#define PKT_FLAG_WDT_RESET      0x40    /**< Last reset was caused by the watchdog */
#define PKT_FLAG_FAST           0x80    /**< Node is in the fast (60 s) interval */

#define PKT_TEMP_INVALID        INT16_MIN
#define PKT_U16_INVALID         0xFFFFU

/** Encrypted part of a DATA packet (Python: '<BHhHHB'). */
typedef struct __attribute__((packed)) {
    uint8_t  flags;        /**< PKT_FLAG_* */
    uint16_t vbat_mv;      /**< Battery voltage [mV] */
    int16_t  temp_x100;    /**< Temperature [0.01 degC] */
    uint16_t hum_x100;     /**< Relative humidity [0.01 %] */
    uint16_t probe_100uv;  /**< Probe output voltage [0.1 mV] */
    uint8_t  fw;           /**< FW_VERSION */
} telemetry_t;

_Static_assert(sizeof(telemetry_t) == 10, "telemetry must be 10 bytes");

#endif /* CONFIG_H_ */
