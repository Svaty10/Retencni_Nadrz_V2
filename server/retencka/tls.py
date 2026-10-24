"""HTTPS with the hub's own certificate authority – the application side.

The root helper (system/bin/retencka_tls.py, run by retencka-control) creates the CA and
the server certificate and publishes them in <data>/tls/ (ca.crt, server.crt, server.key,
status.json – written last). The app only reads them: it serves HTTPS once they exist and
restarts its HTTPS listener whenever status.json changes (renewal, new IP address).
While no certificate exists the app asks the helper for one (control-tls).
"""

from __future__ import annotations

import json
import logging
import os
import ssl
from typing import Any, Optional

log = logging.getLogger("retencka.tls")

DIR = "tls"


class TlsFiles:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.dir = os.path.join(data_dir, DIR)
        self.status: Optional[dict] = None
        self._stamp: Optional[tuple] = None

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def refresh(self) -> bool:
        """Re-read status.json; True when it changed since the last call."""
        try:
            st = os.stat(self.path("status.json"))
            stamp: Optional[tuple] = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        status = None
        if stamp is not None:
            try:
                with open(self.path("status.json"), "rb") as f:
                    status = json.loads(f.read(65536).decode("utf-8"))
            except (OSError, ValueError):
                status = None
        self.status = status if isinstance(status, dict) else None
        return True

    @property
    def ready(self) -> bool:
        return self.status is not None and os.path.isfile(self.path("server.crt")) \
            and os.path.isfile(self.path("server.key"))

    def context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.path("server.crt"), self.path("server.key"))
        return ctx

    def ca_pem(self) -> Optional[bytes]:
        try:
            with open(self.path("ca.crt"), "rb") as f:
                data = f.read(65536)
        except OSError:
            return None
        return data if data.startswith(b"-----BEGIN CERTIFICATE-----") else None

    def info(self) -> dict[str, Any]:
        s = self.status or {}
        return {"available": self.ca_pem() is not None, "fingerprint": s.get("fingerprint"),
                "ca_not_after": s.get("ca_not_after"), "cert_not_after": s.get("cert_not_after"),
                "names": list(s.get("names") or []), "ips": list(s.get("ips") or []),
                "issued_at": s.get("issued_at")}


class HttpsManager:
    """Owns the HTTPS listener: started when a certificate appears, restarted with a
    fresh SSL context when it is renewed. port 0 = HTTPS disabled."""

    def __init__(self, core, app, bind: str, port: int):
        self.core, self.app, self.bind, self.port = core, app, bind, port
        self.files = TlsFiles(core.data_dir)
        self.server = None
        self._asked = False

    @property
    def active(self) -> bool:
        return self.server is not None

    async def tick(self) -> None:
        from .httpserver import Server
        changed = self.files.refresh()
        if changed and self.files.status:
            self.core.tls_issued(self.files.status)
        if self.port <= 0:
            return
        if not self.files.ready:
            if not self._asked:                 # ask the root helper once per start
                self._asked = True
                try:
                    self.core.control_request("tls")
                except OSError:
                    pass
            return
        if self.server is not None and not changed:
            return
        try:
            ctx = self.files.context()
        except (OSError, ssl.SSLError) as e:
            log.error("HTTPS certificate unusable: %s", e)
            return
        if self.server is not None:
            self.server.close()
            self.server = None
        srv = Server(self.app, ctx)
        try:
            await srv.start(self.bind, self.port)
        except OSError as e:
            log.error("HTTPS port %d not available: %s", self.port, e)
            self.app["https_port"] = None
            return
        self.server = srv
        self.app["https_port"] = self.port
        log.info("HTTPS on :%d (%s)", self.port, ", ".join(self.files.info()["names"] + self.files.info()["ips"]))

    def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None
