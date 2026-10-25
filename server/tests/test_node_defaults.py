"""Shared tank defaults with per-tank overrides (settings.Nodes) and DB migration v9."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka.db import MIGRATIONS, Database  # noqa: E402
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
        self.assertEqual(self.nodes.set_defaults({"notify_low_pct": 25, "battery_type": "lifes2"}),
                         ["battery_type", "notify_low_pct"])
        self.assertEqual(self.nodes.get(0xA)["notify_low_pct"], 40)
        self.assertEqual(self.nodes.get(0xB)["notify_low_pct"], 25)
        self.assertEqual(self.nodes.get(0xA)["battery_type"], "lifes2")
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
        # a 1.1.x node config (battery capacity was global then; new tank shapes did not exist)
        full = {k: v[0] for k, v in NODE_SCHEMA.items()
                if not k.startswith("battery_capacity")
                and k not in ("tank_shape", "tank_diameter_cm", "tank_length_cm", "tank_width_cm")}
        full.update(battery_type="lifes2", flow_rate_lpm=41.6)          # the schema defaults back then
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
        self.assertEqual(db.scalar("PRAGMA user_version"), len(MIGRATIONS))
        stored = json.loads(db.scalar("SELECT config FROM nodes WHERE node_id=?", (0xC,)))
        self.assertEqual({k for k in stored if k in INHERITABLE}, {"battery_type", "flow_rate_lpm", "notify_low_pct", "rain_wait_enabled"})
        n = Nodes(db).get(0xC)
        for k, v in full.items():                                       # no effective value changed
            self.assertEqual(n[k], v, k)
        self.assertEqual(sorted(n["_overrides"]), ["battery_type", "flow_rate_lpm", "notify_low_pct", "rain_wait_enabled"])
        db.close_thread()

    def test_v14_keeps_the_old_battery_and_flow_defaults(self):
        c = sqlite3.connect(self.path)
        c.execute("INSERT INTO nodes(node_id, name, config, created, sort) VALUES(?,?,?,?,?)",
                  (0xC, "Vlastní", json.dumps({"battery_type": "alkaline", "flow_rate_lpm": 12.0}), 0, 2))
        c.execute("PRAGMA user_version=13")
        c.commit()
        c.close()
        self.db.close_thread()
        db = Database(self.path)
        n = Nodes(db)
        for nid in (0xA, 0xB):                       # inherited the 1.12 schema defaults
            self.assertEqual((n.get(nid)["battery_type"], n.get(nid)["flow_rate_lpm"]), ("lifes2", 41.6))
        self.assertEqual((n.get(0xC)["battery_type"], n.get(0xC)["flow_rate_lpm"]), ("alkaline", 12.0))
        n.add(0xD, "Nová")                           # a new tank gets the new defaults
        self.assertEqual(n.get(0xD)["battery_type"], NODE_SCHEMA["battery_type"][0])
        db.close_thread()

    def test_v14_leaves_stored_shared_defaults_alone(self):
        self.nodes.set_defaults({"battery_type": "lifes2", "flow_rate_lpm": 30.0})
        self.db.execute("PRAGMA user_version=13")
        self.db.close_thread()
        db = Database(self.path)
        stored = json.loads(db.scalar("SELECT config FROM nodes WHERE node_id=?", (0xA,)))
        self.assertNotIn("flow_rate_lpm", stored)    # still inherits the shared 30 L/min
        self.assertEqual(Nodes(db).get(0xA)["flow_rate_lpm"], 30.0)
        db.close_thread()

    def test_strip_for_old_backups(self):
        full = {k: v[0] for k, v in NODE_SCHEMA.items()}
        full["runoff_coef"] = 0.6
        out = strip_schema_defaults(full)
        self.assertEqual({k for k in out if k in INHERITABLE}, {"runoff_coef"})
        self.assertIn("tank_area_m2", out)

    def test_battery_capacity_per_tank(self):
        from retencka import model
        self.assertIn("battery_capacity_mah", INHERITABLE)
        self.nodes.set_defaults({"battery_capacity_mah": 3500})
        self.nodes.update(0xA, {"battery_capacity_mah": 2000})
        a, b = self.nodes.get(0xA), self.nodes.get(0xB)
        self.assertEqual((a["battery_capacity_mah"], b["battery_capacity_mah"]), (2000, 3500))
        energy = {"node_cycle_mAs": 3.3, "node_sleep_uA": 1.5, "node_heat_mAs": 66.0}
        da = model.battery_days_left(80, 288, {**energy, "battery_capacity_mah": a["battery_capacity_mah"]})
        db_ = model.battery_days_left(80, 288, {**energy, "battery_capacity_mah": b["battery_capacity_mah"]})
        self.assertLess(da, db_)

    def test_migration_v11_global_capacity(self):
        c = sqlite3.connect(self.path)
        c.execute("DELETE FROM settings WHERE key='node_defaults'")
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('battery_capacity_mah', '3400.0')")
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('battery_capacity_alk_mah', '2500.0')")
        c.execute("PRAGMA user_version=10")
        c.commit()
        c.close()
        self.db.close_thread()
        db = Database(self.path)                                         # applies v11
        d = json.loads(db.scalar("SELECT value FROM settings WHERE key='node_defaults'"))
        self.assertEqual(d, {"battery_capacity_mah": 3400.0})            # alkaline = default: inherited
        n = Nodes(db)
        self.assertEqual(n.get(0xA)["battery_capacity_mah"], 3400.0)
        self.assertEqual(n.get(0xA)["battery_capacity_alk_mah"], 2500.0)
        db.close_thread()

    def test_migration_v11_defaults_left_alone(self):
        self.nodes.set_defaults({"notify_low_pct": 33})
        c = sqlite3.connect(self.path)
        c.execute("PRAGMA user_version=10")
        c.commit()
        c.close()
        self.db.close_thread()
        db = Database(self.path)                                         # no global capacity rows
        self.assertEqual(json.loads(db.scalar("SELECT value FROM settings WHERE key='node_defaults'"))["notify_low_pct"], 33)
        self.assertEqual(Nodes(db).get(0xA)["battery_capacity_mah"], 3000.0)
        db.close_thread()

    def test_relay_id_validation(self):
        self.assertEqual(self.nodes.update(0xA, {"hydrawise_relay_id": " 12345 "}), ["hydrawise_relay_id"])
        self.assertEqual(self.nodes.get(0xA)["hydrawise_relay_id"], "12345")
        with self.assertRaises(SettingsError) as cm:
            self.nodes.update(0xA, {"hydrawise_relay_id": "A1B2C3D4E5F6A7B8"})        # pasted API key
        self.assertIn("API klíč", str(cm.exception))
        with self.assertRaises(SettingsError):
            self.nodes.update(0xA, {"hydrawise_relay_id": "zona1"})
        with self.assertRaises(SettingsError):
            self.nodes.update(0xA, {"hydrawise_relay_id": "１２"})          # non-ASCII digits
        self.assertEqual(self.nodes.update(0xA, {"hydrawise_relay_id": ""}), ["hydrawise_relay_id"])


if __name__ == "__main__":
    unittest.main()
