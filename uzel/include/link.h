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

/**
 * @brief One pairing attempt: send PAIR_REQ and listen PAIR_RX_WINDOW_MS for PAIR_ACK.
 * The X25519 key pair is generated on the first call after boot (~10 s of CPU at 10 MHz).
 * @return true if the node got (new) keys and stored them.
 */
bool link_pair_attempt(void);

/** Encrypt, authenticate and send one telemetry record. */
bool link_send_data(const telemetry_t *t);

#endif /* LINK_H_ */
