import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import crypto as C  # noqa: E402


class VectorTest(unittest.TestCase):
    def test_aes_fips197(self):
        self.assertEqual(C.aes128_encrypt(bytes.fromhex("000102030405060708090a0b0c0d0e0f"),
                                          bytes.fromhex("00112233445566778899aabbccddeeff")).hex(),
                         "69c4e0d86a7b0430d8cdb78070b4c55a")

    def test_cmac_rfc4493(self):
        k = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        self.assertEqual(C.aes_cmac(k, b"").hex(), "bb1d6929e95937287fa37d129b756746")
        self.assertEqual(C.aes_cmac(k, bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")).hex(),
                         "070a16b46b4d4144f79bdd9dd04a287c")
        m = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e5130c81c46a35ce411")
        self.assertEqual(C.aes_cmac(k, m).hex(), "dfa66747de9ae63030ca32611497c827")

    def test_x25519_rfc7748(self):
        self.assertEqual(C.x25519(
            bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4"),
            bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")).hex(),
            "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")
        a = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        b = bytes.fromhex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
        self.assertEqual(C.x25519(a, C.x25519_base(b)), C.x25519(b, C.x25519_base(a)))

    def test_firmware_packet(self):
        # produced by uzel/tools/crypto_selftest.c: data 11..11 BEEF 5 00112233445566778899
        ke, km = C.derive_keys(bytes([0x11] * 32))
        pkt = C.build_data(ke, km, 0xBEEF, 5, bytes.fromhex("00112233445566778899"))
        self.assertEqual(pkt.hex(), "03efbe0500000073df7627fc3818aaf2a910eb79b2")

    def test_unpair(self):
        # produced by uzel/tools/crypto_selftest.c: unpair 0011..eeff BEEF abab..ab
        km, pub = bytes.fromhex("00112233445566778899aabbccddeeff"), bytes([0xab]) * 32
        pkt = C.build_unpair(km, 0xBEEF, pub)
        self.assertEqual(pkt.hex(), "06efbed25f9fde0a93a0cfd59c24881c7fe710")
        self.assertTrue(C.check_unpair(km, 0xBEEF, pub, pkt))
        self.assertFalse(C.check_unpair(os.urandom(16), 0xBEEF, pub, pkt))       # foreign hub
        self.assertFalse(C.check_unpair(km, 0xBEEF, bytes(32), pkt))             # replay, other boot
        req = C.build_pair_req(0xBEEF, pub, True)
        self.assertEqual(C.parse_pair_req(req), (0xBEEF, pub, True))

    def test_open_and_tamper(self):
        ke, km = os.urandom(16), os.urandom(16)
        tel = C.build_telemetry(0x20, 4950, -150, 8000, 12400)
        pkt = C.build_data(ke, km, 0x1234, 77, tel)
        self.assertEqual(C.open_data(ke, km, pkt), (0x20, 4950, -150, 8000, 12400, 31))
        for i in range(21):
            bad = bytearray(pkt)
            bad[i] ^= 0x01
            self.assertIsNone(C.open_data(ke, km, bytes(bad)), i)
        self.assertIsNone(C.open_data(os.urandom(16), km, pkt[:20] + b"x"))
        self.assertIsNone(C.derive_keys(bytes(32)))


if __name__ == "__main__":
    unittest.main()
