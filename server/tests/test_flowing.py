"""Flow diagram: the dots run only while water flows right now, not for any flow earlier today."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka.core import Core, FLOWING_WINDOW_S  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.settings import Settings  # noqa: E402

A = 0x0A01
NOW = 1_760_000_000


class FlowingNow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        self.core = Core(self.db, Settings(self.db), self.tmp.name)
        self.core.nodes.add(A, "Zahrada")

    def tearDown(self):
        self.db.close_thread()
        self.tmp.cleanup()

    def series(self, vols, step=300):
        """Volumes every `step` s, the last one at NOW."""
        with self.db.transaction() as c:
            for i, v in enumerate(vols):
                ts = NOW - (len(vols) - 1 - i) * step
                c.execute("INSERT INTO measurements(ts, node_id, seq, flags, level_mm, volume_l, refilling) "
                          "VALUES(?,?,?,0,500,?,0)", (ts, A, i, v))

    def test_calm_level_is_not_flowing(self):
        self.series([5000] * 12)
        self.assertEqual(self.core.flowing_now(A, NOW), {"rain": False, "use": False})

    def test_consumption_just_now(self):
        self.series([5000] * 10 + [4900, 4800])
        self.assertEqual(self.core.flowing_now(A, NOW), {"rain": False, "use": True})

    def test_rain_just_now(self):
        self.series([5000] * 10 + [5200, 5400])
        self.assertEqual(self.core.flowing_now(A, NOW), {"rain": True, "use": False})

    def test_flow_earlier_today_is_over(self):
        # consumption 45 min ago, calm since then
        self.series([5000] * 3 + [4800] * 10)
        self.assertGreater(9 * 300, FLOWING_WINDOW_S)
        self.assertEqual(self.core.flowing_now(A, NOW), {"rain": False, "use": False})


if __name__ == "__main__":
    unittest.main()
