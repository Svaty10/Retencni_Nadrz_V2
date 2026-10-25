"""Updates: the root updater's pure logic (loaded like install.sh does) and the app side."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import updates  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("retencka_update", ROOT / "system" / "bin" / "retencka-update.py")
upd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(upd)


class UpdaterParsing(unittest.TestCase):
    def test_request_modes(self):
        self.assertIsNone(upd.parse_request(None))                           # timer run
        self.assertEqual(upd.parse_request(b""), {"action": "check"})        # empty file
        self.assertEqual(upd.parse_request(b"1729000000"), {"action": "check"})   # old style
        self.assertEqual(upd.parse_request(b'{"action":"check"}'), {"action": "check"})
        self.assertEqual(upd.parse_request(b'{"action":"install","version":"1.2.0-beta1"}'),
                         {"action": "install", "version": "1.2.0-beta1"})
        for bad in (b'{"action":"install"}', b'{"action":"install","version":1.2}',
                    b'{"action":"install","version":"1.2.0; rm -rf /"}', b'{"action":"install","version":"../x"}',
                    b'{"action":"reboot"}', b'["install"]', b"\xff\xfe"):
            self.assertEqual(upd.parse_request(bad), {"action": "check"}, bad)

    def test_prefs_strict(self):
        self.assertEqual(upd.parse_prefs(b'{"auto": true, "channel": "stable"}'), {"auto": True, "channel": "stable"})
        for bad in (b"", b"null", b"[]", b'{"auto": "yes", "channel": "stable"}', b'{"auto": 0, "channel": "beta"}',
                    b'{"auto": true, "channel": "https://evil"}', b'{"auto": true}', b"{bad json"):
            self.assertIsNone(upd.parse_prefs(bad), bad)
        # extra keys (e.g. an attempt to inject a repo) are ignored, never returned
        self.assertEqual(upd.parse_prefs(b'{"auto": false, "channel": "beta", "repo": "evil/x"}'),
                         {"auto": False, "channel": "beta"})

    def test_read_small(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "f")
            self.assertIsNone(upd.read_small(p))
            p.write_bytes(b"x" * 10)
            self.assertEqual(upd.read_small(p, 100), b"x" * 10)
            self.assertEqual(upd.read_small(p, 5), b"")             # oversized = empty
            target = Path(d, "secret")
            target.write_text("top secret")
            link = Path(d, "link")
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not available")
            if hasattr(os, "O_NOFOLLOW"):
                self.assertEqual(upd.read_small(link), b"")         # planted symlink is not followed

    def test_decide(self):
        D = upd.decide
        self.assertEqual(D(None, True, "1.1.0", "1.1.0", set()), "up_to_date")
        self.assertEqual(D(None, True, "1.1.0", None, set()), "up_to_date")
        self.assertEqual(D({"action": "check"}, True, "1.1.0", "1.0.9", set()), "up_to_date")
        self.assertEqual(D(None, True, "1.1.0", "1.2.0", set()), "install")
        self.assertEqual(D(None, False, "1.1.0", "1.2.0", set()), "available")
        self.assertEqual(D(None, True, "1.1.0", "1.2.0", {"1.2.0"}), "failed_before")
        self.assertEqual(D({"action": "check"}, True, "1.1.0", "1.2.0", set()), "available")
        self.assertEqual(D({"action": "install", "version": "1.2.0"}, False, "1.1.0", "1.2.0", set()), "install")
        self.assertEqual(D({"action": "install", "version": "1.2.0"}, True, "1.1.0", "1.2.0", {"1.2.0"}), "install")
        self.assertEqual(D({"action": "install", "version": "1.2.0"}, True, "1.1.0", "1.3.0", set()), "mismatch")
        self.assertEqual(D({"action": "install", "version": "1.0.0"}, True, "1.1.0", "1.1.0", set()), "up_to_date")

    def test_changelog_sections(self):
        md = ("# Changelog\n\nintro\n\n## [Unreleased]\n- wip\n\n"
              "## [1.3.0] - 2026-12-01\n### Přidáno\n- c\n\n"
              "## [1.2.0] - 2026-11-01\n- b\n\n## [1.1.0] - 2026-10-01\n- a\n\n"
              "[1.3.0]: https://example/compare\n")
        secs = upd.changelog_sections(md, "1.1.0", "1.2.0")
        self.assertEqual([(s["version"], s["date"]) for s in secs], [("1.2.0", "2026-11-01")])
        self.assertEqual(secs[0]["markdown"], "- b")
        secs = upd.changelog_sections(md, "1.0.0", "1.3.0")
        self.assertEqual([s["version"] for s in secs], ["1.3.0", "1.2.0", "1.1.0"])
        self.assertIn("### Přidáno", secs[0]["markdown"])
        self.assertNotIn("example/compare", secs[0]["markdown"])
        self.assertEqual(upd.changelog_sections("no headings", "1.0", "2.0"), [])
        # the real changelog of this repository parses
        real = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        got = upd.changelog_sections(real, "0", "99.0.0")
        self.assertGreater(len(got), 3)
        self.assertTrue(all(updates.valid_version(s["version"]) and s["markdown"] for s in got))


class UpdaterMain(unittest.TestCase):
    """main() with the network and the install step replaced: check never installs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self.tmp.name)
        cur = d / "app" / "current" / "server" / "retencka"
        cur.mkdir(parents=True)
        (cur / "__init__.py").write_text('__version__ = "1.1.0"\n')
        data = d / "data"
        data.mkdir()
        self.saved = {k: getattr(upd, k) for k in ("CURRENT", "DATA", "STATUS", "REQUEST", "PREFS", "BAD",
                                                   "find_release", "fetch_notes", "install", "env")}
        upd.CURRENT, upd.DATA = d / "app" / "current", data
        upd.STATUS, upd.REQUEST = data / "update-status.json", data / "update-request"
        upd.PREFS, upd.BAD = data / "update-prefs.json", data / "update-bad-versions.json"
        upd.find_release = lambda repo, ch: {"version": "1.2.0", "tag": "v1.2.0", "published_at": "2026-11-01",
                                             "tar": "t", "sig": "s"}
        upd.fetch_notes = lambda repo, tag, cur, tgt: [{"version": "1.2.0", "date": "2026-11-01", "markdown": "- x"}]
        self.installed = []
        upd.install = lambda rel, cur, bad, st: self.installed.append(rel["version"]) or 0
        upd.env = lambda name, default: default
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()
    def tearDown(self):
        self._quiet.__exit__(None, None, None)
        for k, v in self.saved.items():
            setattr(upd, k, v)
        self.tmp.cleanup()

    def status(self):
        return json.loads(upd.STATUS.read_text(encoding="utf-8"))

    def test_web_check_only_reports(self):
        upd.REQUEST.write_text('{"action": "check"}')
        self.assertEqual(upd.main(), 0)
        st = self.status()
        self.assertEqual(st["state"], "available")
        self.assertEqual((st["current"], st["latest"], st["published_at"]), ("1.1.0", "1.2.0", "2026-11-01"))
        self.assertEqual(st["notes"][0]["markdown"], "- x")
        self.assertEqual(st["trigger"], "web")
        self.assertFalse(upd.REQUEST.exists())
        self.assertEqual(self.installed, [])

    def test_web_install_exact_version(self):
        upd.REQUEST.write_text('{"action": "install", "version": "1.2.0"}')
        upd.main()
        self.assertEqual(self.installed, ["1.2.0"])
        upd.REQUEST.write_text('{"action": "install", "version": "1.1.5"}')     # not the newest any more
        upd.main()
        self.assertEqual(self.installed, ["1.2.0"])
        self.assertEqual(self.status()["state"], "available")

    def test_timer_respects_prefs(self):
        upd.PREFS.write_text('{"auto": false, "channel": "stable"}')
        upd.main()
        self.assertEqual(self.installed, [])
        self.assertEqual(self.status()["trigger"], "timer")
        upd.PREFS.write_text('{"auto": true, "channel": "stable"}')
        upd.main()
        self.assertEqual(self.installed, ["1.2.0"])

    def test_up_to_date_and_error(self):
        upd.find_release = lambda repo, ch: {"version": "1.1.0", "tag": "v1.1.0", "published_at": None,
                                             "tar": "t", "sig": "s"}
        upd.main()
        self.assertEqual(self.status()["state"], "up_to_date")

        def boom(repo, ch):
            raise OSError("offline")
        upd.find_release = boom
        self.assertEqual(upd.main(), 1)
        st = self.status()
        self.assertEqual(st["state"], "error")
        self.assertIn("offline", st["message"])
        self.assertIsInstance(st["updated_at"], int)


class AppSide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_prefs_roundtrip_and_validation(self):
        self.assertIn(updates.read_prefs(self.d)["channel"], ("stable", "beta"))      # default
        self.assertEqual(updates.write_prefs(self.d, False, "beta"), {"auto": False, "channel": "beta"})
        self.assertEqual(updates.read_prefs(self.d), {"auto": False, "channel": "beta"})
        for auto, ch in ((1, "beta"), (True, "nightly"), ("true", "stable"), (None, None)):
            with self.assertRaises(ValueError):
                updates.write_prefs(self.d, auto, ch)
        # what the app writes is exactly what the root updater accepts
        raw = Path(self.d, updates.PREFS).read_bytes()
        self.assertEqual(upd.parse_prefs(raw), {"auto": False, "channel": "beta"})

    def test_request_matches_updater(self):
        updates.request(self.d, "install", "1.2.0")
        raw = Path(self.d, updates.REQUEST).read_bytes()
        self.assertEqual(upd.parse_request(raw), {"action": "install", "version": "1.2.0"})
        self.assertTrue(updates.pending(self.d))
        updates.request(self.d, "check")
        self.assertEqual(upd.parse_request(Path(self.d, updates.REQUEST).read_bytes()), {"action": "check"})
        with self.assertRaises(ValueError):
            updates.request(self.d, "install", "1.2; x")
        with self.assertRaises(ValueError):
            updates.request(self.d, "reboot")

    def test_busy(self):
        now = time.time()
        self.assertFalse(updates.busy(None))
        self.assertFalse(updates.busy({"state": "available", "updated_at": now}))
        self.assertTrue(updates.busy({"state": "installing", "updated_at": now}))
        self.assertFalse(updates.busy({"state": "installing", "updated_at": now - 3600}))   # stale



OPENSSL = shutil.which("openssl")


@unittest.skipIf(OPENSSL is None, "openssl not available")
class InstallAndRollback(unittest.TestCase):
    """install(): a really signed package, a really verified signature; only systemd,
    the symlink switch and the health check are replaced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = self.d = Path(self.tmp.name)
        self.saved = {k: getattr(upd, k) for k in ("APP", "RELEASES", "CURRENT", "DATA", "PUBKEY", "BAD", "http_get",
                                                   "switch", "sync_units", "restore_units", "healthy", "subprocess")}
        upd.APP = d / "app"
        upd.RELEASES = upd.APP / "releases"
        upd.DATA = d / "data"
        upd.DATA.mkdir(parents=True)
        prev = upd.RELEASES / "1.1.0"
        (prev / "server" / "retencka").mkdir(parents=True)
        upd.CURRENT = prev                                    # stands for the "current" symlink
        upd.BAD = d / "bad.json"
        key, upd.PUBKEY = d / "release.key", d / "release.pub"
        self.ossl("genpkey", "-algorithm", "ed25519", "-out", str(key))
        self.ossl("pkey", "-in", str(key), "-pubout", "-out", str(upd.PUBKEY))
        self.key = key
        self.files: dict = {}
        upd.http_get = lambda url, limit, accept="": self.files[url]
        self.switched, self.units_restored, self.restarts = [], [], []
        upd.switch = lambda target: self.switched.append(Path(target).name)
        upd.sync_units = lambda release, backup: backup.update({"unit": b"old"})
        upd.restore_units = lambda backup: self.units_restored.append(dict(backup))
        self.health = True
        upd.healthy = lambda expected, timeout=120: self.health
        real = self.saved["subprocess"]

        class Sub:                                            # systemctl / sync are not run
            CompletedProcess = real.CompletedProcess

            @staticmethod
            def run(cmd, *a, **kw):
                if cmd[0] in ("systemctl", "sync"):
                    self.restarts.append(cmd)
                    return real.CompletedProcess(cmd, 0, "", "")
                return real.run(cmd, *a, **kw)
        upd.subprocess = Sub
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()
        self.st = upd.Status("1.1.0", "web", path=d / "status.json")

    def tearDown(self):
        self._quiet.__exit__(None, None, None)
        for k, v in self.saved.items():
            setattr(upd, k, v)
        self.tmp.cleanup()

    def ossl(self, *args):
        subprocess.run([OPENSSL, *args], check=True, capture_output=True)

    def release(self, ver: str = "1.2.0", inner: str = None, extra: tuple = (), tamper: bool = False) -> dict:
        tar = self.d / f"r-{ver}.tar.gz"
        with tarfile.open(tar, "w:gz") as tf:
            def add(name, data):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            add("server/retencka/__init__.py", f'__version__ = "{inner or ver}"\n'.encode())
            add("server/retencka/__main__.py", b"")
            add("system/bin/retencka-update.py", b"")
            for name in extra:
                add(name, b"x")
        sig = tar.with_suffix(".sig")
        self.ossl("pkeyutl", "-sign", "-inkey", str(self.key), "-rawin", "-in", str(tar), "-out", str(sig))
        data = tar.read_bytes()
        if tamper:
            data = data[:-8] + bytes(8)
        self.files["t"], self.files["s"] = data, sig.read_bytes()
        return {"version": ver, "tar": "t", "sig": "s"}

    def state(self):
        return json.loads((self.d / "status.json").read_text(encoding="utf-8"))["state"]

    def test_healthy_update_switches_and_keeps_the_old_release(self):
        self.assertEqual(upd.install(self.release(), "1.1.0", set(), self.st), 0)
        self.assertEqual(self.switched, ["1.2.0"])
        self.assertTrue((upd.RELEASES / "1.2.0" / "server" / "retencka" / "__main__.py").is_file())
        self.assertTrue((upd.RELEASES / "1.1.0").is_dir())                 # kept for a rollback
        self.assertEqual(self.state(), "done")
        self.assertIn(["systemctl", "restart", "retencka.service"], self.restarts)
        self.assertEqual(self.units_restored, [])

    def test_unhealthy_update_rolls_back(self):
        self.health = False
        self.assertEqual(upd.install(self.release(), "1.1.0", set(), self.st), 3)
        self.assertEqual(self.switched, ["1.2.0", "1.1.0"])                 # back to the previous one
        self.assertEqual(self.units_restored, [{"unit": b"old"}])           # systemd units undone too
        self.assertEqual(json.loads(upd.BAD.read_text()), ["1.2.0"])        # timer will not retry it
        self.assertEqual(self.state(), "rolled_back")
        self.assertEqual(upd.decide(None, True, "1.1.0", "1.2.0", upd.load_bad()), "failed_before")

    def test_unhealthy_update_restores_database(self):
        import sqlite3
        db_file = upd.DATA / "retencka.db"
        with contextlib.closing(sqlite3.connect(str(db_file))) as conn:
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES ('before-update')")
            conn.execute("PRAGMA user_version = 13")
            conn.commit()

        def fail_after_migrating(expected, timeout=120):
            # Simulate the new version migrating the DB before failing healthcheck
            with contextlib.closing(sqlite3.connect(str(db_file))) as conn:
                conn.execute("UPDATE t SET v = 'mutated-by-failed-release'")
                conn.execute("PRAGMA user_version = 99")
                conn.commit()
            return False

        upd.healthy = fail_after_migrating
        self.assertEqual(upd.install(self.release(), "1.1.0", set(), self.st), 3)
        with contextlib.closing(sqlite3.connect(str(db_file))) as conn:
            val = conn.execute("SELECT v FROM t").fetchone()[0]
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(val, "before-update")
        self.assertEqual(ver, 13)
        self.assertFalse((upd.APP / ".db-pre-update.sqlite").exists())

    def test_no_switch_without_database_snapshot(self):
        (upd.DATA / "retencka.db").write_bytes(b"not a database")       # snapshot cannot be made
        self.assertEqual(upd.install(self.release(), "1.1.0", set(), self.st), 1)
        self.assertEqual(self.switched, [])                              # old version keeps running
        self.assertEqual(self.state(), "error")
        self.assertFalse((upd.APP / ".db-pre-update.sqlite").exists())

    def test_bad_signature_refused(self):
        self.assertEqual(upd.install(self.release(tamper=True), "1.1.0", set(), self.st), 2)
        self.assertEqual(self.switched, [])
        self.assertFalse((upd.RELEASES / "1.2.0").exists())
        self.assertEqual(self.state(), "error")

    def test_signed_but_unsafe_or_wrong_package_refused(self):
        for rel, why in ((self.release(extra=("../evil.py",)), "path traversal"),
                         (self.release(inner="1.3.0"), "version in the package differs"),
                         (self.release("1.0.9"), "downgrade")):
            self.assertEqual(upd.install(rel, "1.1.0", set(), self.st), 1, why)
            self.assertEqual(self.switched, [], why)
            self.assertEqual(self.state(), "error", why)
        self.assertFalse((self.d / "app" / "evil.py").exists())
        self.assertFalse((self.d / "evil.py").exists())
        self.assertEqual(sorted(p.name for p in upd.RELEASES.iterdir()), ["1.1.0"])


if __name__ == "__main__":
    unittest.main()
