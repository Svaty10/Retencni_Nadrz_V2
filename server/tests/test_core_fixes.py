"""Hub core: pairing only after confirmation, safe unpairing, refill stop retry / fault lock,
broken calibration, thread-safe notifications, replay watermark."""
import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import core as core_mod, crypto, model  # noqa: E402
from retencka.__main__ import open_database  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database, SchemaTooNewError  # noqa: E402
from retencka.external import Hydrawise  # noqa: E402
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


class PairingTest(Base):
    def test_removed_node_does_not_come_back_from_legacy_key(self):
        # first single-node build left "paired_node_id" in settings
        self.core.db.execute("INSERT INTO settings(key, value) VALUES('paired_node_id', ?)", (str(0xBF1C),))
        c2 = self.new_core()
        self.assertIsNotNone(c2.nodes.get(0xBF1C))          # migrated once
        c2.forget_node(0xBF1C, "NODE_REMOVED", "test")
        c3 = self.new_core()                                 # restart / update
        self.assertIsNone(c3.nodes.get(0xBF1C))
        self.assertIsNone(c3.db.scalar("SELECT value FROM settings WHERE key='paired_node_id'"))

    def test_pairing_needs_confirmation(self):
        # empty hub: neither the own nor a foreign sensor attaches itself
        self.assertIsNone(Node(self.core, 0x1234).try_pair())
        self.assertIsNone(self.core.nodes.get(0x1234))
        # the user confirms the ID from the label -> paired
        self.core.start_pairing(0x1234, "Nádrž", "admin")
        n = Node(self.core, 0x1234)
        self.assertEqual(n.try_pair(), "paired")
        n.send()
        self.assertTrue(self.core.nodes.keys(0x1234))
        # removed node is not taken back on its own, not even after a restart
        self.core.forget_node(0x1234, "NODE_REMOVED", "test")
        self.assertIsNone(Node(self.core, 0x1234).try_pair())
        c2 = self.new_core()
        self.assertIsNone(Node(c2, 0x5678).try_pair())

    def test_battery_swap_keeps_pairing(self):
        self.core.start_pairing(0x1234, "Nádrž", "admin")
        n = Node(self.core, 0x1234)
        self.assertEqual(n.try_pair(), "paired")
        n.send(ctr=1)
        # battery re-inserted: paired request, hub does not answer, the key stays valid
        self.assertIsNone(n.try_pair())
        n.send(ctr=1025)
        self.assertEqual(self.core.db.scalar("SELECT COUNT(*) FROM measurements"), 2)

    def test_huge_counter_jump_needs_confirmation(self):
        self.core.start_pairing(0x1234, "Nádrž", "admin")
        n = Node(self.core, 0x1234)
        self.assertEqual(n.try_pair(), "paired")
        n.send(ctr=1)
        # single 1:2^32 MAC collision with a huge counter must not lock out the real sensor
        n.send(ctr=0xFFFFFFFF)
        self.assertEqual(self.core.db.scalar("SELECT COUNT(*) FROM measurements"), 1)
        self.assertEqual(self.core._last_ctr[0x1234], 1)
        n.send(ctr=2)
        self.assertEqual(self.core.db.scalar("SELECT COUNT(*) FROM measurements"), 2)
        self.assertEqual(self.core._last_ctr[0x1234], 2)
        # real sensor returning after years offline resynchronizes on its second frame
        n.send(ctr=500000)
        n.send(ctr=500000)                           # duplicate of the same frame is not confirmation
        self.assertEqual(self.core._last_ctr[0x1234], 2)
        n.send(ctr=500001)
        self.assertEqual(self.core._last_ctr[0x1234], 500001)
        self.assertEqual(self.core.db.scalar("SELECT COUNT(*) FROM measurements"), 3)

    def test_pending_key_expires(self):
        self.core.start_pairing(0x1234, "Nádrž", "admin")
        n = Node(self.core, 0x1234)
        self.assertEqual(n.try_pair(), "paired")
        for e in self.core._pending_keys[0x1234]:
            e["ts"] -= core_mod.PENDING_KEY_S + 1          # no DATA for a whole day
        n.send()
        self.assertIsNone(self.core.nodes.get(0x1234))
        self.core.unpair_tick()
        self.assertNotIn(0x1234, self.core._pending_keys)
        self.assertTrue(self.core.archive_info(0x1234)["archived"])   # kept for a later UNPAIR

    def test_pending_max_prefers_strongest_rssi_and_reports_pub_count(self):
        self.core.start_pairing(0x1234, "Nádrž", "admin")
        rssis = [-95, -90, -85, -80, -60, -92]
        pubs = []
        for rssi in rssis:
            priv = os.urandom(32)
            pub = crypto.x25519_base(priv)
            pubs.append(pub)
            req = crypto.build_pair_req(0x1234, pub, False)
            self.core.responder(req)
            self.core._on_frame(req, float(rssi), 20)

        pending = self.core._pending_keys[0x1234]
        self.assertEqual(len(pending), core_mod.PENDING_MAX)
        pending_pubs = {e.get("pub") for e in pending}
        self.assertIn(pubs[4].hex(), pending_pubs)
        self.assertTrue(any(e.get("rssi") == -60.0 for e in pending))
        status = self.core.pairing_status()
        self.assertGreaterEqual(status["requests"][0]["pub_count"], 2)

    def test_pair_request_log_is_bounded(self):
        # PAIR_REQ is unauthenticated: a radio flood with random keys and IDs must not
        # grow the request log without limit (outside any pairing window)
        for i in range(core_mod.PAIR_PUBS_MAX * 5):
            self.core._on_frame(crypto.build_pair_req(0x1234, os.urandom(32), False), -80.0, 20)
        pr = self.core.pair_requests[0x1234]
        self.assertEqual(len(pr["pubs"]), core_mod.PAIR_PUBS_MAX)
        self.assertEqual(pr["pub_count"], core_mod.PAIR_PUBS_MAX)
        self.assertEqual(pr["count"], core_mod.PAIR_PUBS_MAX * 5)
        for nid in range(1, core_mod.PAIR_REQUESTS_MAX * 3):
            self.core._on_frame(crypto.build_pair_req(nid, os.urandom(32), False), -80.0, 20)
        self.assertEqual(len(self.core.pair_requests), core_mod.PAIR_REQUESTS_MAX)
        self.assertIn(core_mod.PAIR_REQUESTS_MAX * 3 - 1, self.core.pair_requests)   # newest kept
        self.assertNotIn(0x1234, self.core.pair_requests)                            # oldest dropped


class UnpairTest(Base):
    def setUp(self):
        super().setUp()
        self.n = Node(self.core, 0x2222)
        self.core.start_pairing(0x2222, "Nádrž", "admin")
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
        self.core.start_pairing(0x3333, "Nádrž", "admin")
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
        self.core.start_pairing(0x4444, "Nádrž", "admin")
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

    def test_uncertain_start_sends_preventive_stop_and_retries(self):
        calls = []
        stop_results = [(False, "timeout"), (True, "OK")]

        async def fake_call(key, relay, action, seconds=0):
            calls.append(action)
            if action == "run":
                return False, Hydrawise.UNSURE
            if action == "stop":
                return stop_results.pop(0) if stop_results else (True, "OK")
            return True, "OK"

        self.core.hydrawise.call = fake_call
        ok, msg = self.run_(self.core.refill_start(0x5555, 50, "manual", "a"))
        self.assertFalse(ok)
        self.assertEqual(calls, ["run", "stop"])
        self.assertIn(0x5555, self.core.refills)
        self.assertTrue(self.core.refills[0x5555].get("stop_retry"))
        rid = self.core.refills[0x5555]["id"]
        self.run_(self.core.refill_tick())
        self.assertEqual(calls, ["run", "stop", "stop"])
        self.assertNotIn(0x5555, self.core.refills)
        self.assertIsNotNone(self.row(rid)["end_ts"])

    def test_refused_start_sends_no_stop_and_records_nothing(self):
        calls = []

        async def fake_call(key, relay, action, seconds=0):
            calls.append(action)
            return (False, "Hydrawise odpověděl chybou (HTTP 401).") if action == "run" else (True, "OK")

        self.core.hydrawise.call = fake_call
        ok, _ = self.run_(self.core.refill_start(0x5555, 50, "manual", "a"))
        self.assertFalse(ok)
        self.assertEqual(calls, ["run"])                 # a schedule run in the zone is left alone
        self.assertNotIn(0x5555, self.core.refills)
        self.assertEqual(self.core.db.scalar("SELECT COUNT(*) FROM refills"), 0)

    def test_hydrawise_unanswered_attempt_makes_the_result_unsure(self):
        answers = [TimeoutError(), (500, "x"), (500, "x")]

        class FakeHttp:
            async def get(self, url, params):
                a = answers.pop(0)
                if isinstance(a, Exception):
                    raise a
                return a

        async def no_sleep(_):
            pass

        with mock.patch.object(asyncio, "sleep", no_sleep):
            self.assertEqual(self.run_(Hydrawise(FakeHttp()).call("k", "1", "run", 60)), (False, Hydrawise.UNSURE))
            answers[:] = [(500, "x")] * 3
            ok, msg = self.run_(Hydrawise(FakeHttp()).call("k", "1", "run", 60))
            self.assertNotEqual(msg, Hydrawise.UNSURE)


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

    def test_manual_start_waits_for_valid_data(self):
        self.core.last[0x5555]["level_mm_stale"] = True
        ok, msg = self.run_(self.core.refill_start(0x5555, 50, "manual", "a"))
        self.assertTrue(ok, msg)
        self.assertIn("Čeká se", msg)
        self.assertEqual(self.calls, [])                 # valve untouched while data is invalid
        self.assertIn(0x5555, self.core.refill_wait)
        self.assertTrue(self.core.refill_info(0x5555)["waiting_for_data"])
        self.run_(self.core._refill_on_measurement(0x5555))   # still a probe error -> keeps waiting
        self.assertEqual(self.calls, [])
        self.core.last[0x5555]["level_mm_stale"] = False      # fresh valid measurement arrives
        self.run_(self.core._refill_on_measurement(0x5555))
        self.assertIn(0x5555, self.core.refills)
        self.assertNotIn(0x5555, self.core.refill_wait)

    def test_auto_start_refused_with_stale_level(self):
        self.core.last[0x5555]["level_mm_stale"] = True
        ok, _ = self.run_(self.core.refill_start(0x5555, 50, "auto", "system"))
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])
        self.assertNotIn(0x5555, self.core.refill_wait)

    def test_waiting_request_expires_or_is_cancelled(self):
        self.core.last[0x5555]["level_mm_stale"] = True
        self.run_(self.core.refill_start(0x5555, 50, "manual", "a"))
        ok, msg = self.run_(self.core.refill_stop(0x5555, "Ručně"))
        self.assertTrue(ok, msg)
        self.assertNotIn(0x5555, self.core.refill_wait)
        self.run_(self.core.refill_start(0x5555, 50, "manual", "a"))
        self.core.refill_wait[0x5555]["until"] = time.time() - 1
        self.run_(self.core.refill_tick())
        self.assertNotIn(0x5555, self.core.refill_wait)
        self.core.last[0x5555]["level_mm_stale"] = False
        self.run_(self.core._refill_on_measurement(0x5555))
        self.assertNotIn(0x5555, self.core.refills)          # expired request never starts the valve
        self.assertEqual(self.calls, [])


class RefillCancelTest(RefillBase):
    def test_cancel_running_auto_pauses_automation(self):
        ok, msg = self.run_(self.core.refill_start(0x5555, 50, "auto", "system"))
        self.assertTrue(ok, msg)
        ok, msg = self.run_(self.core.refill_cancel(0x5555, "Ručně", "admin"))
        self.assertTrue(ok, msg)
        self.assertNotIn(0x5555, self.core.refills)
        self.assertIn("stop", self.calls)
        self.assertIsNotNone(self.core.refill_info(0x5555)["auto_paused_until"])
        # the next measurement must not start it again
        self.core._auto_block_until.clear()
        self.calls.clear()
        self.run_(self.core._refill_on_measurement(0x5555))
        self.assertNotIn(0x5555, self.core.refills)
        self.assertEqual(self.calls, [])
        # pause survives a restart, resume lifts it
        c2 = self.new_core()
        self.assertIn(0x5555, c2._auto_pause)
        ok, _ = c2.refill_resume_auto(0x5555, "admin")
        self.assertTrue(ok)
        self.assertIsNone(c2.refill_info(0x5555)["auto_paused_until"])
        self.assertNotIn(0x5555, self.new_core()._auto_pause)

    def test_cancel_rain_wait(self):
        self.core._rain_wait[0x5555] = {"start_pct": 10, "since": time.time(), "deadline": time.time() + 3600}
        ok, msg = self.run_(self.core.refill_cancel(0x5555, "Ručně", "admin"))
        self.assertTrue(ok, msg)
        self.assertNotIn(0x5555, self.core._rain_wait)
        self.assertIn(0x5555, self.core._auto_pause)
        self.assertEqual(self.calls, [])                 # nothing ran, no valve call needed

    def test_cancel_running_manual_does_not_pause_automation(self):
        self.start()
        ok, _ = self.run_(self.core.refill_cancel(0x5555, "Ručně", "admin"))
        self.assertTrue(ok)
        self.assertNotIn(0x5555, self.core._auto_pause)

    def test_cancel_waiting_manual(self):
        self.core.last[0x5555]["level_mm_stale"] = True
        self.run_(self.core.refill_start(0x5555, 50, "manual", "a"))
        ok, msg = self.run_(self.core.refill_cancel(0x5555, "Ručně", "admin"))
        self.assertTrue(ok, msg)
        self.assertNotIn(0x5555, self.core.refill_wait)
        self.assertEqual(self.calls, [])


class RefillSafetyTest(RefillBase):
    """The limits that keep a valve from overfilling a tank or running for hours."""

    def test_target_capped_at_max_refill_pct(self):
        ok, _ = self.run_(self.core.refill_start(0x5555, 100, "manual", "a"))
        self.assertTrue(ok)
        self.assertEqual(self.core.refills[0x5555]["target_pct"], 90.0)        # schema default max

    def test_run_time_capped_at_max_refill_minutes(self):
        self.core.nodes.update(0x5555, {"max_refill_minutes": 10})
        seen = []
        orig = self.core.hydrawise.call

        async def spy(key, relay, action, seconds=0):
            seen.append((action, seconds))
            return await orig(key, relay, action, seconds)
        self.core.hydrawise.call = spy
        self.start()                                 # 3000 L at 41.6 L/min would need ~79 min
        self.assertEqual(self.core.refills[0x5555]["planned_s"], 600)
        self.assertEqual(seen, [("run", 600)])

    def test_target_reached_closes_the_valve(self):
        rid = self.start()
        self.core.last[0x5555].update(ts=int(time.time()), level_mm=520)        # 52 % >= 50 %
        self.run_(self.core._refill_on_measurement(0x5555))
        self.assertEqual(self.calls, ["run", "stop"])
        self.assertNotIn(0x5555, self.core.refills)
        self.assertIsNotNone(self.row(rid)["end_ts"])
        self.assertIn("Dosaženo cíle", self.row(rid)["result"])

    def test_already_at_target_refused(self):
        self.core.last[0x5555]["level_mm"] = 500
        ok, msg = self.run_(self.core.refill_start(0x5555, 50, "manual", "a"))
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])

    def test_one_valve_never_serves_two_tanks_at_once(self):
        self.core.nodes.add(0x6666, "T2", {"tank_area_m2": 10.0, "max_depth_cm": 100.0,
                                           "refill_provider": "hydrawise", "hydrawise_relay_id": "7"})
        self.core.last[0x6666] = {"ts": int(time.time()), "level_mm": 200, "flags": 0}
        self.start()
        ok, msg = self.run_(self.core.refill_start(0x6666, 50, "manual", "a"))
        self.assertFalse(ok)
        self.assertIn("ventil", msg)
        self.assertEqual(self.calls, ["run"])


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


class DatabaseRecoveryTest(Base):
    def test_schema_too_new_raises_and_does_not_rename(self):
        self.core.db.close_thread()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA user_version = 999")
        finally:
            conn.close()

        with self.assertRaises(SchemaTooNewError):
            Database(self.path)
        with self.assertRaises(SchemaTooNewError):
            open_database(self.path)

        corrupt_files = [f for f in os.listdir(self.tmp.name) if ".corrupt-" in f]
        self.assertEqual(corrupt_files, [])
        self.assertTrue(os.path.exists(self.path))

    def test_nightly_backup_auto_restore(self):
        self.core.nodes.add(0x1111, "Tank 1")
        self.core.db.execute("INSERT INTO measurements(ts, node_id, seq, flags, volume_l) VALUES(1000, 0x1111, 1, 0, 500)")
        backup_path = self.core.backup_local()
        self.assertTrue(os.path.exists(backup_path))
        self.core.db.close_thread()

        with open(self.path, "wb") as f:
            f.write(b"not sqlite")

        def conn_with_cleanup(db_self):
            c = getattr(db_self._local, "conn", None)
            if c is None:
                c = sqlite3.connect(db_self.path, timeout=10, isolation_level=None, check_same_thread=True)
                try:
                    c.row_factory = sqlite3.Row
                    c.execute("PRAGMA journal_mode=WAL")
                    c.execute("PRAGMA synchronous=NORMAL")
                    c.execute("PRAGMA foreign_keys=ON")
                    c.execute("PRAGMA busy_timeout=5000")
                    c.execute("PRAGMA temp_store=MEMORY")
                    c.execute("PRAGMA cache_size=-1024")
                    db_self._local.conn = c
                except Exception:
                    c.close()
                    raise
            return c

        with mock.patch.object(Database, "conn", conn_with_cleanup):
            restored = open_database(self.path)
            try:
                self.assertEqual(restored.scalar("SELECT name FROM nodes WHERE node_id=?", (0x1111,)), "Tank 1")
                self.assertEqual(restored.scalar("SELECT volume_l FROM measurements WHERE node_id=?", (0x1111,)), 500)
            finally:
                restored.close_thread()

    def test_sensor_key_mirror_restores_node_and_secret(self):
        self.core.start_pairing(0x1234, "Nádrž", "admin")
        node = Node(self.core, 0x1234)
        self.assertEqual(node.try_pair(), "paired")
        node.send()
        keys_path = f"{self.path}.keys"
        self.assertTrue(os.path.exists(keys_path))
        orig_keys = self.core.nodes.keys(0x1234)
        self.assertIsNotNone(orig_keys)

        self.core.db.execute("DELETE FROM nodes")
        self.core.db.close_thread()

        db2 = Database(self.path)
        core2 = Core(db2, Settings(db2), self.tmp.name)
        try:
            self.assertIsNotNone(core2.nodes.get(0x1234))
            self.assertEqual(core2.nodes.keys(0x1234), orig_keys)
        finally:
            db2.close_thread()


if __name__ == "__main__":
    unittest.main()
