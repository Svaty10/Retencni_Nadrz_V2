"""Flow diagram: the dots run only while water flows right now, not for any flow earlier today."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka.core import Core, FLOWING_WINDOW_S  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.model import Level  # noqa: E402
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


class Outlook(unittest.TestCase):
    """Výhled: spotřeba je změřená, předpověď deště jen posune odhad dopouštění."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        self.core = Core(self.db, Settings(self.db), self.tmp.name)
        self.core.nodes.add(A, "Zahrada")
        self.core.nodes.update(A, {"refill_provider": "hydrawise", "hydrawise_relay_id": "1",
                                   "hydrawise_api_key": "k", "auto_refill_enabled": True,
                                   "refill_threshold_pct": 30, "auto_refill_target_pct": 80,
                                   "catchment_area_m2": 10, "runoff_coef": 1.0})
        self.flows = [{"day": f"2000-01-0{i}", "use": 100, "rain": 0, "refill": 0} for i in range(1, 8)]

    def tearDown(self):
        self.db.close_thread()
        self.tmp.cleanup()

    def out(self, rain_per_day_mm):
        self.core.weather.rain_ahead = lambda h: (rain_per_day_mm * h / 24, None)
        lv = Level(100, 100, 50.0, 1000.0, 2000.0)          # 1000 L z 2000 L, práh 600 L
        return self.core.outlook(self.core.nodes.get(A), lv, self.flows)

    def test_dry_week_predicts_refill(self):
        o = self.out(0)
        self.assertEqual((o["use_per_day_l"], o["days_left"]), (100, 10.0))
        self.assertEqual((o["refill_in_days"], o["refill_l"]), (5, 1000))     # 1000 − 5×100 < 600

    def test_forecast_rain_postpones_refill(self):
        self.assertIsNone(self.out(5)["refill_in_days"])                        # +50 L/den deště
        self.assertEqual(self.out(5)["days_left"], 10.0)                        # výdrž jen ze spotřeby

    def test_without_automatic_refill_no_prediction(self):
        self.core.nodes.update(A, {"auto_refill_enabled": False})
        o = self.out(0)
        self.assertFalse(o["auto_refill"])
        self.assertIsNone(o["refill_in_days"])


if __name__ == "__main__":
    unittest.main()
