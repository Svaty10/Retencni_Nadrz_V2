/* Host / simulator self-test of uzel/src/crypto.c against official test vectors.
 *   cc -I include tools/crypto_selftest.c src/crypto.c -o /tmp/ct && /tmp/ct
 * Prints "OK" when all vectors match. With argument "x25519 <scalar hex> <point hex>"
 * or "cmac <key hex> <msg hex>" it prints the result (used by the Python cross-check). */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "crypto.h"
#include "proto.h"

static void unhex(const char *h, uint8_t *o, int n) { for (int i = 0; i < n; i++) { unsigned v; sscanf(h + 2 * i, "%2x", &v); o[i] = (uint8_t)v; } }
static void phex(const uint8_t *b, int n) { for (int i = 0; i < n; i++) printf("%02x", b[i]); printf("\n"); }
static int eq(const uint8_t *a, const char *hex, int n) { uint8_t b[64]; unhex(hex, b, n); return memcmp(a, b, n) == 0; }

int main(int argc, char **argv)
{
    uint8_t k[32], p[300], o[32];
    if (argc == 4 && !strcmp(argv[1], "x25519")) { unhex(argv[2], k, 32); unhex(argv[3], p, 32); x25519(o, k, p); phex(o, 32); return 0; }
    if (argc == 6 && !strcmp(argv[1], "data")) {   /* data <shared hex> <node_id> <ctr> <telemetry hex> */
        uint8_t ke[16], km[16], tl[10], pkt[21];
        unhex(argv[2], k, 32); unhex(argv[5], tl, 10);
        proto_derive_keys(k, ke, km);
        proto_build_data(pkt, ke, km, (uint16_t)strtoul(argv[3], 0, 16), (uint32_t)strtoul(argv[4], 0, 10), tl);
        phex(pkt, 21); return 0;
    }
    if (argc == 5 && !strcmp(argv[1], "unpair")) {   /* unpair <k_mac hex> <node_id> <pub hex>: build tag via CMAC and self-check */
        uint8_t km[16], pub[32], pkt[19], msg[35];
        uint16_t id = (uint16_t)strtoul(argv[3], 0, 16);
        unhex(argv[2], km, 16); unhex(argv[4], pub, 32);
        msg[0] = 0x06; msg[1] = (uint8_t)id; msg[2] = (uint8_t)(id >> 8); memcpy(&msg[3], pub, 32);
        pkt[0] = 0x06; pkt[1] = msg[1]; pkt[2] = msg[2]; aes_cmac(km, msg, 35, &pkt[3]);
        if (!proto_check_unpair(pkt, 19, km, id, pub)) { printf("FAIL\n"); return 1; }
        pkt[18] ^= 1;
        if (proto_check_unpair(pkt, 19, km, id, pub)) { printf("FAIL\n"); return 1; }
        pkt[18] ^= 1;
        phex(pkt, 19); return 0;
    }
    if (argc == 4 && !strcmp(argv[1], "cmac")) { int n = (int)strlen(argv[3]) / 2; unhex(argv[2], k, 16); unhex(argv[3], p, n); aes_cmac(k, p, (uint8_t)n, o); phex(o, 16); return 0; }
    int ok = 1;
    /* FIPS-197 C.1 */
    unhex("000102030405060708090a0b0c0d0e0f", k, 16); unhex("00112233445566778899aabbccddeeff", p, 16);
    aes128_encrypt(k, p, o); ok &= eq(o, "69c4e0d86a7b0430d8cdb78070b4c55a", 16);
    /* RFC 4493 */
    unhex("2b7e151628aed2a6abf7158809cf4f3c", k, 16);
    aes_cmac(k, p, 0, o); ok &= eq(o, "bb1d6929e95937287fa37d129b756746", 16);
    unhex("6bc1bee22e409f96e93d7e117393172a", p, 16);
    aes_cmac(k, p, 16, o); ok &= eq(o, "070a16b46b4d4144f79bdd9dd04a287c", 16);
    unhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e5130c81c46a35ce411", p, 40);
    aes_cmac(k, p, 40, o); ok &= eq(o, "dfa66747de9ae63030ca32611497c827", 16);
    /* RFC 7748 5.2 */
    unhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4", k, 32);
    unhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c", p, 32);
    x25519(o, k, p); ok &= eq(o, "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552", 32);
    /* RFC 7748 6.1 (Alice public key) */
    unhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a", k, 32);
    x25519_base(o, k); ok &= eq(o, "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a", 32);
    printf(ok ? "OK\n" : "FAIL\n");
    return ok ? 0 : 1;
}
