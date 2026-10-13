"""Pure-Python crypto matching the sensor firmware (uzel/src/crypto.c, proto.c).

AES-128 (encrypt only), AES-CMAC (RFC 4493), X25519 (RFC 7748) and the radio
protocol v3 packet format. No third-party packages – speed is irrelevant here
(one packet per sensor every few minutes, one key exchange per pairing).
"""

from __future__ import annotations

import os
import struct
from typing import Optional

# ============================================================== AES-128
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")


def _xtime(x: int) -> int:
    x <<= 1
    return (x ^ 0x11B) if x & 0x100 else x


def _expand(key: bytes) -> list[list[int]]:
    rks = [list(key)]
    rcon = 1
    for _ in range(10):
        p = rks[-1]
        t = [_SBOX[p[13]] ^ rcon, _SBOX[p[14]], _SBOX[p[15]], _SBOX[p[12]]]
        n = [0] * 16
        for i in range(4):
            n[i] = p[i] ^ t[i]
        for i in range(4, 16):
            n[i] = p[i] ^ n[i - 4]
        rks.append(n)
        rcon = _xtime(rcon)
    return rks


def aes128_encrypt(key: bytes, block: bytes) -> bytes:
    assert len(key) == 16 and len(block) == 16
    rks = _expand(key)
    s = [b ^ k for b, k in zip(block, rks[0])]
    for rnd in range(1, 11):
        t = [_SBOX[s[((c + r) & 3) * 4 + r]] for c in range(4) for r in range(4)]
        if rnd != 10:
            for c in range(4):
                a0, a1, a2, a3 = t[c * 4:c * 4 + 4]
                al = a0 ^ a1 ^ a2 ^ a3
                t[c * 4 + 0] = a0 ^ al ^ _xtime(a0 ^ a1)
                t[c * 4 + 1] = a1 ^ al ^ _xtime(a1 ^ a2)
                t[c * 4 + 2] = a2 ^ al ^ _xtime(a2 ^ a3)
                t[c * 4 + 3] = a3 ^ al ^ _xtime(a3 ^ a0)
        s = [b ^ k for b, k in zip(t, rks[rnd])]
    return bytes(s)


def _dbl(b: bytes) -> bytes:
    v = int.from_bytes(b, "big") << 1
    if v >> 128:
        v = (v & ((1 << 128) - 1)) ^ 0x87
    return v.to_bytes(16, "big")


def aes_cmac(key: bytes, msg: bytes) -> bytes:
    k1 = _dbl(aes128_encrypt(key, bytes(16)))
    k2 = _dbl(k1)
    n = max(1, (len(msg) + 15) // 16)
    complete = len(msg) > 0 and len(msg) % 16 == 0
    last = msg[(n - 1) * 16:]
    if complete:
        last = bytes(a ^ b for a, b in zip(last, k1))
    else:
        last = last + b"\x80" + bytes(15 - len(last))
        last = bytes(a ^ b for a, b in zip(last, k2))
    x = bytes(16)
    for i in range(n - 1):
        x = aes128_encrypt(key, bytes(a ^ b for a, b in zip(x, msg[i * 16:(i + 1) * 16])))
    return aes128_encrypt(key, bytes(a ^ b for a, b in zip(x, last)))


# ================================================================ X25519
_P = 2 ** 255 - 19
_A24 = 121665


def x25519(scalar: bytes, point: bytes) -> bytes:
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k_int = int.from_bytes(k, "little")
    x1 = int.from_bytes(point, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        kt = (k_int >> t) & 1
        swap ^= kt
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = kt
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def x25519_base(scalar: bytes) -> bytes:
    return x25519(scalar, (9).to_bytes(32, "little"))


def x25519_keypair() -> tuple[bytes, bytes]:
    priv = os.urandom(32)
    return priv, x25519_base(priv)


# ======================================================== protocol v3
PKT_DATA, PKT_PAIR_REQ, PKT_PAIR_ACK, PKT_UNPAIR = 0x03, 0x04, 0x05, 0x06
DATA_LEN, PAIR_LEN, MAC_LEN = 21, 35, 4
PAIR_REQ_LEN, UNPAIR_LEN = 36, 19
REQ_PAIRED = 0x01                           # PAIR_REQ state bit: node holds a key
_TELEMETRY = struct.Struct("<BHhHHB")       # flags vbat temp hum probe fw


def derive_keys(shared: bytes) -> Optional[tuple[bytes, bytes]]:
    k0 = bytes(a ^ b for a, b in zip(shared[:16], shared[16:]))
    if not any(shared):
        return None
    return (aes128_encrypt(k0, b"RTK-ENC-KEY".ljust(16, b"\0")),
            aes128_encrypt(k0, b"RTK-MAC-KEY".ljust(16, b"\0")))


def _keystream(k_enc: bytes, header6: bytes) -> bytes:
    return aes128_encrypt(k_enc, header6 + bytes(10))


def build_data(k_enc: bytes, k_mac: bytes, node_id: int, ctr: int, telemetry: bytes) -> bytes:
    hdr = bytes([PKT_DATA]) + struct.pack("<HI", node_id, ctr)
    ks = _keystream(k_enc, hdr[1:7])
    body = hdr + bytes(a ^ b for a, b in zip(telemetry, ks[:10]))
    return body + aes_cmac(k_mac, body)[:MAC_LEN]


def data_header(pkt: bytes) -> Optional[tuple[int, int]]:
    """(node_id, ctr) of a DATA packet, without verification."""
    if len(pkt) != DATA_LEN or pkt[0] != PKT_DATA:
        return None
    return struct.unpack_from("<HI", pkt, 1)


def open_data(k_enc: bytes, k_mac: bytes, pkt: bytes) -> Optional[tuple]:
    """Verify + decrypt. Returns (flags, vbat, temp, hum, probe, fw) or None."""
    if len(pkt) != DATA_LEN or pkt[0] != PKT_DATA:
        return None
    tag = aes_cmac(k_mac, pkt[:17])[:MAC_LEN]
    if not _consteq(tag, pkt[17:21]):
        return None
    ks = _keystream(k_enc, pkt[1:7])
    plain = bytes(a ^ b for a, b in zip(pkt[7:17], ks[:10]))
    return _TELEMETRY.unpack(plain)


def build_telemetry(flags: int, vbat: int, temp: int, hum: int, probe: int, fw: int = 10) -> bytes:
    return _TELEMETRY.pack(flags, vbat, temp, hum, probe, fw)


def build_pair_ack(node_id: int, hub_pub: bytes) -> bytes:
    return bytes([PKT_PAIR_ACK]) + struct.pack("<H", node_id) + hub_pub


def build_pair_req(node_id: int, node_pub: bytes, paired: bool) -> bytes:
    return (bytes([PKT_PAIR_REQ]) + struct.pack("<H", node_id) + node_pub
            + bytes([REQ_PAIRED if paired else 0]))


def parse_pair_req(pkt: bytes) -> Optional[tuple[int, bytes, bool]]:
    """(node_id, node public key, node is paired) of a PAIR_REQ."""
    if len(pkt) != PAIR_REQ_LEN or pkt[0] != PKT_PAIR_REQ:
        return None
    return struct.unpack_from("<H", pkt, 1)[0], bytes(pkt[3:35]), bool(pkt[35] & REQ_PAIRED)


def unpair_tag(k_mac: bytes, node_id: int, node_pub: bytes) -> bytes:
    """Authorises the node to forget its key. Bound to the fresh public key of this
    boot (sent in the PAIR_REQ), so a recorded UNPAIR can never be replayed."""
    return aes_cmac(k_mac, bytes([PKT_UNPAIR]) + struct.pack("<H", node_id) + node_pub)


def build_unpair(k_mac: bytes, node_id: int, node_pub: bytes) -> bytes:
    return bytes([PKT_UNPAIR]) + struct.pack("<H", node_id) + unpair_tag(k_mac, node_id, node_pub)


def check_unpair(k_mac: bytes, node_id: int, node_pub: bytes, pkt: bytes) -> bool:
    if len(pkt) != UNPAIR_LEN or pkt[0] != PKT_UNPAIR or struct.unpack_from("<H", pkt, 1)[0] != node_id:
        return False
    return _consteq(unpair_tag(k_mac, node_id, node_pub), pkt[3:19])


def _consteq(a: bytes, b: bytes) -> bool:
    r = len(a) ^ len(b)
    for x, y in zip(a, b):
        r |= x ^ y
    return r == 0
