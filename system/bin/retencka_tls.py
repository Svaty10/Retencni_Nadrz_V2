#!/usr/bin/env python3
"""retencka_tls – the hub's own certificate authority for HTTPS (island system).

No domain, no internet, no Let's Encrypt: the hub creates ONE local CA (10 years) and
signs a server certificate for retencka.local, <hostname>.local and its current LAN IPv4
addresses. A device installs the CA once (download in the web UI) and from then on
https://retencka.local is trusted.

  /var/lib/retencka/tls-ca/        root 0700 – ca.key (never leaves this directory),
                                   ca.crt, state.json, server.key/.crt (master copies)
  /var/lib/retencka/data/tls/      root 0755 – published for the app:
                                   ca.crt (0644), server.crt (0644),
                                   server.key (root:retencka 0640), status.json (0644)

The CA carries name constraints (.local names and private / CGNAT IPv4 ranges only), so
even a leaked CA key could not be used to impersonate other web sites on a device that
trusts it.

The server certificate (397 days, the limit browsers accept) is renewed 30 days before
it expires and whenever a current name / IP is not covered. Called by retencka-control.py
(timer every 5 minutes and on a "control-tls" request from the app); runs as root. The
data directory belongs to the unprivileged app, so everything published there is written
through a directory descriptor opened without following symlinks.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import time
import hashlib
from pathlib import Path
from typing import Optional

CA_DIR = Path("/var/lib/retencka/tls-ca")
PUB_DIR = Path("/var/lib/retencka/data/tls")
APP_GROUP = "retencka"
CA_DAYS = 3650
CERT_DAYS = 397
RENEW_BEFORE_S = 30 * 86400
ALIAS = "retencka.local"
# what the CA may sign (nameConstraints) – IPs outside these never go into a certificate
PERMITTED_NETS = [ipaddress.ip_network(n) for n in
                  ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")]
HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def log(msg: str) -> None:
    print(f"tls: {msg}", flush=True)


# ------------------------------------------------------------------ pure logic
def permitted_ip(ip: str) -> bool:
    """Private / CGNAT IPv4 only (what the CA's name constraints allow).

    >>> permitted_ip("192.168.1.20"), permitted_ip("8.8.8.8"), permitted_ip("127.0.0.1")
    (True, False, False)
    """
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and any(a in n for n in PERMITTED_NETS)


def parse_ip_addr(text: str) -> list[str]:
    """IPv4 addresses from `ip -4 -o addr show` (loopback and non-private skipped).

    >>> parse_ip_addr("1: lo    inet 127.0.0.1/8 scope host lo\\n3: wlan0    inet 192.168.1.20/24 brd 192.168.1.255 scope global wlan0")
    ['192.168.1.20']
    """
    out: list[str] = []
    for m in re.finditer(r"\binet\s+([0-9.]+)/\d+", text):
        ip = m.group(1)
        if permitted_ip(ip) and ip not in out:
            out.append(ip)
    return sorted(out, key=lambda x: ipaddress.ip_address(x))


def wanted_names(hostname: str) -> list[str]:
    """The shared alias + the unique name of this device.

    >>> wanted_names("retencka-ab12")
    ['retencka.local', 'retencka-ab12.local']
    """
    names = [ALIAS]
    h = (hostname or "").split(".")[0].lower()
    if HOST_RE.match(h) and f"{h}.local" not in names:
        names.append(f"{h}.local")
    return names


def renew_reason(state: Optional[dict], names: list[str], ips: list[str], now: float) -> Optional[str]:
    """Why the server certificate has to be (re)issued, None when it is fine."""
    if not state or not state.get("cert_not_after"):
        return "nový certifikát"
    if state["cert_not_after"] - now < RENEW_BEFORE_S:
        return "blíží se konec platnosti"
    have_n, have_i = set(state.get("names") or []), set(state.get("ips") or [])
    if not set(names) <= have_n:
        return "změna názvu zařízení"
    if not set(ips) <= have_i:
        return "změna IP adresy"
    return None


def ca_config(hostname: str) -> str:
    perm = ", ".join(["permitted;DNS:local"] +
                     [f"permitted;IP:{n.network_address}/{n.netmask}" for n in PERMITTED_NETS])
    return (
        "[req]\ndistinguished_name = dn\nprompt = no\n"
        f"[dn]\nO = Retencni nadrz\nCN = Retencka CA ({hostname})\n"
        "[v3_ca]\nbasicConstraints = critical, CA:TRUE, pathlen:0\n"
        "keyUsage = critical, keyCertSign, cRLSign\nsubjectKeyIdentifier = hash\n"
        f"nameConstraints = critical, {perm}\n")


def server_config(names: list[str], ips: list[str]) -> str:
    alt = [f"DNS.{i} = {n}" for i, n in enumerate(names, 1)] + \
          [f"IP.{i} = {ip}" for i, ip in enumerate(ips, 1)]
    return (
        "[req]\ndistinguished_name = dn\nprompt = no\n"
        f"[dn]\nCN = {names[-1]}\n"
        "[v3_srv]\nbasicConstraints = critical, CA:FALSE\nkeyUsage = critical, digitalSignature\n"
        "extendedKeyUsage = serverAuth\nsubjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid\n"
        "subjectAltName = @alt\n[alt]\n" + "\n".join(alt) + "\n")


def fingerprint(pem: bytes) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


# --------------------------------------------------------------------- openssl
def openssl(*args: str, cwd: Path) -> None:
    p = subprocess.run(["openssl", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"openssl {args[0]} failed: {(p.stderr or p.stdout).strip()[-300:]}")


def not_after(cert: Path) -> int:
    p = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-in", str(cert)],
                       capture_output=True, text=True, timeout=30)
    m = re.search(r"notAfter=(.+)", p.stdout)
    if p.returncode != 0 or not m:
        raise RuntimeError(f"cannot read {cert.name}")
    return int(ssl.cert_time_to_seconds(m.group(1).strip()))


def make_ca(work: Path, hostname: str) -> None:
    """ca.key + ca.crt in work."""
    (work / "ca.cnf").write_text(ca_config(hostname))
    openssl("req", "-x509", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", "ca.key", "-out", "ca.crt", "-days", str(CA_DAYS), "-sha256",
            "-config", "ca.cnf", "-extensions", "v3_ca", cwd=work)


def make_server(work: Path, names: list[str], ips: list[str]) -> None:
    """server.key + server.crt in work, signed by work/ca.key."""
    (work / "srv.cnf").write_text(server_config(names, ips))
    openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-keyout", "server.key", "-out", "server.csr", "-config", "srv.cnf", cwd=work)
    openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-set_serial", "0x" + secrets.token_hex(15), "-days", str(CERT_DAYS), "-sha256",
            "-extfile", "srv.cnf", "-extensions", "v3_srv", "-out", "server.crt", cwd=work)


# --------------------------------------------------------- safe file handling
def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _open_pub_dir(pub: Path, uid: int) -> int:
    """Directory fd of the published directory, guaranteed a real directory owned by
    root (anything else – a symlink or a directory planted by the app – is moved aside)."""
    try:
        st = os.lstat(pub)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
            os.rename(pub, pub.with_name(f"{pub.name}.bad-{int(time.time())}"))   # renames a symlink itself
            st = None
    except FileNotFoundError:
        st = None
    if st is None:
        os.mkdir(pub, 0o755)
    fd = os.open(pub, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        os.close(fd)
        raise RuntimeError("published directory is not owned by root")
    os.fchmod(fd, 0o755)
    return fd


def _write_in(dfd: int, name: str, data: bytes, mode: int, gid: Optional[int] = None) -> None:
    tmp = f".{name}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    try:
        os.write(fd, data)
        if gid is not None:
            os.fchown(fd, -1, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)


def _app_gid() -> Optional[int]:
    try:
        import grp
        return grp.getgrnam(APP_GROUP).gr_gid
    except (ImportError, KeyError):
        return None


def publish(ca_dir: Path, pub: Path, status: dict, uid: int = 0) -> None:
    """Copy the public pieces + the server key (readable by the app's group) for the app;
    status.json last – the app reloads when it changes."""
    dfd = _open_pub_dir(pub, uid)
    try:
        _write_in(dfd, "ca.crt", (ca_dir / "ca.crt").read_bytes(), 0o644)
        _write_in(dfd, "server.key", (ca_dir / "server.key").read_bytes(), 0o640, _app_gid())
        _write_in(dfd, "server.crt", (ca_dir / "server.crt").read_bytes(), 0o644)
        _write_in(dfd, "status.json", json.dumps(status).encode(), 0o644)
    finally:
        os.close(dfd)


def _published_ok(pub: Path) -> bool:
    return all((pub / n).is_file() for n in ("ca.crt", "server.crt", "server.key", "status.json"))


# ------------------------------------------------------------------ main entry
def current_ips() -> list[str]:
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_ip_addr(out)


def ensure(ca_dir: Path = CA_DIR, pub: Path = PUB_DIR, hostname: Optional[str] = None,
           ips: Optional[list[str]] = None, now: Optional[float] = None, uid: int = 0) -> Optional[str]:
    """Create the CA once, (re)issue the server certificate when needed, publish.
    Returns the reason when a certificate was issued, else None."""
    if shutil.which("openssl") is None:
        log("openssl missing – HTTPS disabled")
        return None
    now = time.time() if now is None else now
    hostname = hostname or socket.gethostname()
    names = wanted_names(hostname)
    ips = current_ips() if ips is None else [ip for ip in ips if permitted_ip(ip)]
    _private_dir(ca_dir)
    try:
        state = json.loads((ca_dir / "state.json").read_text())
    except (OSError, ValueError):
        state = {}
    have_ca = (ca_dir / "ca.key").is_file() and (ca_dir / "ca.crt").is_file()
    reason = None if have_ca else "nový certifikát"
    reason = reason or renew_reason(state, names, ips, now)
    if reason is None:
        if not _published_ok(pub):                      # e.g. the app's data directory was wiped
            publish(ca_dir, pub, state, uid)
        return None

    work = Path(tempfile.mkdtemp(dir=str(ca_dir), prefix="work-"))
    try:
        if have_ca:
            shutil.copy2(ca_dir / "ca.key", work / "ca.key")
            shutil.copy2(ca_dir / "ca.crt", work / "ca.crt")
        else:
            log("creating the hub CA")
            make_ca(work, names[-1].split(".")[0])
        make_server(work, names, ips)
        if not have_ca:
            os.replace(work / "ca.key", ca_dir / "ca.key")
            os.replace(work / "ca.crt", ca_dir / "ca.crt")
        os.replace(work / "server.key", ca_dir / "server.key")
        os.replace(work / "server.crt", ca_dir / "server.crt")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for f in ca_dir.iterdir():
        if f.is_file():
            os.chmod(f, 0o600)
    status = {"fingerprint": fingerprint((ca_dir / "ca.crt").read_bytes()),
              "ca_not_after": not_after(ca_dir / "ca.crt"), "cert_not_after": not_after(ca_dir / "server.crt"),
              "names": names, "ips": ips, "issued_at": int(now), "reason": reason}
    tmp = ca_dir / "state.json.tmp"
    tmp.write_text(json.dumps(status))
    os.replace(tmp, ca_dir / "state.json")
    publish(ca_dir, pub, status, uid)
    log(f"server certificate issued ({reason}): {', '.join(names + ips)}")
    return reason


if __name__ == "__main__":
    ensure()
