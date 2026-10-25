#!/usr/bin/env python3
"""Captive portal for first Wi-Fi setup (runs only while the hotspot is active).

Every HTTP request on the hotspot is redirected here (nftables :80 -> :8088 and
DNS wildcard to 192.168.4.1), so phones open this page automatically.
The chosen SSID/password are handed to retencka-netwatch through
/run/retencka/wifi-req (root-only directory); the portal itself never touches
NetworkManager.
"""

import html
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

RUN = "/run/retencka"
REQ = os.path.join(RUN, "wifi-req")
PORT = 8088
MAX_BODY = 4096

CSS = """
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:linear-gradient(135deg,#0f172a,#1e293b);color:#f8fafc;min-height:100vh;display:flex;justify-content:center;align-items:flex-start}
.box{background:rgba(30,41,59,.85);border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:28px;margin:24px 16px;max-width:420px;width:100%;box-sizing:border-box}
h1{color:#38bdf8;font-size:24px;margin:0 0 6px}p{color:#94a3b8;font-size:14px;line-height:1.5}
label{display:block;margin:16px 0 6px;font-weight:600;font-size:14px;color:#94a3b8}
select,input{width:100%;padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,.15);background:rgba(15,23,42,.6);color:#fff;font-size:16px;box-sizing:border-box}
button{width:100%;margin-top:20px;padding:14px;border:0;border-radius:8px;background:linear-gradient(to right,#0ea5e9,#3b82f6);color:#fff;font-size:16px;font-weight:600}
a.btn{display:block;text-align:center;margin-top:12px;color:#38bdf8}
.err{background:rgba(239,68,68,.2);border:1px solid rgba(239,68,68,.4);color:#fca5a5;padding:10px;border-radius:8px;font-size:14px}
.ok{background:rgba(16,185,129,.15);border:1px solid rgba(16,185,129,.4);padding:12px;border-radius:8px}
code{color:#e0f2fe}
"""


def networks() -> list[tuple[str, int, str]]:
    best: dict[str, tuple[int, str]] = {}
    try:
        with open(os.path.join(RUN, "scan.txt"), encoding="utf-8", errors="replace") as f:
            for line in f:
                parts, cur, esc = [], "", False
                for ch in line.rstrip("\n"):
                    if esc:
                        cur += ch
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == ":":
                        parts.append(cur)
                        cur = ""
                    else:
                        cur += ch
                parts.append(cur)
                if len(parts) < 3 or not parts[0]:
                    continue
                ssid, sig, sec = parts[0], int(parts[1] or 0), parts[2]
                if ssid not in best or best[ssid][0] < sig:
                    best[ssid] = (sig, sec)
    except OSError:
        pass
    return sorted(((s, v[0], v[1]) for s, v in best.items()), key=lambda x: -x[1])[:40]


def last_result() -> str:
    try:
        with open(os.path.join(RUN, "wifi-result"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def page(body: str) -> bytes:
    host = socket.gethostname()
    return (f"<!DOCTYPE html><html lang='cs'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Retenční nádrž – nastavení Wi-Fi</title><style>{CSS}</style></head>"
            f"<body><div class='box'><h1>💧 Retenční nádrž</h1>"
            f"<p>Zařízení <code>{html.escape(host)}</code></p>{body}</div></body></html>").encode()


def form_page() -> bytes:
    res = last_result()
    err = ""
    if res.startswith("error:"):
        err = f"<div class='err'>{html.escape(res[6:])}</div>"
    opts = "".join(
        f"<option value='{html.escape(s, quote=True)}'>{html.escape(s)} "
        f"({sig} %{'' if sec and sec != '--' else ', otevřená'})</option>"
        for s, sig, sec in networks())
    if not opts:
        opts = "<option value=''>Žádné sítě nenalezeny – zadejte název ručně</option>"
    host = socket.gethostname()
    return page(f"""{err}
<p>Vyberte domácí Wi-Fi síť, ke které se má zařízení připojit (jen 2,4 GHz).
Tato stránka je dostupná i na <b>http://{html.escape(host)}.local</b> nebo <b>http://192.168.4.1</b>.</p>
<form method="post" action="/connect">
<label for="ssid">Wi-Fi síť</label>
<select id="ssid" name="ssid">{opts}</select>
<label for="ssid_manual">…nebo název skryté sítě</label>
<input id="ssid_manual" name="ssid_manual" maxlength="32" autocomplete="off">
<label for="psk">Heslo Wi-Fi</label>
<input id="psk" name="psk" type="password" maxlength="63" autocomplete="off">
<button type="submit">Připojit</button>
</form>
<a class="btn" href="/rescan">Znovu vyhledat sítě</a>""")


def done_page(ssid: str) -> bytes:
    host = socket.gethostname()
    return page(f"""<div class='ok'>Zařízení se teď připojuje k síti <b>{html.escape(ssid)}</b>.</div>
<p>Hotspot se za chvíli vypne. Připojte telefon zpět k domácí Wi-Fi a otevřete
<b>http://retencka.local</b> (nebo <b>http://{html.escape(host)}.local</b>).</p>
<p>Pokud se připojení nepovede, hotspot se do minuty znovu objeví a zde uvidíte chybu.</p>""")


class Handler(BaseHTTPRequestHandler):
    server_version = "retencka-portal"
    sys_version = ""
    timeout = 15

    def log_message(self, fmt, *args):  # quiet
        pass

    def send(self, body: bytes, status: int = 200, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/rescan"):
            open(os.path.join(RUN, "rescan"), "w").close()
            self.send(b"", 303, {"Location": "/"})
            return
        # every other URL (incl. OS connectivity checks) shows the portal
        self.send(form_page())

    def do_POST(self):
        if not self.path.startswith("/connect"):
            self.send(b"", 404)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if not 0 < n <= MAX_BODY:
            self.send(b"", 400)
            return
        data = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        ssid = (data.get("ssid_manual", [""])[0] or data.get("ssid", [""])[0]).strip()
        psk = data.get("psk", [""])[0]
        if not 1 <= len(ssid.encode()) <= 32 or ssid.startswith("-") \
                or (psk and not 8 <= len(psk) <= 63) \
                or any(ord(c) < 32 for c in ssid + psk):
            with open(os.path.join(RUN, "wifi-result"), "w") as f:
                f.write("error:Neplatný název sítě nebo heslo (heslo má 8–63 znaků).")
            self.send(b"", 303, {"Location": "/"})
            return
        os.makedirs(REQ, mode=0o700, exist_ok=True)
        for name, val in (("ssid", ssid), ("psk", psk)):
            tmp = os.path.join(REQ, name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(val)
            os.replace(tmp, os.path.join(REQ, name))
        open(os.path.join(REQ, "go"), "w").close()
        self.send(done_page(ssid))


def main() -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    srv.request_queue_size = 16
    threading.excepthook = lambda args: None
    srv.serve_forever()


if __name__ == "__main__":
    main()
