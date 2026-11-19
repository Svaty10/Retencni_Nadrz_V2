"""Tank shape keys: volume table type, overflow / dome / cone validation, node order."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka.db import Database  # noqa: E402
from retencka.settings import NODE_SCHEMA, Nodes, SettingsError, coerce  # noqa: E402


class TableType(unittest.TestCase):
    def test_table_rejects_single_point(self):
        with self.assertRaisesRegex(SettingsError, "aspoň 2 body"):
            coerce(NODE_SCHEMA, "tank_table", [[0, 0]])

    def test_table_rejects_falling_volume(self):
        with self.assertRaisesRegex(SettingsError, "musí s každým řádkem růst"):
            coerce(NODE_SCHEMA, "tank_table", [[0, 0], [100, 500], [150, 400]])

    def test_table_sorted(self):
        self.assertEqual(coerce(NODE_SCHEMA, "tank_table", [[100, 1000], [0, 0]]), [[0.0, 0.0], [100.0, 1000.0]])

    def test_table_empty_allowed(self):
        self.assertEqual(coerce(NODE_SCHEMA, "tank_table", []), [])


class TankValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        self.nodes = Nodes(self.db)
        self.nodes.add(0xA, "Zahrada")
        self.nodes.add(0xB, "Garáž")

    def tearDown(self):
        self.db.close_thread()
        self.tmp.cleanup()

    def test_old_config_still_valid(self):
        n = self.nodes.get(0xA)
        self.assertEqual(n["overflow_cm"], 0.0)
        self.assertEqual(n["tank_table"], [])
        self.nodes.update(0xA, {"tank_shape": "cyl_v", "tank_diameter_cm": 100, "max_depth_cm": 150})

    def test_overflow_above_height_rejected(self):
        with self.assertRaisesRegex(SettingsError, r"Přepad nemůže být výš než nádrž \(200 cm\)"):
            self.nodes.update(0xA, {"tank_shape": "cyl_h", "tank_diameter_cm": 200, "tank_length_cm": 300,
                                    "overflow_cm": 210})
        self.assertEqual(self.nodes.get(0xA)["tank_shape"], "area")      # nothing stored

    def test_overflow_ok(self):
        self.nodes.update(0xA, {"tank_shape": "cyl_h", "tank_diameter_cm": 200, "tank_length_cm": 300,
                                "overflow_cm": 180})
        self.assertEqual(self.nodes.get(0xA)["overflow_cm"], 180.0)

    def test_dome_too_deep_rejected(self):
        with self.assertRaisesRegex(SettingsError, r"nejvýše polovina průměru \(100 cm\)"):
            self.nodes.update(0xA, {"tank_shape": "cyl_h_dished", "tank_diameter_cm": 200,
                                    "tank_length_cm": 300, "tank_dome_cm": 120})

    def test_cone_too_tall_rejected(self):
        with self.assertRaisesRegex(SettingsError, "Kužel musí být nižší než celá nádrž"):
            self.nodes.update(0xA, {"tank_shape": "cyl_v_cone", "tank_diameter_cm": 100,
                                    "max_depth_cm": 150, "tank_cone_cm": 150})

    def test_table_shape_needs_rows(self):
        with self.assertRaisesRegex(SettingsError, "vyplňte aspoň 2 řádky"):
            self.nodes.update(0xA, {"tank_shape": "table"})
        self.nodes.update(0xA, {"tank_shape": "table", "tank_table": [[0, 0], [150, 3000]]})


if __name__ == "__main__":
    unittest.main()
