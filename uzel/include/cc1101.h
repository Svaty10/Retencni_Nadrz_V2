/**
 * @file cc1101.h
 * @brief Low-power bare-metal driver for CC1101 433 MHz RF transceiver.
 * @author Matej
 * @date 2026-10-10
 */

#ifndef CC1101_H_
#define CC1101_H_

#include "config.h"

#ifdef __cplusplus
extern "C" {
#endif

/* CC1101 Command Strobes */
#define CC1101_SRES         0x30    /**< Reset chip */
#define CC1101_SFSTXON      0x31    /**< Enable and calibrate frequency synthesizer */
#define CC1101_SXOFF        0x32    /**< Turn off crystal oscillator */
#define CC1101_SCAL         0x33    /**< Calibrate frequency synthesizer */
#define CC1101_SRX          0x34    /**< Enable RX */
#define CC1101_STX          0x35    /**< Enable TX */
#define CC1101_SIDLE        0x36    /**< Exit RX / TX, turn off frequency synthesizer */
#define CC1101_SWOR         0x38    /**< Start automatic RX polling sequence */
#define CC1101_SPWD         0x39    /**< Enter power down mode when CSn goes high */
#define CC1101_SFRX         0x3A    /**< Flush the RX FIFO buffer */
#define CC1101_SFTX         0x3B    /**< Flush the TX FIFO buffer */
#define CC1101_SWORRST      0x3C    /**< Reset real time clock to Event1 value */
#define CC1101_SNOP         0x3D    /**< No operation */

/* CC1101 Configuration Registers */
#define CC1101_IOCFG2       0x00    /**< GDO2 output pin configuration */
#define CC1101_IOCFG1       0x01    /**< GDO1 output pin configuration */
#define CC1101_IOCFG0       0x02    /**< GDO0 output pin configuration */
#define CC1101_FIFOTHR      0x03    /**< RX FIFO and TX FIFO thresholds */
#define CC1101_SYNC1        0x04    /**< Sync word, high byte */
#define CC1101_SYNC0        0x05    /**< Sync word, low byte */
#define CC1101_PKTLEN       0x06    /**< Packet length */
#define CC1101_PKTCTRL1     0x07    /**< Packet automation control */
#define CC1101_PKTCTRL0     0x08    /**< Packet automation control */
#define CC1101_ADDR         0x09    /**< Device address */
#define CC1101_CHANNR       0x0A    /**< Channel number */
#define CC1101_FSCTRL1      0x0B    /**< Frequency synthesizer control */
#define CC1101_FSCTRL0      0x0C    /**< Frequency synthesizer control */
#define CC1101_FREQ2        0x0D    /**< Frequency control word, high byte */
#define CC1101_FREQ1        0x0E    /**< Frequency control word, middle byte */
#define CC1101_FREQ0        0x0F    /**< Frequency control word, low byte */
#define CC1101_MDMCFG4      0x10    /**< Modem configuration */
#define CC1101_MDMCFG3      0x11    /**< Modem configuration */
#define CC1101_MDMCFG2      0x12    /**< Modem configuration */
#define CC1101_MDMCFG1      0x13    /**< Modem configuration */
#define CC1101_MDMCFG0      0x14    /**< Modem configuration */
#define CC1101_DEVIATN      0x15    /**< Modem deviation setting */
#define CC1101_MCSM2        0x16    /**< Main Radio Control State Machine config */
#define CC1101_MCSM1        0x17    /**< Main Radio Control State Machine config */
#define CC1101_MCSM0        0x18    /**< Main Radio Control State Machine config */
#define CC1101_FOCCFG       0x19    /**< Frequency Offset Compensation config */
#define CC1101_BSCFG        0x1A    /**< Bit Synchronization configuration */
#define CC1101_AGCCTRL2     0x1B    /**< AGC control */
#define CC1101_AGCCTRL1     0x1C    /**< AGC control */
#define CC1101_AGCCTRL0     0x1D    /**< AGC control */
#define CC1101_WOREVT1      0x1E    /**< High byte Event 0 timeout */
#define CC1101_WOREVT0      0x1F    /**< Low byte Event 0 timeout */
#define CC1101_WORCTRL      0x20    /**< Wake On Radio control */
#define CC1101_FREND1       0x21    /**< Front end RX configuration */
#define CC1101_FREND0       0x22    /**< Front end TX configuration */
#define CC1101_FSCAL3       0x23    /**< Frequency synthesizer calibration */
#define CC1101_FSCAL2       0x24    /**< Frequency synthesizer calibration */
#define CC1101_FSCAL1       0x25    /**< Frequency synthesizer calibration */
#define CC1101_FSCAL0       0x26    /**< Frequency synthesizer calibration */
#define CC1101_TEST2        0x2C    /**< Various test settings */
#define CC1101_TEST1        0x2D    /**< Various test settings */
#define CC1101_TEST0        0x2E    /**< Various test settings */

/* CC1101 Multi-byte / FIFO Registers */
#define CC1101_PATABLE      0x3E    /**< PA power control table */
#define CC1101_FIFO         0x3F    /**< Single / Burst FIFO access */

/* CC1101 Status Registers */
#define CC1101_PARTNUM      (0x30 | 0xC0) /**< Chip ID */
#define CC1101_VERSION      (0x31 | 0xC0) /**< Chip version */
#define CC1101_MARCSTATE    (0x35 | 0xC0) /**< Main Radio Control State Machine state */
#define CC1101_TXBYTES      (0x3A | 0xC0) /**< Underflow and number of bytes */

/**
 * @brief Initialize SPI0 hardware and configure CC1101 registers for 433 MHz, 4.8 kBaud, 2-FSK.
 * @return true if communication succeeded and CC1101 chip detected, false otherwise.
 */
bool cc1101_init(void);

/**
 * @brief Wake CC1101 from SPWD mode with oscillator ready timeout.
 * @return true if crystal stabilized, false on timeout.
 */
bool cc1101_wakeup(void);

/**
 * @brief Transmit a buffer with hardware CRC, whitening, and deterministic completion timeout.
 * Automatically handles wakeup, transmission, and returns radio to deep SPWD sleep.
 *
 * @param packet Pointer to telemetry packet data.
 * @param len Size of packet in bytes.
 * @return true if packet was transmitted successfully, false on timeout or error.
 */
bool cc1101_transmit_packet(const void *packet, uint8_t len);

/**
 * @brief Put CC1101 into ultra-low-power deep sleep (SPWD mode, ~200 nA).
 */
void cc1101_power_down(void);

/**
 * @brief Calculate CCITT-16 CRC for telemetry packet validation.
 *
 * @param data Pointer to input data.
 * @param len Length in bytes.
 * @return Calculated 16-bit CRC.
 */
uint16_t cc1101_calc_crc16(const uint8_t *data, uint16_t len);

#ifdef __cplusplus
}
#endif

#endif /* CC1101_H_ */
