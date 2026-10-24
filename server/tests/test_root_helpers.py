"""Root helpers vs. the unprivileged app: nothing written by root into the app's data
directory may follow a symlink (or trip over a directory) the app planted there."""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "system" / "bin"
BASH = shutil.which("bash")


def _symlinks_ok(d: str) -> bool:
    try:
        os.symlink(os.path.join(d, "x"), os.path.join(d, "probe"))
        os.unlink(os.path.join(d, "probe"))
        return True
    except (OSError, NotImplementedError):
        return False


@unittest.skipIf(BASH is None, "bash not available")
class IdentitySetupCode(unittest.TestCase):
    """retencka-identity.sh (root, every boot) publishes the label password for the app."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name, "base")        # /var/lib/retencka (root)
        self.data = self.base / "data"                 # /var/lib/retencka/data (app)
        self.data.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, pw: str) -> subprocess.CompletedProcess:
        script = (BIN / "retencka-identity.sh").as_posix()
        return subprocess.run([BASH, "-c", 'source "$1" && publish_setup_code "$2" "$3" "$4"', "_",
                               script, self.data.as_posix(), self.base.as_posix(), pw],
                              capture_output=True, text=True, timeout=30)

    def test_writes_the_password(self):
        r = self.publish("Abcdefgh23")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.data / "setup_code").read_text().strip(), "Abcdefgh23")
        self.assertEqual([p.name for p in self.base.iterdir()], ["data"])    # no temp file left
        if os.name == "posix":
            self.assertEqual((self.data / "setup_code").stat().st_mode & 0o777, 0o600)

    def test_planted_directory_is_replaced(self):
        (self.data / "setup_code").mkdir()
        (self.data / "setup_code" / "junk").write_text("x")
        r = self.publish("Abcdefgh23")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.data / "setup_code").is_file())
        self.assertEqual((self.data / "setup_code").read_text().strip(), "Abcdefgh23")

    def test_planted_symlink_is_not_followed(self):
        if not _symlinks_ok(self.tmp.name):
            self.skipTest("symlinks not available")
        victim = Path(self.tmp.name, "passwd")
        victim.write_text("root:x:0:0\n")
        os.symlink(victim, self.data / "setup_code")
        r = self.publish("Abcdefgh23")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(victim.read_text(), "root:x:0:0\n")             # target untouched
        self.assertFalse((self.data / "setup_code").is_symlink())
        self.assertEqual((self.data / "setup_code").read_text().strip(), "Abcdefgh23")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs")
    def test_planted_fifo_neither_blocks_nor_stays(self):
        os.mkfifo(self.data / "setup_code")
        r = self.publish("Abcdefgh23")                 # TimeoutExpired if it blocked on the FIFO
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.data / "setup_code").is_file())

    def test_unchanged_password_is_not_rewritten(self):
        self.assertEqual(self.publish("Abcdefgh23").returncode, 0)
        before = os.stat(self.data / "setup_code")
        self.assertEqual(self.publish("Abcdefgh23").returncode, 0)
        after = os.stat(self.data / "setup_code")
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))


class InstallScript(unittest.TestCase):
    def test_no_plain_writes_into_the_app_data_dir(self):
        """install.sh runs as root while the app may already be running (reinstall):
        `> "$DATA/…"` or `chown … "$DATA/…"` would follow a planted symlink."""
        for line_no, line in enumerate((ROOT / "system" / "install.sh").read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            self.assertNotRegex(code, r'>\s*"\$DATA/', f"install.sh:{line_no}")
            self.assertNotRegex(code, r'\bch(own|mod)\b[^|;&]*"\$DATA/', f"install.sh:{line_no}")


_spec = importlib.util.spec_from_file_location("retencka_control", BIN / "retencka-control.py")
ctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctl)


class ControlHelper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self.tmp.name)
        self.saved = {k: getattr(ctl, k) for k in ("DATA", "WANT", "STATUS", "NB_REQUEST", "NB_STATUS",
                                                   "apply_servis", "retencka_tls")}
        ctl.DATA, ctl.WANT, ctl.STATUS = d, d / "servis.json", d / "servis-status.json"
        ctl.NB_REQUEST, ctl.NB_STATUS = d / "control-netbird-install", d / "netbird-install.json"
        self.applied = []
        ctl.apply_servis = lambda: self.applied.append(True)
        ctl.retencka_tls = type("T", (), {"ensure": staticmethod(lambda: None)})
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()

    def tearDown(self):
        self._quiet.__exit__(None, None, None)
        for k, v in self.saved.items():
            setattr(ctl, k, v)
        self.tmp.cleanup()

    def test_planted_directory_does_not_stop_the_helper(self):
        (ctl.DATA / "control-tls").mkdir()
        (ctl.DATA / "control-servis").write_text("")
        ctl.main()                                   # must not raise
        self.assertEqual(self.applied, [True])
        self.assertFalse((ctl.DATA / "control-servis").exists())

    def test_wanted_reads_only_a_small_regular_file(self):
        ctl.WANT.write_text('{"mode": "on"}' + " " * 10000)       # oversized
        self.assertEqual(ctl.wanted(), {"mode": "off"})
        ctl.WANT.write_text('{"mode": "on"}')
        self.assertEqual(ctl.wanted(), {"mode": "on"})
        ctl.WANT.unlink()
        ctl.WANT.mkdir()
        self.assertEqual(ctl.wanted(), {"mode": "off"})


if __name__ == "__main__":
    unittest.main()
