"""Root helpers vs. the unprivileged app: nothing written by root into the app's data
directory may follow a symlink (or trip over a directory) the app planted there."""
import ast
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


def _find_bash() -> str | None:
    candidates = [shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"]
    for b in candidates:
        if not b or not os.path.isfile(b):
            continue
        try:
            r = subprocess.run([b, "-c", "true"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return b
        except OSError:
            continue
    return None


BASH = _find_bash()


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
        """Root shell scripts must never `> "$DATA/…"` or `chown/chmod … "$DATA/…"`
        because that would follow a symlink planted by the unprivileged app."""
        scripts = [
            ROOT / "system" / "install.sh",
            BIN / "retencka-identity.sh",
            BIN / "retencka-netbird-install.sh",
            BIN / "retencka-alias.sh",
            BIN / "retencka-netwatch.sh",
        ]
        for path in scripts:
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                self.assertNotRegex(code, r'>\s*"\$(DATA|1)/', f"{path.name}:{line_no}")
                self.assertNotRegex(code, r'\bch(own|mod)\b[^|;&]*"\$(DATA|1)/', f"{path.name}:{line_no}")

    def test_python_root_helpers_never_chown_or_chmod_by_path_in_data_writers(self):
        """GHSA-pc46-j66c-c3w2: write_json, write_atomic and restore_db write into the
        app-owned data directory and must set mode/owner via fchmod/fchown on the open
        descriptor, never via path-based os.chmod / os.chown / shutil.chown."""
        checks = [
            (BIN / "retencka-control.py", ("write_json",)),
            (BIN / "retencka-update.py", ("write_atomic", "restore_db")),
        ]
        for script, fn_names in checks:
            tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
            funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
            for fn_name in fn_names:
                self.assertIn(fn_name, funcs, f"{script.name}:{fn_name}")
                for node in ast.walk(funcs[fn_name]):
                    if isinstance(node, ast.Call):
                        call_src = ast.unparse(node.func)
                        self.assertNotIn(
                            call_src,
                            ("os.chmod", "os.chown", "shutil.chown", "Path.chmod"),
                            f"{script.name}:{fn_name} must not call {call_src} by path",
                        )


_spec = importlib.util.spec_from_file_location("retencka_control", BIN / "retencka-control.py")
ctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctl)


class ControlHelper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self.tmp.name)
        self.saved = {k: getattr(ctl, k) for k in ("DATA", "WANT", "STATUS", "NB_REQUEST", "NB_STATUS",
                                                   "apply_servis", "OLD_TLS_DIRS")}
        ctl.DATA, ctl.WANT, ctl.STATUS = d, d / "servis.json", d / "servis-status.json"
        ctl.NB_REQUEST, ctl.NB_STATUS = d / "control-netbird-install", d / "netbird-install.json"
        self.applied = []
        ctl.apply_servis = lambda: self.applied.append(True)
        ctl.OLD_TLS_DIRS = (d / "tls-ca", d / "tls")
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

    def test_old_https_files_removed_without_following_links(self):
        outside = Path(self.tmp.name, "outside")
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        ca = ctl.DATA / "tls-ca"
        ca.mkdir()
        (ca / "ca.key").write_text("k")
        try:
            os.symlink(outside, ctl.DATA / "tls")          # planted by the app
        except (OSError, NotImplementedError):
            (ctl.DATA / "tls").mkdir()
        ctl.main()
        self.assertFalse(ca.exists())
        self.assertFalse(os.path.lexists(ctl.DATA / "tls"))
        self.assertTrue((outside / "keep.txt").exists())

    def test_wanted_reads_only_a_small_regular_file(self):
        ctl.WANT.write_text('{"mode": "on"}' + " " * 10000)       # oversized
        self.assertEqual(ctl.wanted(), {"mode": "off"})
        ctl.WANT.write_text('{"mode": "on"}')
        self.assertEqual(ctl.wanted(), {"mode": "on"})
        ctl.WANT.unlink()
        ctl.WANT.mkdir()
        self.assertEqual(ctl.wanted(), {"mode": "off"})

    def test_reboot_dangling_symlink_is_unlinked(self):
        if not _symlinks_ok(self.tmp.name):
            self.skipTest("symlinks not available")
        os.symlink(os.path.join(self.tmp.name, "missing"), ctl.DATA / "control-reboot")
        ran = []
        orig = ctl.subprocess.run
        ctl.subprocess.run = lambda argv, **kw: ran.append(argv)
        try:
            ctl.main()
        finally:
            ctl.subprocess.run = orig
        self.assertEqual(ran, [["systemctl", "reboot"]])
        self.assertFalse(os.path.lexists(ctl.DATA / "control-reboot"))

    def test_write_json_never_uses_path_chmod_or_chown_and_replaces_symlink_or_dir(self):
        orig_chmod, orig_shchown = ctl.os.chmod, ctl.shutil.chown

        def forbid(*a, **kw):
            raise AssertionError(f"path-based chmod/chown called: {a}")

        ctl.os.chmod = forbid
        ctl.shutil.chown = forbid
        try:
            # 1. normal write + planted directory at target
            ctl.STATUS.mkdir()
            (ctl.STATUS / "planted").write_text("x")
            ctl.write_json(ctl.STATUS, {"ok": 1})
            self.assertTrue(ctl.STATUS.is_file())
            # 2. planted symlink at target is replaced, target untouched
            if _symlinks_ok(self.tmp.name):
                victim = Path(self.tmp.name, "victim")
                victim.write_text("secret")
                ctl.STATUS.unlink()
                os.symlink(victim, ctl.STATUS)
                ctl.write_json(ctl.STATUS, {"ok": 2})
                self.assertFalse(ctl.STATUS.is_symlink())
                self.assertEqual(victim.read_text(), "secret")
        finally:
            ctl.os.chmod = orig_chmod
            ctl.shutil.chown = orig_shchown


if __name__ == "__main__":
    unittest.main()
