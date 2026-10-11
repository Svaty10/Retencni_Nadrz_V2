/**
 * @file proto.c
 * @brief Protocol v3 packet construction (see config.h for the layout).
 */
#include "proto.h"
#include "crypto.h"
#include <string.h>

bool proto_derive_keys(const uint8_t shared[32], uint8_t k_enc[16], uint8_t k_mac[16])
{
    static const uint8_t lbl_enc[16] = "RTK-ENC-KEY";
    static const uint8_t lbl_mac[16] = "RTK-MAC-KEY";
    uint8_t k0[16], acc = 0;
    for (uint8_t i = 0; i < 16; i++) {
        k0[i] = shared[i] ^ shared[16 + i];
        acc |= shared[i] | shared[16 + i];
    }
    if (acc == 0) {
        return false;                       /* low-order point – reject */
    }
    aes128_encrypt(k0, lbl_enc, k_enc);
    aes128_encrypt(k0, lbl_mac, k_mac);
    return true;
}

void proto_build_data(uint8_t p[21], const uint8_t k_enc[16], const uint8_t k_mac[16],
                      uint16_t node_id, uint32_t ctr, const uint8_t telemetry[10])
{
    uint8_t blk[16], ks[16], tag[16];

    p[0] = 0x03;
    p[1] = (uint8_t)node_id;
    p[2] = (uint8_t)(node_id >> 8);
    p[3] = (uint8_t)ctr;
    p[4] = (uint8_t)(ctr >> 8);
    p[5] = (uint8_t)(ctr >> 16);
    p[6] = (uint8_t)(ctr >> 24);

    memset(blk, 0, 16);
    memcpy(blk, &p[1], 6);                  /* nonce = node_id | ctr | 0.. */
    aes128_encrypt(k_enc, blk, ks);
    for (uint8_t i = 0; i < 10; i++) {
        p[7 + i] = telemetry[i] ^ ks[i];
    }
    aes_cmac(k_mac, p, 17, tag);
    memcpy(&p[17], tag, 4);
}
