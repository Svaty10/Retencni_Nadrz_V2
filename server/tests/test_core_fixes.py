"""Hub core: one-time auto pairing, safe unpairing, refill stop retry / fault lock,
broken calibration, thread-safe notifications, replay watermark."""
import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import core as core_mod, crypto, model  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.settings import Settings, SettingsError, NODE_SCHEMA, coerce  # noqa: E402


class Node:
    """Minimal firmware model (see uzel/src/link.c)."""

    def __init__(self, core, nid):
        self.core, self.id = core, nid
        self.keys = None
        self.ctr = 0
        self.priv = os.urandom(32)
        self.pub = crypto.x25519_base(self.priv)

    def pair_req(self, paired=None):
        req = crypto.build_pair_req(self.id, self.pub, self.keys is not None if paired is None else paired)
        reply = self.core.responder(req)
        self.core._on_frame(req, -80.0, 20)
        return reply

    def try_pair(self):
        reply = self.pair_req()
        if not reply:
            return None
        if self.keys is not None:
            if crypto.check_unpair(self.keys[1], self.id, self.pub, reply):
                self.keys = None
                return "unpaired"
            return None
        if len(reply) == crypto.PAIR_LEN and reply[0] == crypto.PKT_PAIR_ACK:
            self.keys = crypto.derive_keys(crypto.x25519(self.priv, reply[3:35]))
            return "paired"
        return None

    def send(self, probe=7000, ctr=None):
        c = self.ctr if ctr is None else ctr
        self.ctr = max(self.ctr, c + 1)
        frame = crypto.build_data(self.keys[0], self.keys[1], self.id, c,
                                  crypto.build_telemetry(0, 4900, 1500, 8000, probe))
        self.core._on_frame(frame, -80.0, 20)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = os.path.join(self.tmp.name, "t.db")
        self.core = self.new_core()

    def new_core(self):
        db = Database(self.path)
        return Core(db, Settings(db), self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class AutoPairTest(Base):
    def test_first_install_only_once(self):
        n = Node(self.core, 0x1234)
        self.assertEqual(n.try_pair(), "paired")
        n.send()
        self.assertTrue(self.core.nodes.keys(0x1234))
        self.core.forget_node(0x1234, "NODE_REMOVED", "test")
        # the emptied hub must not take the (same or a foreign) node again on its own
        self.assertIsNone(Node(self.core, 0x1234).try_pair())
        self.assertIsNone(Node(self.core, 0x5678).try_pair())
        # also not after a restart
        c2 = self.new_core()
        self.assertIsNone(Node(c2, 0x5678).try_pair())
        # explicit pairing window still works
        c2.start_pairing(0x5678, "Nová", "admin")
        self.assertEqual(Node(c2, 0x5678).try_pair(), "paired")

    def test_existing_install_counts_as_used(self):
        self.core.db.execute("INSERT INTO measurements(ts, node_id, seq, flags) VALUES(?,?,?,0)",
                             (int(time.time()), 1, 1))
        c2 = self.new_core()
        self.assertIsNone(Node(c2, 0x1111).try_pair())

    def test_pending_key_expires(self):
        n = Node(self.core, 0x1234)
        self.assertEqual(n.try_pair(), "paired")
        k = self.core._pending_keys[0x1234]
        self.core._pending_keys[0x1234] = (k[0], k[1], time.time() - 1)   # window over
        n.send()
        self.assertIsNone(self.core.nodes.get(0x1234))
        self.assertNotIn(0x1234, self.core._pending_keys)


class UnpairTest(Base):
    def setUp(self):
        super().setUp()
        self.n = Node(self.core, 0x2222)
        self.assertEqual(self.n.try_pair(), "paired")
        self.n.send()
        self.core.request_unpair(0x2222, "admin")

    def settle(self):
        for k in list(self.core._unpair_confirm):
            self.core._unpair_confirm[k] -= core_mod.UNPAIR_SETTLE_S + 1
        self.core.unpair_tick()

    def test_forged_unpaired_request_does_not_strand_the_node(self):
        forged = Node(self.core, 0x2222)               # attacker: same id, no key, paired bit 0
        forged.pair_req(paired=False)
        self.settle()
        self.assertTrue(self.core.nodes.keys(0x2222))  # key kept
        self.assertEqual(self.n.try_pair(), "unpaired")   # real node still gets the signed UNPAIR

    def test_lost_unpair_is_retried(self):
        reply = self.n.pair_req()                       # UNPAIR sent, but lost on air
        self.assertTrue(reply)
        Node(self.core, 0x2222).pair_req(paired=False)  # forged "confirmation" right after
        self.assertEqual(self.n.try_pair(), "unpaired")  # real node retries -> confirmation withdrawn
        self.assertTrue(self.core.nodes.keys(0x2222))
        self.n.pair_req()                                # real confirmation (paired bit 0)
        self.assertTrue(self.core.nodes.get(0x2222))     # not yet: settling
        self.settle()
        self.assertIsNone(self.core.nodes.get(0x2222))
        self.assertTrue(self.core.db.scalar("SELECT COUNT(*) FROM audit WHERE action='NODE_UNPAIRED'"))


class WatermarkTest(Base):
    def test_survives_retention(self):
        n = Node(self.core, 0x3333)
        self.assertEqual(n.try_pair(), "paired")
        n.send(ctr=50)
        self.core.db.execute("DELETE FROM measurements")
        c2 = self.new_core()
        self.assertEqual(c2._last_ctr[0x3333], 50)
        n.core = c2
        n.send(ctr=49)                                   # replay of an old frame
        self.assertEqual(c2.db.scalar("SELECT COUNT(*) FROM measurements"), 0)
        n.send(ctr=51)
        self.assertEqual(c2.db.scalar("SELECT COUNT(*) FROM measurements"), 1)


class CalibrationTest(Base):
    def test_duplicate_points_rejected_and_tolerated(self):
        with self.assertRaises(SettingsError):
            coerce(NODE_SCHEMA, "cal_points", [[100, 10], [100, 20]])
        v = model.column_cm(500.0, [[100.0, 10.0], [100.0, 20.0], [900.0, 90.0]])
        self.assertTrue(v == v and abs(v) < 1e6)
        self.assertEqual(model.column_cm(100.0, [[100.0, 10.0], [100.0, 20.0]]), 15.0)

    def test_level_error_keeps_sample(self):
        n = Node(self.core, 0x4444)
        self.assertEqual(n.try_pair(), "paired")
        with mock.patch.object(model, "compute_level", side_effect=ZeroDivisionError):
            n.send(ctr=1)
        row = self.core.db.one("SELECT seq, level_mm, probe_100uv FROM measurements")
        self.assertEqual((row["seq"], row["level_mm"], row["probe_100uv"]), (1, None, 7000))


class RefillBase(Base):
    def setUp(self):
        super().setUp()
        self.core.settings.update({"hydrawise_api_key": "key"})
        self.core.nodes.add(0x5555, "T", {"tank_area_m2": 10.0, "max_depth_cm": 100.0,
                                          "refill_provider": "hydrawise", "hydrawise_relay_id": "7"})
        self.core.last[0x5555] = {"ts": int(time.time()), "level_mm": 200, "flags": 0}
        self.stop_ok = [True]
        self.calls = []

        async def fake_call(key, relay, action, seconds=0):
            self.calls.append(action)
            if action == "stop":
                return (True, "OK") if self.stop_ok.pop(0) else (False, "cloud error")
            return True, "OK"
        self.core.hydrawise.call = fake_call

    def run_(self, coro):
        return asyncio.run(coro)

    def start(self):
        ok, msg = self.run_(self.core.refill_start(0x5555, 50, "manual", "a"))
        self.assertTrue(ok, msg)
        return self.core.refills[0x5555]["id"]

    def row(self, rid):
        return self.core.db.one("SELECT end_ts, result FROM refills WHERE id=?", (rid,))

    def faults(self):
        return self.core.db.scalar("SELECT COUNT(*) FROM audit WHERE action='REFILL_FAULT'")


class RefillStopRetryTest(RefillBase):
    def test_failed_stop_is_retried(self):
        rid = self.start()
        self.stop_ok = [False, True]
        ok, _ = self.run_(self.core.refill_stop(0x5555, "Ručně zastaveno"))
        self.assertFalse(ok)
        self.assertIn(0x5555, self.core.refills)
        self.assertIsNone(self.row(rid)["end_ts"])
        self.run_(self.core.refill_tick())
        self.assertEqual(self.calls, ["run", "stop", "stop"])
        self.assertNotIn(0x5555, self.core.refills)
        self.assertEqual(self.row(rid)["result"], "Ručně zastaveno")

    def test_failed_stop_closed_after_planned_end(self):
        rid = self.start()
        self.stop_ok = [False]
        self.run_(self.core.refill_stop(0x5555, "Ručně zastaveno"))
        r = self.core.refills[0x5555]
        r["start_ts"] -= r["planned_s"] + core_mod.STOP_RETRY_MARGIN_S + 5
        self.run_(self.core.refill_tick())
        self.assertNotIn(0x5555, self.core.refills)
        self.assertIn("časovač", self.row(rid)["result"])
        self.assertEqual(self.faults(), 0)

    def test_tick_survives_one_failing_node(self):
        self.start()
        good = self.core.refills.pop(0x5555)
        self.core.refills[0x9999] = {"id": 0, "start_ts": "broken", "planned_s": 1}   # checked first
        good["start_ts"] -= 10 ** 6                             # expired
        self.core.refills[0x5555] = good
        self.run_(self.core.refill_tick())
        self.assertNotIn(0x5555, self.core.refills)


class RefillFaultTest(RefillBase):
    def age(self, secs):
        r = self.core.refills[0x5555]
        r["start_ts"] -= secs

    def test_manual_early_stop_is_no_fault(self):
        self.start()
        self.age(1200)
        self.run_(self.core.refill_stop(0x5555, "Ručně zastaveno"))
        self.assertEqual(self.faults(), 0)

    def test_data_outage_stop_is_no_fault(self):
        self.start()
        self.age(1200)
        self.core.last[0x5555]["ts"] -= core_mod.FRESH_S + 10
        self.run_(self.core.refill_tick())
        self.assertNotIn(0x5555, self.core.refills)
        self.assertEqual(self.faults(), 0)

    def test_timer_without_rise_is_fault(self):
        self.start()
        self.age(self.core.refills[0x5555]["planned_s"] + 700)
        self.run_(self.core.refill_tick())
        self.assertEqual(self.faults(), 1)
        self.assertGreater(self.core._auto_block_until[0x5555], time.time() + 3600)

    def test_timer_with_stale_level_is_no_fault(self):
        self.start()
        self.age(self.core.refills[0x5555]["planned_s"] + 700)
        self.core.last[0x5555]["level_mm_stale"] = True
        self.run_(self.core.refill_tick())
        self.assertNotIn(0x5555, self.core.refills)
        self.assertEqual(self.faults(), 0)

    def test_start_refused_with_stale_level(self):
        self.core.last[0x5555]["level_mm_stale"] = True
        ok, msg = self.run_(self.core.refill_start(0x5555, 50, "manual", "a"))
        self.assertFalse(ok)
        self.assertIn("chyba sondy", msg)
        self.assertEqual(self.calls, [])


class NotifyThreadTest(Base):
    def test_audit_from_worker_thread_reaches_loop(self):
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        try:
            self.core.loop = loop
            got = threading.Event()
            seen = {}

            def notifier(text, admin_only):
                seen["thread"] = threading.current_thread()
                seen["text"], seen["admin"] = text, admin_only
                asyncio.get_running_loop()           # must be on the loop (Telegram uses ensure_future)
                got.set()
            self.core.notifier = notifier
            worker = threading.Thread(target=lambda: self.core.audit("DISK_LOW", "Málo místa"))
            worker.start()
            worker.join()
            self.assertTrue(got.wait(5))
            self.assertIs(seen["thread"], t)
            self.assertIn("Málo místa", seen["text"])
            self.assertTrue(seen["admin"])
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(5)
            loop.close()

    def test_notify_without_loop_is_direct(self):
        got = []
        self.core.notifier = lambda text, admin: got.append(text)
        self.core.notify("x")
        self.assertEqual(got, ["x"])


if __name__ == "__main__":
    unittest.main()
