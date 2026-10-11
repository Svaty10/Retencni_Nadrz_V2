/**
 * @file proto.h
 * @brief Pure (hardware independent) packet functions of protocol v3 – shared by the
 *        firmware and the host interoperability test.
 */
#ifndef PROTO_H_
#define PROTO_H_

#include <stdint.h>
#include <stdbool.h>

/** Kenc/Kmac from the X25519 shared secret. Returns false for an all-zero secret. */
bool proto_derive_keys(const uint8_t shared[32], uint8_t k_enc[16], uint8_t k_mac[16]);

/** Build a 21-byte DATA packet from a 10-byte telemetry record. */
void proto_build_data(uint8_t out[21], const uint8_t k_enc[16], const uint8_t k_mac[16],
                      uint16_t node_id, uint32_t ctr, const uint8_t telemetry[10]);

#endif /* PROTO_H_ */
