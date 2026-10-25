import math
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

    def test_refill_follows_raw_without_lag(self):
        # 19 mm per 5 min is below 2x dead band: smoothing alone would lag ~40 mm
        f = model.LevelFilter()
        for _ in range(5):
            f.update(184, 10)
        level, series = 184.0, []
        for i in range(12):
            level += 19
            series.append((i * 300, f.update(level, 10, follow=True) * 10.9, 1))
            self.assertEqual(series[-1][1], level * 10.9)    # control sees the real level
        for i in range(12, 24):                              # valve closed, level flat
            series.append((i * 300, f.update(level, 10) * 10.9, 0))
        rain = [x for x in model.flows(series, 54) if x[1] == "rain"]
        self.assertEqual(rain, [])                           # no fake inflow after the refill


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

    def test_battery_alkaline(self):
        # alkaline: half capacity at ~1.30 V/cell, where the Li-FeS2 curve would say "low"
        self.assertAlmostEqual(model.battery_percent(3.9, kind="alkaline"), 50.0)
        self.assertLess(model.battery_percent(3.9), model.BATTERY_LOW_PCT + 5)
        self.assertEqual(model.battery_percent(4.8, kind="alkaline"), 100)
        self.assertEqual(model.battery_percent(3.0, kind="alkaline"), 0)
        vals = [model.battery_percent(v / 100, kind="alkaline") for v in range(280, 500, 5)]
        self.assertEqual(vals, sorted(vals))
        s = {"battery_capacity_mah": 3000.0, "battery_capacity_alk_mah": 2500.0, "node_cycle_mAs": 3.3,
             "node_sleep_uA": 1.5}
        li = model.battery_days_left(100, 288, s)
        alk = model.battery_days_left(100, 288, s, kind="alkaline")
        self.assertGreater(alk, 365)                    # a year easily
        self.assertLess(alk, li)                        # smaller capacity, more self-discharge


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


class TankShapeTest(unittest.TestCase):
    def test_cyl_h_volume_and_percent(self):
        # horizontal cylinder: diameter 200 cm (radius 100 cm), length 300 cm
        s = {"tank_shape": "cyl_h", "tank_diameter_cm": 200.0, "tank_length_cm": 300.0,
             "max_depth_cm": 92.0, "sensor_offset_cm": 0.0, "cal_points": [[0, 0], [1000, 100]],
             "probe_accuracy_pct": 0.5, "probe_range_cm": 200.0}
        self.assertEqual(model.tank_height_cm(s), 200.0)
        max_vol = math.pi * (100.0 ** 2) * 300.0 / 1000.0
        self.assertAlmostEqual(model.max_volume_l(s), max_vol)

        # empty
        lv0 = model.level_from_mm(0, s)
        self.assertAlmostEqual(lv0.volume_l, 0.0)
        self.assertAlmostEqual(lv0.percent, 0.0)

        # half-full (100 cm height = 50% volume)
        lv_mid = model.level_from_mm(1000, s)
        self.assertAlmostEqual(lv_mid.level_cm, 100.0)
        self.assertAlmostEqual(lv_mid.volume_l, max_vol / 2.0)
        self.assertAlmostEqual(lv_mid.percent, 50.0)

        # quarter height (50 cm height) -> non-linear volume (< 25% due to curvature)
        lv_q = model.level_from_mm(500, s)
        self.assertAlmostEqual(lv_q.level_cm, 50.0)
        self.assertLess(lv_q.percent, 25.0)
        self.assertAlmostEqual(lv_q.percent, (lv_q.volume_l / max_vol) * 100.0)

        # full (200 cm)
        lv_full = model.level_from_mm(2000, s)
        self.assertAlmostEqual(lv_full.volume_l, max_vol)
        self.assertAlmostEqual(lv_full.percent, 100.0)

        # deadband at mid-height: surface area = 2.0 m * 3.0 m = 6.0 m2; deadband_mm = 10 mm -> 60 L
        self.assertAlmostEqual(model.deadband_l(s), 60.0)

    def test_cyl_v_volume_and_percent(self):
        # vertical cylinder: diameter 200 cm (radius 1.0 m), max_depth 150 cm
        s = {"tank_shape": "cyl_v", "tank_diameter_cm": 200.0, "max_depth_cm": 150.0,
             "sensor_offset_cm": 0.0, "cal_points": [[0, 0], [1000, 100]],
             "probe_accuracy_pct": 0.5, "probe_range_cm": 200.0}
        self.assertEqual(model.tank_height_cm(s), 150.0)
        area_m2 = math.pi * (1.0 ** 2)
        max_vol = area_m2 * 150.0 * 10.0
        self.assertAlmostEqual(model.max_volume_l(s), max_vol)

        lv = model.level_from_mm(750, s)    # 75 cm = 50%
        self.assertAlmostEqual(lv.volume_l, max_vol / 2.0)
        self.assertAlmostEqual(lv.percent, 50.0)
        self.assertAlmostEqual(model.deadband_l(s), 1.0 * area_m2 * 10.0)

    def test_rect_volume_and_percent(self):
        # rectangular: length 300 cm, width 200 cm, max_depth 150 cm
        s = {"tank_shape": "rect", "tank_length_cm": 300.0, "tank_width_cm": 200.0,
             "max_depth_cm": 150.0, "sensor_offset_cm": 0.0, "cal_points": [[0, 0], [1000, 100]],
             "probe_accuracy_pct": 0.5, "probe_range_cm": 200.0}
        self.assertEqual(model.tank_height_cm(s), 150.0)
        area_m2 = 3.0 * 2.0
        max_vol = area_m2 * 150.0 * 10.0
        self.assertAlmostEqual(model.max_volume_l(s), 9000.0)

        lv = model.level_from_mm(500, s)    # 50 cm = 1/3 height & volume
        self.assertAlmostEqual(lv.volume_l, 3000.0)
        self.assertAlmostEqual(lv.percent, 100.0 / 3.0)
        self.assertAlmostEqual(model.deadband_l(s), 60.0)

    def test_zero_dimensions(self):
        s = {"tank_shape": "area", "tank_area_m2": 0.0, "max_depth_cm": 0.0,
             "sensor_offset_cm": 0.0, "cal_points": [[0, 0], [1000, 100]],
             "probe_accuracy_pct": 0.5, "probe_range_cm": 200.0}
        self.assertEqual(model.tank_height_cm(s), 0.0)
        self.assertEqual(model.max_volume_l(s), 0.0)
        lv = model.compute_level(5000, s)
        self.assertEqual(lv.volume_l, 0.0)
        self.assertEqual(lv.percent, 0.0)
        self.assertEqual(model.deadband_l(s), 0.0)

        s_cyl = {"tank_shape": "cyl_h", "tank_diameter_cm": 0.0, "tank_length_cm": 0.0,
                 "sensor_offset_cm": 0.0, "cal_points": [[0, 0], [1000, 100]],
                 "probe_accuracy_pct": 0.5, "probe_range_cm": 200.0}
        self.assertEqual(model.tank_height_cm(s_cyl), 0.0)
        self.assertEqual(model.max_volume_l(s_cyl), 0.0)
        lv_cyl = model.compute_level(5000, s_cyl)
        self.assertEqual(lv_cyl.volume_l, 0.0)
        self.assertEqual(lv_cyl.percent, 0.0)
        self.assertEqual(model.deadband_l(s_cyl), 0.0)


if __name__ == "__main__":
    unittest.main()
