#!/usr/bin/env python3
"""Signed over-the-air updates from GitHub Releases with automatic rollback.

What a run does depends on how it was started:
  * web / Telegram request (/var/lib/retencka/data/update-request, JSON):
      {"action": "check"}                       – only look, never install
      {"action": "install", "version": "1.2.0"} – install exactly that version, if it is
                                                  still the newest signed release
      empty / old-style file                    – treated as "check"
  * timer (no request file) – install only when automatic updates are enabled
    (update-prefs.json written by the web, fallback /etc/default/retencka),
    otherwise just record that a new version is available.

Install steps:
  1. ask GitHub for the newest release (stable = latest non-prerelease,
     beta = newest including pre-releases)
  2. download   retencka-<ver>.tar.gz  and  retencka-<ver>.tar.gz.sig
  3. verify the Ed25519 signature with /etc/retencka/release.pub (openssl)
  4. unpack into  /var/lib/retencka/app/releases/<ver>  (path-traversal safe)
  5. atomically switch the 'current' symlink, restart retencka.service
  6. wait up to 120 s for http://127.0.0.1:<RETENCKA_HEALTH_PORT>/health to report the
     new version, otherwise switch back (application AND systemd units), restart
     and blacklist the broken version

Progress goes to update-status.json ("state"): checking → available | up_to_date | error,
downloading → verifying → installing → restarting → done | rolled_back | error.

Only the application directory changes; the database and settings live in
/var/lib/retencka/data and are never touched. Runs as root (oneshot). Everything read
from the data directory is written by the unprivileged app: it is read without following
symlinks, size-limited and validated strictly, and it never chooses URLs or paths.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import pwd
except ImportError:  # pragma: no cover – Windows test runs
    pwd = None  # type: ignore[assignment]

APP = Path("/var/lib/retencka/app")
RELEASES = APP / "releases"
CURRENT = APP / "current"
DATA = Path("/var/lib/retencka/data")
STATUS = DATA / "update-status.json"
REQUEST = DATA / "update-request"
PREFS = DATA / "update-prefs.json"
BAD = DATA / "update-bad-versions.json"
PUBKEY = Path("/etc/retencka/release.pub")
MAX_DOWNLOAD = 30 * 1024 * 1024
MAX_CHANGELOG = 512 * 1024
MAX_NOTE = 8000            # characters of release notes per version
MAX_NOTES = 20             # versions in one status
KEEP = 3
CHANNELS = ("stable", "beta")
VERSION_RE = re.compile(r"^\d{1,4}(\.\d{1,4}){1,3}(-[0-9A-Za-z.]{1,20})?\Z")


def env(name: str, default: str) -> str:
    vals: dict[str, str] = {}
    try:
        for line in Path("/etc/default/retencka").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return os.environ.get(name, vals.get(name, default))


def health_url() -> str:
    """Local health endpoint; port and bind come from /etc/default/retencka
    (RETENCKA_HA_PORT is the name used until 1.21)."""
    port = env("RETENCKA_HEALTH_PORT", env("RETENCKA_HA_PORT", "8081"))
    host = env("RETENCKA_BIND", "127.0.0.1").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/health"


def vtuple(v: str) -> tuple:
    """Sort key of a version; a pre-release is older than the final release.

    >>> vtuple("1.2.0-beta1") < vtuple("1.2.0")
    True
    >>> vtuple("v1.2.0") == vtuple("1.2")
    True
    >>> vtuple("1.2.0-beta2") < vtuple("1.2.0-beta10") < vtuple("1.2.0-rc1") < vtuple("1.2.0")
    True
    >>> vtuple("1.2.0") < vtuple("1.2.1-beta1") < vtuple("1.10.0")
    True
    """
    core, _, pre = v.lstrip("vV").split("+", 1)[0].partition("-")
    nums = [int(x) for x in re.findall(r"\d+", core)[:4]]
    nums += [0] * (4 - len(nums))
    ident = tuple((0, int(t), "") if t.isdigit() else (1, 0, t.lower())
                  for t in re.findall(r"\d+|[A-Za-z]+", pre))
    return (tuple(nums), 0 if pre else 1, ident)


# ------------------------------------------------- files shared with the app
def read_small(path: Path, limit: int = 4096) -> Optional[bytes]:
    """Content of a small regular file written by the unprivileged app, or None when it
    does not exist. Symlinks, FIFOs, devices and oversized files count as empty."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError:                      # e.g. ELOOP = a symlink planted instead of the file
        return b""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        data = os.read(fd, limit + 1)
        return b"" if len(data) > limit else data
    finally:
        os.close(fd)


def parse_request(raw: Optional[bytes]) -> Optional[dict]:
    """What the web asked for. None = no request file (timer run).

    >>> parse_request(None) is None
    True
    >>> parse_request(b"1729000000")                     # old style: timestamp
    {'action': 'check'}
    >>> parse_request(b'{"action": "install", "version": "1.2.0"}')
    {'action': 'install', 'version': '1.2.0'}
    >>> parse_request(b'{"action": "install", "version": "../../etc"}')
    {'action': 'check'}
    """
    if raw is None:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"action": "check"}
    if isinstance(obj, dict) and obj.get("action") == "install":
        ver = obj.get("version")
        if isinstance(ver, str) and VERSION_RE.match(ver):
            return {"action": "install", "version": ver}
    return {"action": "check"}


def parse_prefs(raw: Optional[bytes]) -> Optional[dict]:
    """update-prefs.json → {"auto": bool, "channel": "stable"|"beta"}; None if missing or
    invalid in any way (then /etc/default/retencka applies).

    >>> parse_prefs(b'{"auto": false, "channel": "beta"}')
    {'auto': False, 'channel': 'beta'}
    >>> parse_prefs(b'{"auto": 1, "channel": "beta"}') is None     # not a bool
    True
    >>> parse_prefs(b'{"auto": true, "channel": "nightly"}') is None
    True
    """
    if not raw:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    auto, channel = obj.get("auto"), obj.get("channel")
    if not isinstance(auto, bool) or channel not in CHANNELS:
        return None
    return {"auto": auto, "channel": channel}


def load_prefs() -> dict:
    p = parse_prefs(read_small(PREFS))
    if p is not None:
        return p
    channel = env("RETENCKA_CHANNEL", "stable")
    return {"auto": env("RETENCKA_AUTO_UPDATE", "1") == "1",
            "channel": channel if channel in CHANNELS else "stable"}


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` atomically. The temp name is random and created exclusively, mode and
    owner are set via the open file descriptor (never by path in the app's data directory),
    and rename() replaces a planted symlink instead of writing through it."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), 0o644)
            if pwd is not None and hasattr(os, "fchown"):
                try:
                    pw = pwd.getpwnam("retencka")
                    os.fchown(f.fileno(), pw.pw_uid, pw.pw_gid)
                except (KeyError, OSError):
                    pass
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Status:
    """update-status.json of this run; fields given once (latest, notes …) are kept."""

    def __init__(self, current: str, trigger: str, path: Optional[Path] = None):
        now = int(time.time())
        self.path = path or STATUS
        self.data: dict = {"current": current, "latest": None, "notes": [], "trigger": trigger,
                           "started_at": now}

    def __call__(self, state: str, message: str, **kw) -> dict:
        now = int(time.time())
        self.data.update(kw)
        self.data.update(state=state, message=message, updated_at=now, checked_at=now)
        try:
            write_atomic(self.path, json.dumps(self.data, ensure_ascii=False))
        except OSError as e:
            print(f"status write failed: {e}", flush=True)
        try:
            print(f"[{state}] {message}", flush=True)
        except UnicodeEncodeError:           # non-UTF-8 console (tests on Windows)
            print(f"[{state}] " + message.encode("ascii", "replace").decode(), flush=True)
        return self.data


def decide(req: Optional[dict], auto: bool, cur: str, latest: Optional[str], bad: set) -> str:
    """up_to_date | available | failed_before | mismatch | install

    >>> decide(None, True, "1.0.0", "1.1.0", set())                 # timer, automatic
    'install'
    >>> decide(None, False, "1.0.0", "1.1.0", set())                # timer, automatic off
    'available'
    >>> decide({"action": "check"}, True, "1.0.0", "1.1.0", set())  # web check never installs
    'available'
    >>> decide({"action": "install", "version": "1.1.0"}, False, "1.0.0", "1.2.0", set())
    'mismatch'
    """
    if latest is None or vtuple(latest) <= vtuple(cur):
        return "up_to_date"
    if req is not None and req.get("action") == "install":
        return "install" if req.get("version") == latest else "mismatch"
    if latest in bad:
        return "failed_before"
    return "install" if req is None and auto else "available"


HEAD_RE = re.compile(r"^##\s+\[?v?(\d[^\]\s]*)\]?\s*(?:[-–—(]\s*([0-9]{4}-[0-9]{2}-[0-9]{2}))?", re.M)


def changelog_sections(md: str, cur: str, target: str) -> list[dict]:
    """Sections of a Keep-a-Changelog file for versions cur < v <= target, newest first.

    >>> md = "# Changelog\\n## [1.2.0] - 2026-11-01\\n### Přidáno\\n- a\\n## [1.1.0] - 2026-10-01\\n- b\\n"
    >>> [(s["version"], s["date"]) for s in changelog_sections(md, "1.0.0", "1.2.0")]
    [('1.2.0', '2026-11-01'), ('1.1.0', '2026-10-01')]
    >>> [s["version"] for s in changelog_sections(md, "1.1.0", "1.2.0")]
    ['1.2.0']
    """
    heads = list(HEAD_RE.finditer(md))
    out = []
    for i, m in enumerate(heads):
        ver = m.group(1)
        if not VERSION_RE.match(ver) or not (vtuple(cur) < vtuple(ver) <= vtuple(target)):
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        body = md[m.end():end].split("\n", 1)[-1] if "\n" in md[m.end():end] else ""
        # stop at a following level-1/level-2 heading that is not a version (e.g. link refs)
        body = re.split(r"^\[[^\]]+\]:\s", body, maxsplit=1, flags=re.M)[0].strip()
        out.append({"version": ver, "date": m.group(2), "markdown": body[:MAX_NOTE]})
    out.sort(key=lambda s: vtuple(s["version"]), reverse=True)
    return out[:MAX_NOTES]


def current_version() -> str:
    try:
        init = (CURRENT / "server" / "retencka" / "__init__.py").read_text()
        return re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1)
    except (OSError, AttributeError):
        return "0"


def http_get(url: str, limit: int, accept: str = "application/octet-stream") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "retencka-updater", "Accept": accept})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("download too large")
    return data


def find_release(repo: str, channel: str) -> Optional[dict]:
    """Newest signed release: {version, tag, published_at, tar, sig} or None."""
    if channel == "beta":
        rels = json.loads(http_get(f"https://api.github.com/repos/{repo}/releases?per_page=10", 2_000_000,
                                   "application/vnd.github+json"))
        rels = [r for r in rels if not r.get("draft")]
    else:
        try:
            rels = [json.loads(http_get(f"https://api.github.com/repos/{repo}/releases/latest", 2_000_000,
                                        "application/vnd.github+json"))]
        except urllib.error.HTTPError as e:
            if e.code == 404:                   # no stable release published yet
                return None
            raise
    best = None
    for r in rels:
        tag = str(r.get("tag_name", ""))
        ver = tag.lstrip("v")
        if not VERSION_RE.match(ver):
            continue
        assets = {a["name"]: a["browser_download_url"] for a in r.get("assets", [])}
        tar, sig = f"retencka-{ver}.tar.gz", f"retencka-{ver}.tar.gz.sig"
        if tar in assets and sig in assets and (best is None or vtuple(ver) > vtuple(best["version"])):
            best = {"version": ver, "tag": tag, "published_at": str(r.get("published_at") or "")[:10] or None,
                    "tar": assets[tar], "sig": assets[sig]}
    return best


def fetch_notes(repo: str, tag: str, cur: str, target: str) -> list[dict]:
    """Release notes from CHANGELOG.md of the target tag; [] when unavailable."""
    try:
        md = http_get(f"https://raw.githubusercontent.com/{repo}/{tag}/CHANGELOG.md", MAX_CHANGELOG,
                      "text/plain").decode("utf-8", "replace")
        return changelog_sections(md, cur, target)
    except Exception as e:
        print(f"release notes unavailable: {e}", flush=True)
        return []


def pubkey() -> Path:
    """/etc/retencka/release.pub (copied by install.sh); fallback: the key shipped in
    the currently installed release – installed by root from the trusted checkout."""
    for p in (PUBKEY, CURRENT / "system" / "keys" / "release.pub"):
        if p.is_file():
            return p
    raise RuntimeError("chybí veřejný klíč release.pub (viz docs/instalace.md, Aktualizace)")


def verify(tar_path: Path, sig_path: Path) -> bool:
    key = pubkey()
    res = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key), "-rawin",
                          "-in", str(tar_path), "-sigfile", str(sig_path)],
                         capture_output=True, text=True, timeout=60)
    return res.returncode == 0 and "Signature Verified Successfully" in res.stdout


def safe_extract(tar_path: Path, dest: Path) -> None:
    with tarfile.open(tar_path, "r:gz") as tf:
        for m in tf.getmembers():
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep) and target != dest.resolve():
                raise RuntimeError(f"unsafe path in archive: {m.name}")
            if not (m.isfile() or m.isdir()):
                raise RuntimeError(f"unsupported member type: {m.name}")
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:  # pragma: no cover – very old Python
            tf.extractall(dest)


def sync_units(release: Path, backup: dict) -> None:
    """Install changed / new systemd units of the release (new path and timer units
    are enabled too), so features that need a unit also arrive by auto-update.
    `backup` receives {unit path: previous bytes or None (= did not exist)} so that
    restore_units() can undo everything on rollback."""
    src = release / "system" / "systemd"
    if not src.is_dir():
        return
    changed, new = False, []
    for u in sorted(src.iterdir()):
        dst = Path("/etc/systemd/system") / u.name
        data = u.read_bytes()
        if dst.exists() and dst.read_bytes() == data:
            continue
        if not dst.exists() and u.suffix in (".path", ".timer"):
            new.append(u.name)
        backup[dst] = dst.read_bytes() if dst.exists() else None
        dst.write_bytes(data)
        os.chmod(dst, 0o644)
        changed = True
    if changed:
        subprocess.run(["systemctl", "daemon-reload"], check=False)
    for name in new:
        subprocess.run(["systemctl", "enable", "--now", name], check=False)


def restore_units(backup: dict) -> None:
    """Undo sync_units(): put the old unit files back, remove the newly added ones."""
    if not backup:
        return
    for dst, old in backup.items():
        try:
            if old is None:
                if dst.suffix in (".path", ".timer"):
                    subprocess.run(["systemctl", "disable", "--now", dst.name], check=False)
                dst.unlink(missing_ok=True)
            else:
                dst.write_bytes(old)
                os.chmod(dst, 0o644)
        except OSError as e:
            print(f"unit restore failed for {dst}: {e}", flush=True)
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def stage_version(stage: Path) -> str:
    try:
        init = (stage / "server" / "retencka" / "__init__.py").read_text()
        return re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1)
    except (OSError, AttributeError):
        return ""


def switch(target: Path) -> None:
    tmp = APP / "current.new"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target.relative_to(APP))
    os.replace(tmp, CURRENT)
    subprocess.run(["sync"], check=False)


def healthy(expected: str, timeout: int = 120) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(5)
        try:
            with urllib.request.urlopen(health_url(), timeout=5) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:     # 503 = running but radio degraded
            try:
                body = json.loads(e.read())
            except ValueError:
                continue
        except Exception:
            continue
        if body.get("version") == expected:
            return True
    return False


def load_bad() -> set:
    try:
        v = json.loads((read_small(BAD, 65536) or b"[]").decode("utf-8"))
        return {x for x in v if isinstance(x, str)} if isinstance(v, list) else set()
    except (UnicodeDecodeError, ValueError):
        return set()


# Runs as the app user: reads the live database (and its -wal/-shm siblings, which
# sqlite opens by path) with the app's own rights and prints a serialized copy.
_SNAPSHOT_CHILD = (
    "import sqlite3, sys\n"
    "src = sqlite3.connect(sys.argv[1], timeout=30.0)\n"
    "mem = sqlite3.connect(':memory:')\n"
    "src.backup(mem)\n"
    "sys.stdout.buffer.write(mem.serialize())\n"
)


def _as_app_user() -> dict:
    """subprocess kwargs dropping root to the app user (empty when not root, e.g. tests)."""
    if pwd is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return {}
    pw = pwd.getpwnam("retencka")
    return {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": []}


def snapshot_db(db_path: Path, snap_path: Path) -> Optional[bool]:
    """Create a consistent SQLite backup in root-owned `snap_path` before switching
    to a new version, so a failed update can roll back schema migrations too.
    True = snapshot made, None = no database yet (nothing to protect), False = failed.
    The app is still running and owns the data directory, so root never opens the
    database there: sqlite opens (creates, and as root chowns) the -wal/-shm siblings
    by path, and the app could have planted links under those names. The backup runs
    in a child process as the app user, and root only writes its output."""
    snap_path.unlink(missing_ok=True)
    try:
        fd = os.open(db_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False
        if st.st_size == 0:
            return None
    finally:
        os.close(fd)
    try:
        r = subprocess.run([sys.executable, "-I", "-c", _SNAPSHOT_CHILD, str(db_path)],
                           capture_output=True, env={}, cwd="/", timeout=600, **_as_app_user())
        if r.returncode != 0 or not r.stdout.startswith(b"SQLite format 3\x00"):
            raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[-300:] or "no output")
        with open(snap_path, "wb") as f:
            f.write(r.stdout)
        with contextlib.closing(sqlite3.connect(str(snap_path))) as snap:
            if snap.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("snapshot failed quick_check")
        return True
    except Exception as e:
        print(f"db snapshot failed: {e}", flush=True)
        snap_path.unlink(missing_ok=True)
        return False


def restore_db(snap_path: Path, db_path: Path) -> None:
    """Restore the pre-update SQLite snapshot into `db_path` without following
    symlinks or calling path-based chown/chmod in the app's data directory."""
    if not snap_path.is_file():
        return
    fd, tmp = tempfile.mkstemp(dir=str(db_path.parent), prefix="." + db_path.name + ".")
    try:
        with os.fdopen(fd, "wb") as dst, open(snap_path, "rb") as src:
            shutil.copyfileobj(src, dst)
            dst.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(dst.fileno(), 0o640)
            if pwd is not None and hasattr(os, "fchown"):
                try:
                    pw = pwd.getpwnam("retencka")
                    os.fchown(dst.fileno(), pw.pw_uid, pw.pw_gid)
                except (KeyError, OSError):
                    pass
        for suf in ("-wal", "-shm"):
            aux = Path(str(db_path) + suf)
            if aux.is_dir() and not aux.is_symlink():
                shutil.rmtree(aux)
            else:
                aux.unlink(missing_ok=True)
        if db_path.is_dir() and not db_path.is_symlink():
            shutil.rmtree(db_path)
        os.replace(tmp, db_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        snap_path.unlink(missing_ok=True)


def install(rel: dict, cur: str, bad: set, st: Status) -> int:
    ver = rel["version"]
    RELEASES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=APP) as tmpd:
        tmp = Path(tmpd)
        tar, sig = tmp / "r.tar.gz", tmp / "r.sig"
        try:
            st("downloading", f"Stahuji verzi {ver}…", target=ver)
            tar.write_bytes(http_get(rel["tar"], MAX_DOWNLOAD))
            sig.write_bytes(http_get(rel["sig"], 4096))
            st("verifying", "Ověřuji podpis balíku…")
            if not verify(tar, sig):
                st("error", f"Verze {ver} má neplatný podpis – odmítnuto. Dál běží verze {cur}.")
                return 2
            st("installing", f"Instaluji verzi {ver}…")
            stage = tmp / "x"
            stage.mkdir()
            safe_extract(tar, stage)
            if not (stage / "server" / "retencka" / "__main__.py").exists():
                raise RuntimeError("archive does not contain server/retencka")
            got = stage_version(stage)
            if got != ver:
                raise RuntimeError(f"balík obsahuje verzi '{got}', očekávána {ver}")
            if vtuple(got) <= vtuple(cur):
                raise RuntimeError(f"verze {got} není novější než nainstalovaná {cur}")
            # tar may not keep the exec bit (or a Windows-built archive loses it)
            for f in (stage / "system" / "bin").glob("*"):
                if f.is_file():
                    os.chmod(f, 0o755)
            subprocess.run([sys.executable, "-m", "compileall", "-q", str(stage / "server")], check=False)
            final = RELEASES / ver
            if final.exists():
                shutil.rmtree(final)
            os.replace(stage, final)
        except Exception as e:
            st("error", f"Instalace verze {ver} selhala: {e}. Dál běží verze {cur}.")
            return 1

    previous = CURRENT.resolve()
    db_path = DATA / "retencka.db"
    db_snap = APP / ".db-pre-update.sqlite"
    snap = snapshot_db(db_path, db_snap)
    if snap is False:
        # without a snapshot a rollback would leave the old version on a newer schema,
        # which it refuses to open (db.SchemaTooNewError) – do not switch at all
        st("error", f"Instalace verze {ver} zrušena: nepodařilo se zálohovat databázi. Dál běží verze {cur}.")
        return 1
    has_db_snap = snap is True
    st("restarting", "Restartuji aplikaci…")
    switch(RELEASES / ver)
    units_backup: dict = {}
    try:
        sync_units(RELEASES / ver, units_backup)
    except OSError as e:
        print(f"unit sync failed: {e}", flush=True)
    subprocess.run(["systemctl", "restart", "retencka.service"], check=False)
    if healthy(ver):
        db_snap.unlink(missing_ok=True)
        st("done", f"Aktualizováno {cur} → {ver}", current=ver)
        keep = sorted((p for p in RELEASES.iterdir() if p.is_dir()), key=lambda p: vtuple(p.name))[-KEEP:]
        for p in RELEASES.iterdir():
            if p.is_dir() and p not in keep and p.resolve() not in (previous, final.resolve()):
                shutil.rmtree(p, ignore_errors=True)
        return 0

    if has_db_snap:
        subprocess.run(["systemctl", "stop", "retencka.service"], check=False)
    switch(previous)
    restore_units(units_backup)
    if has_db_snap:
        try:
            restore_db(db_snap, db_path)
        except OSError as e:
            print(f"db restore failed: {e}", flush=True)
    subprocess.run(["systemctl", "restart", "retencka.service"], check=False)
    bad.add(ver)
    try:
        write_atomic(BAD, json.dumps(sorted(bad)))
    except OSError as e:
        print(f"cannot record bad version: {e}", flush=True)
    st("rolled_back", f"Verze {ver} nenaběhla – vráceno na {cur}, dál běží předchozí verze.")
    return 3


def main() -> int:
    repo = env("RETENCKA_REPO", "Svaty10/Retencni_Nadrz_V2")     # never from the data directory
    prefs = load_prefs()
    req = parse_request(read_small(REQUEST))
    try:
        REQUEST.unlink()
    except FileNotFoundError:
        pass
    cur = current_version()
    st = Status(cur, "timer" if req is None else "web")
    st("checking", "Hledám aktualizace…", channel=prefs["channel"])
    bad = load_bad()

    try:
        rel = find_release(repo, prefs["channel"])
    except Exception as e:
        st("error", f"Kontrola aktualizací selhala: {e}")
        return 1
    latest = rel["version"] if rel else None
    what = decide(req, prefs["auto"], cur, latest, bad)
    if what == "up_to_date":
        st("up_to_date", f"Máte nejnovější verzi {cur}.", latest=latest)
        return 0

    st("checking", "Načítám poznámky k vydání…", latest=latest, published_at=rel["published_at"],
       notes=fetch_notes(repo, rel["tag"], cur, latest), failed_before=latest in bad)
    if what == "mismatch":
        st("available", f"Požadovaná verze {req['version']} už není nejnovější – k dispozici je {latest}.")
        return 0
    if what == "failed_before":
        st("available", f"Verze {latest} se dříve nepodařilo spustit; lze ji zkusit nainstalovat ručně.")
        return 0
    if what == "available":
        st("available", f"K dispozici je verze {latest}."
           + ("" if req is not None else " Automatické aktualizace jsou vypnuté."))
        return 0
    return install(rel, cur, bad, st)


if __name__ == "__main__":
    sys.exit(main())
