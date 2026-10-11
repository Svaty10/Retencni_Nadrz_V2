import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import model  # noqa: E402


class LevelTest(unittest.TestCase):
    S = {"cal_points": [[500, 0], [2500, 100]], "max_depth_cm": 150, "sensor_offset_cm": 2,
         "tank_area_m2": 10}

    def test_interpolation_and_extrapolation(self):
        pts = [[500, 0], [1500, 50], [2500, 100]]
        self.assertAlmostEqual(model.column_cm(1000, pts), 25)
        self.assertAlmostEqual(model.column_cm(3000, pts), 125)
        self.assertAlmostEqual(model.column_cm(400, pts), -5)

    def test_level_volume(self):
        lv = model.compute_level(15000, self.S)        # 1500 mV -> 50 cm + 2 offset
        self.assertAlmostEqual(lv.level_cm, 52)
        self.assertAlmostEqual(lv.volume_l, 5200)
        self.assertAlmostEqual(lv.percent, 52 / 150 * 100)

    def test_clamped(self):
        self.assertEqual(model.compute_level(0, self.S).level_cm, 0)
        self.assertEqual(model.compute_level(60000, self.S).level_cm, 150)


class FilterTest(unittest.TestCase):
    def test_noise_smoothed_jump_followed(self):
        f = model.LevelFilter()
        out = [f.update(500 + (8 if i % 2 else -8), 10) for i in range(30)]
        self.assertLess(max(out[10:]) - min(out[10:]), 8)   # noise strongly reduced
        f.update(700, 10); f.update(700, 10)
        self.assertEqual(f.update(700, 10), 700)             # real change passes at once (median of 3)

    def test_spike_removed(self):
        f = model.LevelFilter()
        for _ in range(5):
            f.update(500, 10)
        self.assertLess(abs(f.update(900, 10) - 500), 1)


class FlowTest(unittest.TestCase):
    def test_slow_consumption_counted(self):
        # 10 L per sample is below the 20 L dead band, but must still be counted
        series = [(i, 1000 - 10 * i, 0) for i in range(11)]
        used = sum(l for _, k, l in model.flows(series, 20) if k == "use")
        self.assertGreaterEqual(used, 80)

    def test_noise_ignored(self):
        series = [(i, 1000 + (5 if i % 2 else -5), 0) for i in range(50)]
        self.assertEqual(list(model.flows(series, 20)), [])

    def test_refill_vs_rain(self):
        kinds = {k for _, k, _ in model.flows([(0, 100, 0), (1, 300, 1), (2, 500, 0)], 10)}
        self.assertEqual(kinds, {"refill", "rain"})


class BatteryTest(unittest.TestCase):
    def test_empty_at_3v(self):
        self.assertEqual(model.battery_percent(3.0), 0)
        self.assertAlmostEqual(model.battery_percent(3.3), 1.0)

    def test_percent_monotonic(self):
        vals = [model.battery_percent(v / 100) for v in range(280, 520, 5)]
        self.assertEqual(vals, sorted(vals))
        self.assertEqual(model.battery_percent(5.4), 100)
        self.assertEqual(model.battery_percent(2.9), 0)


class InflowTest(unittest.TestCase):
    def test_rain_interval(self):
        # flat 1000 L, rain from t=600 to t=1500 (+300 L), consumption, refill is ignored
        ser = [(0, 1000, 0), (300, 1000, 0), (600, 1001, 0), (900, 1100, 0), (1200, 1250, 0), (1500, 1300, 0),
               (1800, 1300, 0), (3600, 1290, 0), (7200, 1290, 1), (7500, 1600, 1), (7800, 1600, 0)]
        iv = model.inflow_intervals(ser, 20.0)
        self.assertEqual(len(iv), 1)
        a, b, litres = iv[0]
        self.assertEqual((a, b), (600, 1500))
        self.assertAlmostEqual(litres, 300, delta=2)

    def test_noise_is_not_rain(self):
        ser = [(i * 300, 1000 + (5 if i % 2 else -5), 0) for i in range(50)]
        self.assertEqual(model.inflow_intervals(ser, 20.0), [])


if __name__ == "__main__":
    unittest.main()
