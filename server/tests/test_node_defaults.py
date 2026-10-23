"""Shared tank defaults with per-tank overrides (settings.Nodes) and DB migration v9."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka.db import Database  # noqa: E402
from retencka.settings import INHERITABLE, NODE_SCHEMA, Nodes, SettingsError, strip_schema_defaults  # noqa: E402


class NodeDefaults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = os.path.join(self.tmp.name, "t.db")
        self.db = Database(self.path)
        self.nodes = Nodes(self.db)
        self.nodes.add(0xA, "Zahrada")
        self.nodes.add(0xB, "Garáž")

    def tearDown(self):
        self.db.close_thread()
        self.tmp.cleanup()

    def test_inherit_and_override(self):
        a = self.nodes.get(0xA)
        self.assertEqual(a["notify_low_pct"], NODE_SCHEMA["notify_low_pct"][0])
        self.assertEqual(self.nodes.public(0xA)["inherited"], list(INHERITABLE))
        # posting the unchanged (inherited) value does not create an override
        self.assertEqual(self.nodes.update(0xA, {"notify_low_pct": a["notify_low_pct"], "name": "Zahrada"}), [])
        # a different value becomes the tank's own
        self.assertEqual(self.nodes.update(0xA, {"notify_low_pct": 40}), ["notify_low_pct"])
        self.assertIn("notify_low_pct", self.nodes.public(0xA)["overrides"])
        # a shared default change reaches B, never A's override
        self.assertEqual(self.nodes.set_defaults({"notify_low_pct": 25, "battery_type": "alkaline"}),
                         ["battery_type", "notify_low_pct"])
        self.assertEqual(self.nodes.get(0xA)["notify_low_pct"], 40)
        self.assertEqual(self.nodes.get(0xB)["notify_low_pct"], 25)
        self.assertEqual(self.nodes.get(0xA)["battery_type"], "alkaline")
        # survives a reload from the DB (another process / restart)
        again = Nodes(self.db)
        self.assertEqual((again.get(0xA)["notify_low_pct"], again.get(0xB)["notify_low_pct"]), (40, 25))
        # reset: back to the shared default
        self.assertEqual(self.nodes.update(0xA, {}, reset=["notify_low_pct"]), ["notify_low_pct"])
        self.assertEqual(self.nodes.get(0xA)["notify_low_pct"], 25)
        self.assertNotIn("notify_low_pct", self.nodes.public(0xA)["overrides"])
        with self.assertRaises(SettingsError):
            self.nodes.update(0xA, {}, reset=["tank_area_m2"])         # per-tank key has no default

    def test_per_tank_keys_are_not_shared(self):
        with self.assertRaises(SettingsError):
            self.nodes.set_defaults({"tank_area_m2": 5})
        self.nodes.update(0xA, {"tank_area_m2": 3.5})
        self.assertEqual(self.nodes.get(0xB)["tank_area_m2"], NODE_SCHEMA["tank_area_m2"][0])

    def test_default_change_validated_against_each_tank(self):
        self.nodes.update(0xA, {"refill_threshold_pct": 50, "auto_refill_target_pct": 60})
        with self.assertRaises(SettingsError) as cm:
            self.nodes.set_defaults({"max_refill_pct": 55})            # A's own target 60 > 55
        self.assertIn("Zahrada", str(cm.exception))
        self.assertEqual(self.nodes.get(0xB)["max_refill_pct"], 90.0)   # nothing written
        with self.assertRaises(SettingsError):
            self.nodes.set_defaults({"refill_threshold_pct": 40})       # B: target 30 <= 40

    def test_migration_of_old_full_config(self):
        full = {k: v[0] for k, v in NODE_SCHEMA.items()}
        full.update(notify_low_pct=33.0, rain_wait_enabled=False, tank_area_m2=4.2)
        c = sqlite3.connect(self.path)
        c.execute("INSERT INTO nodes(node_id, name, config, created, sort) VALUES(?,?,?,?,?)",
                  (0xC, "Stará", json.dumps(full), int(time.time()), 2))
        # back to a v8 database: undo the later migrations' schema changes (v10)
        c.execute("DROP TABLE node_key_archive")
        c.execute("ALTER TABLE nodes DROP COLUMN unpair_sent_at")
        c.execute("PRAGMA user_version=8")
        c.commit()
        c.close()
        self.db.close_thread()
        db = Database(self.path)                                         # applies v9 (and v10)
        self.assertEqual(db.scalar("PRAGMA user_version"), 10)
        stored = json.loads(db.scalar("SELECT config FROM nodes WHERE node_id=?", (0xC,)))
        self.assertEqual({k for k in stored if k in INHERITABLE}, {"notify_low_pct", "rain_wait_enabled"})
        n = Nodes(db).get(0xC)
        for k, v in full.items():                                       # no effective value changed
            self.assertEqual(n[k], v, k)
        self.assertEqual(n["_overrides"], ["notify_low_pct", "rain_wait_enabled"])
        db.close_thread()

    def test_strip_for_old_backups(self):
        full = {k: v[0] for k, v in NODE_SCHEMA.items()}
        full["runoff_coef"] = 0.6
        out = strip_schema_defaults(full)
        self.assertEqual({k for k in out if k in INHERITABLE}, {"runoff_coef"})
        self.assertIn("tank_area_m2", out)


if __name__ == "__main__":
    unittest.main()
