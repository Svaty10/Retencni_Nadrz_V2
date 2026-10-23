#!/usr/bin/env python3
"""Signed over-the-air updates from GitHub Releases with automatic rollback.

  1. ask GitHub for the newest release (stable = latest non-prerelease,
     beta = newest including pre-releases)
  2. download   retencka-<ver>.tar.gz  and  retencka-<ver>.tar.gz.sig
  3. verify the Ed25519 signature with /etc/retencka/release.pub (openssl)
  4. unpack into  /var/lib/retencka/app/releases/<ver>  (path-traversal safe)
  5. atomically switch the 'current' symlink, restart retencka.service
  6. wait up to 120 s for http://127.0.0.1:<RETENCKA_HA_PORT>/health to report the
     new version, otherwise switch back (application AND systemd units), restart
     and blacklist the broken version

Only the application directory changes; the database and settings live in
/var/lib/retencka/data and are never touched. Runs as root (oneshot).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = Path("/var/lib/retencka/app")
RELEASES = APP / "releases"
CURRENT = APP / "current"
DATA = Path("/var/lib/retencka/data")
STATUS = DATA / "update-status.json"
BAD = DATA / "update-bad-versions.json"
PUBKEY = Path("/etc/retencka/release.pub")
MAX_DOWNLOAD = 30 * 1024 * 1024
KEEP = 3


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
    """Health endpoint on the HA port; port and bind come from /etc/default/retencka."""
    port = env("RETENCKA_HA_PORT", "8081")
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


def write_status(**kw) -> None:
    kw["checked_at"] = int(time.time())
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(kw, ensure_ascii=False))
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATUS)


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


def find_release(repo: str, channel: str):
    if channel == "beta":
        rels = json.loads(http_get(f"https://api.github.com/repos/{repo}/releases?per_page=10", 2_000_000,
                                   "application/vnd.github+json"))
        rels = [r for r in rels if not r.get("draft")]
    else:
        rels = [json.loads(http_get(f"https://api.github.com/repos/{repo}/releases/latest", 2_000_000,
                                    "application/vnd.github+json"))]
    best = None
    for r in rels:
        ver = r.get("tag_name", "").lstrip("v")
        assets = {a["name"]: a["browser_download_url"] for a in r.get("assets", [])}
        tar, sig = f"retencka-{ver}.tar.gz", f"retencka-{ver}.tar.gz.sig"
        if tar in assets and sig in assets and (best is None or vtuple(ver) > vtuple(best[0])):
            best = (ver, assets[tar], assets[sig])
    return best


def verify(tar_path: Path, sig_path: Path) -> bool:
    if not PUBKEY.exists():
        raise RuntimeError(f"missing public key {PUBKEY}")
    res = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(PUBKEY), "-rawin",
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


def main() -> int:
    repo = env("RETENCKA_REPO", "Svaty10/Retencni_Nadrz_V2")
    channel = env("RETENCKA_CHANNEL", "stable")
    auto = env("RETENCKA_AUTO_UPDATE", "1") == "1"
    manual = (DATA / "update-request").exists()
    (DATA / "update-request").unlink(missing_ok=True)
    cur = current_version()
    try:
        bad = set(json.loads(BAD.read_text()))
    except (OSError, ValueError):
        bad = set()

    try:
        rel = find_release(repo, channel)
    except Exception as e:
        write_status(current=cur, message=f"Kontrola selhala: {e}")
        return 1
    if not rel or vtuple(rel[0]) <= vtuple(cur):
        write_status(current=cur, latest=rel[0] if rel else None, message=f"Aktuální verze {cur}")
        return 0
    ver, tar_url, sig_url = rel
    if ver in bad:
        write_status(current=cur, latest=ver, message=f"Verze {ver} dříve selhala – přeskočeno")
        return 0
    if not (auto or manual):
        write_status(current=cur, latest=ver, message=f"K dispozici verze {ver} (automatické aktualizace vypnuty)")
        return 0

    RELEASES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=APP) as tmpd:
        tmp = Path(tmpd)
        tar, sig = tmp / "r.tar.gz", tmp / "r.sig"
        try:
            tar.write_bytes(http_get(tar_url, MAX_DOWNLOAD))
            sig.write_bytes(http_get(sig_url, 4096))
            if not verify(tar, sig):
                write_status(current=cur, latest=ver, message=f"Verze {ver}: NEPLATNÝ PODPIS – odmítnuto")
                return 2
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
            write_status(current=cur, latest=ver, message=f"Stažení verze {ver} selhalo: {e}")
            return 1

    previous = CURRENT.resolve()
    switch(RELEASES / ver)
    units_backup: dict = {}
    try:
        sync_units(RELEASES / ver, units_backup)
    except OSError as e:
        print(f"unit sync failed: {e}", flush=True)
    subprocess.run(["systemctl", "restart", "retencka.service"], check=False)
    if healthy(ver):
        write_status(current=ver, latest=ver, message=f"Aktualizováno {cur} → {ver}")
        keep = sorted((p for p in RELEASES.iterdir() if p.is_dir()), key=lambda p: vtuple(p.name))[-KEEP:]
        for p in RELEASES.iterdir():
            if p.is_dir() and p not in keep and p.resolve() not in (previous, final.resolve()):
                shutil.rmtree(p, ignore_errors=True)
        return 0

    switch(previous)
    restore_units(units_backup)
    subprocess.run(["systemctl", "restart", "retencka.service"], check=False)
    bad.add(ver)
    BAD.write_text(json.dumps(sorted(bad)))
    write_status(current=cur, latest=ver, message=f"Verze {ver} nenaběhla – vráceno na {cur}")
    return 3


if __name__ == "__main__":
    sys.exit(main())
