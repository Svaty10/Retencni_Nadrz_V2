"""Diagnostic byte, battery drop under TX load and duplicate important packets."""
import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import crypto, model  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.settings import Settings  # noqa: E402

NID = 0x4444
KE, KM = bytes(range(16)), bytes(range(16, 32))


class DiagTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Database(os.path.join(self.tmp.name, "t.db"))
        self.core = Core(db, Settings(db), self.tmp.name)
        self.core.nodes.add(NID, "T")
        self.core.nodes.set_keys(NID, KE, KM)
        self.core._last_ctr[NID] = -1

        class R:                                    # radio counters only
            replays = duplicates = auth_failures = foreign_packets = 0
        self.core.radio = R()

    def tearDown(self):
        self.tmp.cleanup()

    def frame(self, ctr, flags=0, vbat=4900, diag=0, sag=0xFF):
        tel = crypto.build_telemetry(flags, vbat, 1500, 9000, 12000, diag=diag, sag=sag)
        return crypto.build_data(KE, KM, NID, ctr, tel)

    def feed(self, *frames):
        async def run():
            for f in frames:
                self.core._on_frame(f, -80.0, 30)
            await asyncio.sleep(0)
        asyncio.run(run())

    def test_duplicate_is_dropped_not_a_replay(self):
        f = self.frame(7, flags=model.FLAG_BOOT)
        self.feed(f, f)                              # important packet sent twice
        self.assertEqual(self.core.db.scalar("SELECT COUNT(*) FROM measurements"), 1)
        self.assertEqual(self.core.radio.duplicates, 1)
        self.assertEqual(self.core.radio.replays, 0)
        self.feed(self.frame(5))                     # older counter = replay
        self.assertEqual(self.core.radio.replays, 1)

    def test_diag_and_sag_stored(self):
        self.feed(self.frame(1, diag=model.DIAG_HEATED | 2, sag=9))
        r = self.core.db.one("SELECT diag, vbat_sag_mv FROM measurements")
        self.assertEqual((r["diag"], r["vbat_sag_mv"]), (model.DIAG_HEATED | 2, 45))
        lq = self.core.link_quality(NID)
        self.assertEqual(lq["busy_pct"], 100.0)
        self.assertGreater(self.core._heats_per_day(NID), 0)

    def test_bod_reset_audited(self):
        self.feed(self.frame(1, flags=model.FLAG_BOOT, diag=model.DIAG_BOD_RESET))
        detail = self.core.db.scalar("SELECT detail FROM audit WHERE action='NODE_BOOT'")
        self.assertIn("pokles napětí", detail)

    def test_off_detected_under_load(self):
        # 3.04 V at rest, 60 mV drop under TX load -> 2.98 V: the node switched off
        self.feed(self.frame(1, flags=model.FLAG_BAT_LOW, vbat=3040, sag=12))
        self.core.last[NID]["ts"] -= 3600
        b = self.core.battery(NID)
        self.assertTrue(b["off"])
        self.assertEqual(b["sag_mv"], 60)

    def test_heater_in_battery_model(self):
        s = self.core.settings.all()
        base = model.battery_days_left(80, 288, s)
        heated = model.battery_days_left(80, 288, s, heats_per_day=4)
        self.assertLess(heated, base)
        self.assertGreater(heated, base * 0.8)       # 4 heatings a day cost < 20 %


if __name__ == "__main__":
    unittest.main()
