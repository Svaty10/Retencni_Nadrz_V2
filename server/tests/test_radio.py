"""Receiver FIFO handling with a simulated CC1101 (no hardware)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import radio  # noqa: E402


class FakeCC1101:
    """Just enough of the SPI protocol for Receiver._poll_fifo."""

    def __init__(self, state, fifo):
        self.state, self.fifo, self.strobes = state, list(fifo), []

    def xfer2(self, data):
        addr = data[0]
        if len(data) == 1:                                   # command strobe
            self.strobes.append(addr)
            if addr == radio.SFRX:
                self.fifo = []
            return [0]
        if addr == radio.MARCSTATE:
            return [0, self.state]
        if addr == radio.RXBYTES:
            return [0, len(self.fifo)]
        if addr == (radio.FIFO | 0xC0):                      # burst read consumes bytes
            n = len(data) - 1
            out, self.fifo = self.fifo[:n], self.fifo[n:]
            return [0] + out
        return [0, 0]


class PollTest(unittest.TestCase):
    PACKET = [21] + list(range(21)) + [0xD0, 0x80 | 30]       # length, payload, RSSI, LQI|CRC_OK

    def receiver(self, chip):
        frames = []
        rx = radio.Receiver(lambda p, rssi, lqi: frames.append(p))
        rx.spi = chip
        return rx, frames

    def test_packet_in_progress_is_left_alone(self):
        chip = FakeCC1101(radio.MARC_RX, self.PACKET[:10])   # still receiving
        rx, frames = self.receiver(chip)
        rx._poll_fifo()
        self.assertEqual(chip.fifo, self.PACKET[:10])        # nothing read, nothing flushed
        self.assertNotIn(radio.SFRX, chip.strobes)
        self.assertEqual(frames, [])

    def test_finished_packet_is_delivered(self):
        chip = FakeCC1101(radio.MARC_IDLE, self.PACKET)
        rx, frames = self.receiver(chip)
        rx._poll_fifo()
        self.assertEqual(frames, [bytes(range(21))])
        self.assertIn(radio.SRX, chip.strobes)               # back to listening


if __name__ == "__main__":
    unittest.main()
