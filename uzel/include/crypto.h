/**
 * @file crypto.h
 * @brief Small, portable crypto for the sensor node (no dynamic memory, 16-bit-int safe).
 *
 *  - AES-128 encryption (FIPS-197), encrypt direction only
 *  - AES-CMAC (RFC 4493)
 *  - X25519 key agreement (RFC 7748), 8-bit limbs, for pairing only
 *
 * The same algorithms are implemented in server/retencka/crypto.py; both sides are
 * tested against the official test vectors (tools/crypto_selftest.c, server tests).
 */

#ifndef CRYPTO_H_
#define CRYPTO_H_

#include <stdint.h>
#include <stdbool.h>

/** Encrypt one 16-byte block in place-safe manner (out may equal in). */
void aes128_encrypt(const uint8_t key[16], const uint8_t in[16], uint8_t out[16]);

/** AES-CMAC over msg (any length) -> 16-byte tag. */
void aes_cmac(const uint8_t key[16], const uint8_t *msg, uint8_t len, uint8_t tag[16]);

/** Optional hook called periodically during the (slow) X25519 ladder, e.g. to kick a watchdog. */
extern void (*x25519_progress)(void);

/** out = X25519(scalar, point). scalar is clamped internally. */
void x25519(uint8_t out[32], const uint8_t scalar[32], const uint8_t point[32]);

/** out = X25519(scalar, 9) – public key. */
void x25519_base(uint8_t out[32], const uint8_t scalar[32]);

#endif /* CRYPTO_H_ */
