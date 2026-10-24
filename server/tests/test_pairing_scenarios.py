"""Pairing / unpairing edge cases (docs/architektura.md, section "Párování – stavy"):
every scenario must end in a state both sides can recover from, without UPDI."""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from retencka import core as core_mod, crypto  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.settings import Settings  # noqa: E402
from test_core_fixes import Node as _Node  # noqa: E402


class Node(_Node):
    def reboot(self):
        """Battery re-inserted: a new key pair (the EEPROM keeps the link key)."""
        self.priv = os.urandom(32)
        self.pub = crypto.x25519_base(self.priv)


def radio():
    return types.SimpleNamespace(auth_failures=0, foreign_packets=0, replays=0, duplicates=0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = os.path.join(self.tmp.name, "t.db")
        self.core = self.new_core()

    def tearDown(self):
        self.tmp.cleanup()

    def new_core(self, path=None, data_dir=None):
        db = Database(path or self.path)
        return Core(db, Settings(db), data_dir or self.tmp.name)

    def paired(self, nid, name="Nádrž", core=None):
        c = core or self.core
        c.start_pairing(nid, name, "admin")
        n = Node(c, nid)
        self.assertEqual(n.try_pair(), "paired")
        n.send()
        self.assertTrue(c.nodes.keys(nid))
        return n

    def count(self, nid, core=None):
        return (core or self.core).db.scalar("SELECT COUNT(*) FROM measurements WHERE node_id=?", (nid,))

    def archived(self, nid, core=None):
        return (core or self.core).db.scalar("SELECT COUNT(*) FROM node_key_archive WHERE node_id=?", (nid,))

    def age_pending(self, nid, secs, core=None):
        c = core or self.core
        for e in c._pending_keys.get(nid, []):
            e["ts"] -= secs
        c.db.execute("UPDATE node_key_archive SET removed_at=removed_at-? WHERE node_id=? AND reason='pending'",
                     (secs, nid))

    def settle(self, core=None):
        c = core or self.core
        for k in list(c._unpair_confirm):
            c._unpair_confirm[k] -= core_mod.UNPAIR_SETTLE_S + 1
        c.unpair_tick()


class S1RemovedNodeTest(Base):
    def test_s1_removed_node_repaired_from_key_archive_without_updi(self):
        n = self.paired(0x1111, "Jímka")
        self.core.forget_node(0x1111, "NODE_REMOVED", "Odebrat bez čidla")
        self.assertIsNone(self.core.nodes.get(0x1111))
        before = self.count(0x1111)
        n.send()                                          # sensor still holds the key
        self.assertEqual(self.count(0x1111), before)      # ... but its data are not accepted
        info = self.core.archive_info(0x1111)
        self.assertTrue(info["archived"] and info["key_match"])
        self.assertTrue(self.core.heard[0x1111]["key_match"])     # UI: "Spárovat znovu"
        n.reboot()
        self.assertIsNone(n.try_pair())                   # no confirmation -> nothing happens
        self.assertIsNotNone(n.keys)
        # user: "Spárovat znovu" -> UNPAIR signed with the archived key, then a new key
        self.core.start_pairing(0x1111, "", "admin")
        self.assertEqual(n.try_pair(), "unpaired")
        self.assertEqual(n.try_pair(), "paired")
        n.send()
        node = self.core.nodes.get(0x1111)
        self.assertEqual(node["name"], "Jímka")           # name taken from the archive
        self.assertEqual(self.core.nodes.keys(0x1111), n.keys)
        self.assertEqual(self.archived(0x1111), 0)        # archive entry dropped
        self.assertEqual(self.count(0x1111), before + 1)

    def test_s1_unpair_tries_every_archived_key(self):
        n = self.paired(0x1112)
        self.core.forget_node(0x1112, "NODE_REMOVED", "x")
        # a newer, wrong key of the same ID (e.g. a pairing the sensor never took)
        self.core._archive_key(0x1112, os.urandom(16), os.urandom(16), "expired", ts=time.time() + 10)
        self.core._reload_archive()
        self.core.start_pairing(0x1112, "", "admin")
        n.reboot()
        results = [n.try_pair() for _ in range(3)]
        self.assertIn("unpaired", results)
        while n.keys is None and n.try_pair() != "paired":
            pass
        n.send()
        self.assertEqual(self.core.nodes.keys(0x1112), n.keys)

    def test_s1_backup_carries_key_archive_and_old_backup_imports(self):
        n = self.paired(0x1113, "Studna")
        self.core.forget_node(0x1113, "NODE_REMOVED", "x")
        snap = os.path.join(self.tmp.name, "snap.db")
        self.core.snapshot(snap)
        # restore into a freshly installed hub
        d2 = os.path.join(self.tmp.name, "hub2")
        os.makedirs(d2)
        c2 = self.new_core(os.path.join(d2, "t.db"), d2)
        st = c2.import_backup(snap, full=True)
        self.assertEqual(st.get("archived_keys"), 1)
        self.assertTrue(c2.archive_info(0x1113)["archived"])
        self.assertEqual(c2.import_backup(snap).get("archived_keys", 0), 0)   # twice = no duplicate
        n.core = c2
        c2.start_pairing(0x1113, "", "admin")
        n.reboot()
        self.assertEqual(n.try_pair(), "unpaired")
        self.assertEqual(n.try_pair(), "paired")
        n.send()
        self.assertEqual(c2.nodes.get(0x1113)["name"], "Studna")
        # backup made before v10 (no archive table, no unpair_sent_at)
        old = os.path.join(self.tmp.name, "old.db")
        shutil.copyfile(snap, old)
        con = sqlite3.connect(old)
        con.execute("DROP TABLE node_key_archive")
        con.execute("ALTER TABLE nodes DROP COLUMN unpair_sent_at")
        con.execute("PRAGMA user_version=9")
        con.commit()
        con.close()
        d3 = os.path.join(self.tmp.name, "hub3")
        os.makedirs(d3)
        c3 = self.new_core(os.path.join(d3, "t.db"), d3)
        st = c3.import_backup(old, full=True)
        self.assertGreater(st["measurements"], 0)
        self.assertFalse(c3.archive_info(0x1113)["archived"])


class S2LostFirstDataTest(Base):
    def test_s2_lost_first_data_commits_later_even_after_restart(self):
        self.core.start_pairing(0x2201, "Zahrada", "admin")
        n = Node(self.core, 0x2201)
        self.assertEqual(n.try_pair(), "paired")         # first DATA lost on air
        self.core.pair_targets[0x2201]["expires"] = time.time() - 1   # pairing window over
        st = self.core.pairing_status()
        self.assertEqual(st["targets"], [])
        self.assertEqual([p["id"] for p in st["pending"]], ["2201"])
        self.age_pending(0x2201, 3 * 3600)
        c2 = self.new_core()                              # hub restarted meanwhile
        n.core = c2
        n.send()
        self.assertEqual(c2.nodes.keys(0x2201), n.keys)
        self.assertEqual(c2.nodes.get(0x2201)["name"], "Zahrada")
        self.assertEqual(self.archived(0x2201, c2), 0)

    def test_s2_unproven_key_archived_after_a_day_then_recoverable(self):
        self.core.start_pairing(0x2202, "", "admin")
        n = Node(self.core, 0x2202)
        self.assertEqual(n.try_pair(), "paired")
        self.age_pending(0x2202, core_mod.PENDING_KEY_S + 1)
        self.core.unpair_tick()
        self.assertNotIn(0x2202, self.core._pending_keys)
        n.send()
        self.assertIsNone(self.core.nodes.get(0x2202))    # no longer accepted for data
        self.assertTrue(self.core.archive_info(0x2202)["key_match"])
        self.core.start_pairing(0x2202, "", "admin")
        n.reboot()
        self.assertEqual(n.try_pair(), "unpaired")
        self.assertEqual(n.try_pair(), "paired")
        n.send()
        self.assertEqual(self.core.nodes.keys(0x2202), n.keys)

    def test_s2_pending_key_without_confirmation_never_commits(self):
        n = Node(self.core, 0x2203)
        self.assertIsNone(n.try_pair())                   # no start_pairing: no PAIR_ACK at all
        self.core.start_pairing(0x2203, "", "admin")
        self.assertEqual(n.try_pair(), "paired")
        self.core.cancel_pairing(0x2203)                  # user withdrew the confirmation
        n.send()
        self.assertIsNone(self.core.nodes.get(0x2203))
        self.assertTrue(self.core.archive_info(0x2203)["archived"])


class S3RestartDuringUnpairTest(Base):
    def test_s3_hub_restart_after_unpair_sent_still_completes(self):
        n = self.paired(0x3301)
        self.core.request_unpair(0x3301, "admin")
        n.reboot()
        self.assertEqual(n.try_pair(), "unpaired")        # UNPAIR delivered, sensor wiped its key
        self.assertTrue(self.core.nodes.get(0x3301)["_unpair_sent"])
        c2 = self.new_core()                              # restart before the confirmation
        n.core = c2
        c2.db.execute("UPDATE nodes SET unpair_sent_at=unpair_sent_at-300")   # restart took a while
        c2.nodes.reload()
        c3 = self.new_core()
        n.core = c3
        self.assertIsNone(n.try_pair())                   # unpaired request = confirmation
        self.assertIn(0x3301, c3._unpair_confirm)
        self.settle(c3)
        self.assertIsNone(c3.nodes.get(0x3301))
        self.assertTrue(c3.db.scalar("SELECT COUNT(*) FROM audit WHERE action='NODE_UNPAIRED'"))

    def test_s3_late_confirmation_counts(self):
        n = self.paired(0x3302)
        self.core.request_unpair(0x3302, "admin")
        n.reboot()
        self.assertEqual(n.try_pair(), "unpaired")
        self.core._unpair_sent[0x3302] -= 600             # first confirmations lost on air
        self.assertIsNone(n.try_pair())
        self.settle()
        self.assertIsNone(self.core.nodes.get(0x3302))

    def test_s3_unpaired_request_before_any_unpair_does_not_count(self):
        n = self.paired(0x3303)
        self.core.request_unpair(0x3303, "admin")
        Node(self.core, 0x3303).pair_req(paired=False)    # forged: no UNPAIR was sent yet
        self.settle()
        self.assertTrue(self.core.nodes.keys(0x3303))
        n.reboot()
        self.assertEqual(n.try_pair(), "unpaired")


class S4SensorLostKeyTest(Base):
    def test_s4_sensor_lost_key_repaired_keeps_history_and_settings(self):
        n = self.paired(0x4401, "Sklep")
        self.core.nodes.update(0x4401, {"tank_area_m2": 3.3})
        n.send()
        old_keys, hist = self.core.nodes.keys(0x4401), self.count(0x4401)
        fresh = Node(self.core, 0x4401)                   # EEPROM erased / new chip, same ID
        self.assertIsNone(fresh.try_pair())               # not confirmed: nothing changes
        self.assertEqual(self.core.nodes.keys(0x4401), old_keys)
        req = [r for r in self.core.pairing_status()["requests"] if r["id"] == "4401"][0]
        self.assertTrue(req["known"])                     # UI: "Znovu spárovat"
        self.assertFalse(req["node_paired"])
        self.core.start_pairing(0x4401, "", "admin")
        self.assertEqual(fresh.try_pair(), "paired")
        self.assertEqual(self.core.nodes.keys(0x4401), old_keys)   # only after the first DATA
        fresh.send()                                      # counter starts at 0 again
        self.assertEqual(self.core.nodes.keys(0x4401), fresh.keys)
        node = self.core.nodes.get(0x4401)
        self.assertEqual((node["name"], node["tank_area_m2"]), ("Sklep", 3.3))
        self.assertEqual(self.count(0x4401), hist + 1)


class S5UnpairNeverAnsweredTest(Base):
    def test_s5_unpair_can_be_cancelled_or_forced(self):
        n = self.paired(0x5501)
        self.core.request_unpair(0x5501, "admin")
        c0 = self.count(0x5501)
        n.send()
        self.assertEqual(self.count(0x5501), c0)          # blocked while waiting
        self.core.cancel_unpair(0x5501, "admin")
        n.send()
        self.assertEqual(self.count(0x5501), c0 + 1)      # normal data again
        self.core.request_unpair(0x5501, "admin")
        self.core.forget_node(0x5501, "NODE_REMOVED", "Odebrat bez čidla")
        self.assertIsNone(self.core.nodes.get(0x5501))
        self.assertEqual(self.archived(0x5501), 1)        # key archived (scenario 1 recovery)


class S6RepairTest(Base):
    def test_s6_repair_survives_lost_unpair_and_lost_ack(self):
        n = self.paired(0x6601, "Jímka")
        self.core.request_unpair(0x6601, "admin")         # unpair timers engaged ...
        self.core.start_pairing(0x6601, "", "admin")      # ... then "Znovu spárovat" instead
        n.reboot()
        self.assertTrue(n.pair_req())                     # UNPAIR lost on air
        self.assertIsNotNone(n.keys)
        self.assertEqual(n.try_pair(), "unpaired")        # retry
        self.assertTrue(n.pair_req(paired=False))         # PAIR_ACK lost on air
        self.assertEqual(n.try_pair(), "paired")          # retry: same key
        self.settle()
        self.assertTrue(self.core.nodes.get(0x6601))      # settle logic does not interfere
        n.send()
        node = self.core.nodes.get(0x6601)
        self.assertEqual(self.core.nodes.keys(0x6601), n.keys)
        self.assertFalse(node["_unpair"])
        self.assertEqual(node["name"], "Jímka")

    def test_s6_repair_during_confirmed_unpair_is_not_delayed(self):
        n = self.paired(0x6602)
        self.core.request_unpair(0x6602, "admin")
        n.reboot()
        self.assertEqual(n.try_pair(), "unpaired")
        self.assertIsNone(n.try_pair())                   # confirmation, settling
        self.core.start_pairing(0x6602, "", "admin")
        self.assertEqual(n.try_pair(), "paired")          # PAIR_ACK at once
        self.settle()
        n.send()
        self.assertEqual(self.core.nodes.keys(0x6602), n.keys)


class S7OtherHubFirstTest(Base):
    def test_s7_other_hub_answered_first_no_errors_no_spam(self):
        notes = []
        self.core.notifier = lambda text, admin: notes.append(text)
        self.core.radio = radio()
        self.core.start_pairing(0x7701, "", "admin")
        n = Node(self.core, 0x7701)
        self.assertTrue(n.pair_req())                     # our PAIR_ACK ...
        n.keys = (os.urandom(16), os.urandom(16))         # ... but the neighbour's came first
        for _ in range(20):
            n.send()
        self.assertIsNone(self.core.nodes.get(0x7701))
        self.assertEqual(self.core.heard[0x7701]["kind"], "data")
        self.assertFalse(self.core.heard[0x7701]["key_match"])
        self.assertEqual(self.core.radio.auth_failures, 0)
        self.age_pending(0x7701, core_mod.PENDING_KEY_S + 1)
        for _ in range(3):
            self.core.unpair_tick()
            self.core.watch_tick()
        self.assertNotIn(0x7701, self.core._pending_keys)
        self.assertEqual(self.archived(0x7701), 1)
        self.assertEqual(self.core.db.scalar("SELECT COUNT(*) FROM audit"), 0)
        self.assertEqual(notes, [])


class S8OtherSensorRebootTest(Base):
    def test_s8_battery_swap_of_other_sensor_during_pairing_window_ignored(self):
        b = self.paired(0x8802, "B")
        kb = self.core.nodes.keys(0x8802)
        self.core.start_pairing(0x8801, "A", "admin")
        b.reboot()
        self.assertIsNone(b.try_pair())                   # paired B: no UNPAIR, key kept
        self.assertEqual(self.core.nodes.keys(0x8802), kb)
        c0 = self.count(0x8802)
        b.send()
        self.assertEqual(self.count(0x8802), c0 + 1)
        self.assertIsNone(Node(self.core, 0x8803).try_pair())   # unpaired C, not confirmed
        a = Node(self.core, 0x8801)
        self.assertEqual(a.try_pair(), "paired")
        a.send()
        self.assertEqual(sorted(self.core.nodes.ids()), [0x8801, 0x8802])


class S9WatermarkTest(Base):
    def test_s9_watermark_reset_only_by_authenticated_key_change(self):
        self.core.radio = radio()
        n = self.paired(0x9901)
        n.send(ctr=100)
        forged = crypto.build_data(os.urandom(16), os.urandom(16), 0x9901, 10 ** 6,
                                   crypto.build_telemetry(0, 4900, 1500, 8000, 7000))
        self.core._on_frame(forged, -80.0, 20)
        Node(self.core, 0x9901).pair_req(paired=False)    # forged "unpaired" request
        self.assertEqual(self.core._last_ctr[0x9901], 100)
        self.assertEqual(self.core.db.scalar("SELECT last_ctr FROM nodes WHERE node_id=?", (0x9901,)), 100)
        old = n.keys
        self.core.start_pairing(0x9901, "", "admin")
        n.reboot()
        self.assertEqual(n.try_pair(), "unpaired")
        self.assertEqual(n.try_pair(), "paired")
        self.assertEqual(self.core._last_ctr[0x9901], 100)   # unchanged until the new key is proven
        n.send(ctr=101)
        self.assertEqual(self.core._last_ctr[0x9901], 101)
        c0 = self.count(0x9901)
        n.send(ctr=101)                                   # second copy
        frame = crypto.build_data(old[0], old[1], 0x9901, 500, crypto.build_telemetry(0, 4900, 1500, 8000, 7000))
        self.core._on_frame(frame, -80.0, 20)             # old key no longer valid
        self.assertEqual(self.count(0x9901), c0)
        self.assertEqual(self.core._last_ctr[0x9901], 101)
        self.assertEqual(self.new_core()._last_ctr[0x9901], 101)

    def test_s9_recorded_frame_replayed_is_refused_also_after_restart(self):
        self.core.radio = radio()
        n = self.paired(0x9902)
        tel = crypto.build_telemetry(0, 4900, 1500, 8000, 7000)
        recorded = crypto.build_data(n.keys[0], n.keys[1], 0x9902, 5, tel)    # sniffed off the air
        self.core._on_frame(recorded, -80.0, 20)
        n.send(ctr=6)
        c0 = self.count(0x9902)
        self.core._on_frame(recorded, -80.0, 20)
        self.assertEqual(self.count(0x9902), c0)
        self.assertEqual(self.core.radio.replays, 1)
        restarted = self.new_core()
        restarted.radio = radio()
        restarted._on_frame(recorded, -80.0, 20)
        self.assertEqual(self.count(0x9902, restarted), c0)
        self.assertEqual(restarted.radio.replays, 1)


if __name__ == "__main__":
    unittest.main()
