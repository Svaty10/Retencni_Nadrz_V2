"""Updates: the root updater's pure logic (loaded like install.sh does) and the app side."""
import contextlib
import importlib.util
import io
import json
import os
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


if __name__ == "__main__":
    unittest.main()
