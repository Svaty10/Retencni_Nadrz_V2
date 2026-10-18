/**
 * @file link.h
 * @brief Secure radio link: EEPROM key store, frame counter, entropy, pairing, DATA packets.
 */

#ifndef LINK_H_
#define LINK_H_

#include "config.h"

/** Load keys and frame counter from EEPROM. Returns true if the node is paired. */
bool link_init(uint16_t node_id);

bool link_paired(void);

typedef enum {
    LINK_PAIR_NONE = 0,     /**< no (valid) answer */
    LINK_PAIR_PAIRED,       /**< unpaired node got keys from a PAIR_ACK and stored them */
    LINK_PAIR_UNPAIRED      /**< paired node got a valid UNPAIR from its hub: key erased */
} link_pair_t;

/**
 * @brief One pairing exchange: send PAIR_REQ and listen PAIR_RX_WINDOW_MS for the answer.
 *
 * A paired node accepts ONLY an UNPAIR authenticated with its current key (its own
 * hub asked for it) – a foreign PAIR_ACK is ignored, so the sensor cannot be taken
 * over by another hub. An unpaired node accepts a PAIR_ACK.
 * The X25519 key pair is generated on the first call after boot (~9 s of CPU at 10 MHz, ~18 s at 5 MHz);
 * its public key also serves as the fresh nonce of the UNPAIR authentication.
 */
link_pair_t link_pair_attempt(void);

/** Encrypt, authenticate and send one telemetry record. */
bool link_send_data(const telemetry_t *t);

/** Allow 10 MHz for X25519 (battery healthy, VCC > 2.7 V); otherwise 5 MHz is used. */
void link_set_fast_clock(bool ok);

#endif /* LINK_H_ */
