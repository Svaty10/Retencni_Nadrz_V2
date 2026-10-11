"""HTTP layer (retencka.httpserver): web application on :80 and Home Assistant API on :8081."""

from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import mimetypes
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import httpserver as web

from . import __version__, auth, model
from .core import Core
from .settings import SettingsError

log = logging.getLogger("retencka.web")

COOKIE_PLAIN = "rt_sid"
COOKIE_SECURE = "__Host-rt_sid"
MAX_BODY = 64 * 1024

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def jresp(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=dumps)


def jerr(msg: str, status: int = 400, **extra) -> web.Response:
    return jresp({"ok": False, "error": msg, **extra}, status)


# =============================================================== client info
def _is_loopback(addr: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address(addr or "").is_loopback
    except ValueError:
        return False


def client_ip(request: web.Request) -> str:
    """Trust proxy headers only from the local cloudflared/nginx (loopback)."""
    peer = request.remote or ""
    if _is_loopback(peer):
        cf = request.headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip()[:64]
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()[:64]
    return peer


def is_https(request: web.Request) -> bool:
    if request.secure:
        return True
    return _is_loopback(request.remote) and request.headers.get("X-Forwarded-Proto", "").lower() == "https"


async def read_json(request: web.Request) -> dict:
    if request.content_length and request.content_length > MAX_BODY:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=request.content_length)
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=dumps({"ok": False, "error": "Neplatný JSON"}),
                                 content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text=dumps({"ok": False, "error": "Očekáván objekt"}),
                                 content_type="application/json")
    return data


# ============================================================== static files
class Static:
    """In-memory cached static files with ETag and transparent gzip."""

    COMPRESS = {".js", ".css", ".html", ".svg", ".json", ".txt", ".md"}

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self._cache: dict[str, tuple[float, bytes, Optional[bytes], str, str]] = {}

    def _load(self, rel: str):
        path = (self.root / rel).resolve()
        if self.root not in path.parents or not path.is_file():
            return None
        mtime = path.stat().st_mtime
        hit = self._cache.get(rel)
        if hit and hit[0] == mtime:
            return hit
        body = path.read_bytes()
        gz = gzip.compress(body, 9) if path.suffix in self.COMPRESS and len(body) > 1024 else None
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".woff2":
            ctype = "font/woff2"
        etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
        entry = (mtime, body, gz, ctype, etag)
        self._cache[rel] = entry
        return entry

    def response(self, request: web.Request, rel: str, cache: str = "no-cache") -> web.StreamResponse:
        entry = self._load(rel)
        if entry is None:
            raise web.HTTPNotFound()
        _, body, gz, ctype, etag = entry
        headers = {"ETag": etag, "Cache-Control": cache}
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        if gz is not None:
            headers["Vary"] = "Accept-Encoding"
            if "gzip" in request.headers.get("Accept-Encoding", ""):
                headers["Content-Encoding"] = "gzip"
                body = gz
        charset = "utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else None
        return web.Response(body=body, content_type=ctype, charset=charset, headers=headers)


# ================================================================ middleware
def _apply_security_headers(request: web.Request, h) -> None:
    h.setdefault("Content-Security-Policy", CSP)
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    if is_https(request):
        h["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/api/"):
        h["Cache-Control"] = "no-store"


async def security_headers(request: web.Request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        _apply_security_headers(request, e.headers)
        raise
    except Exception:
        log.exception("Unhandled error on %s %s", request.method, request.path)
        resp = jerr("Interní chyba serveru", 500)
    _apply_security_headers(request, resp.headers)
    return resp


def make_auth_middleware(core: Core):
    async def authenticate(request: web.Request, handler):
        token = request.cookies.get(COOKIE_SECURE) or request.cookies.get(COOKIE_PLAIN)
        request["session"] = core.sessions.lookup(token) if token else None
        request["token"] = token
        request["ip"] = client_ip(request)

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # CSRF: Origin must match Host (when sent) ...
            origin = request.headers.get("Origin")
            if origin:
                host = request.headers.get("X-Forwarded-Host") if _is_loopback(request.remote) else None
                host = host or request.host
                if origin.split("://", 1)[-1] != host:
                    return jerr("Neplatný původ požadavku", 403)
            # ... and logged-in requests must carry the session's CSRF token
            sess = request["session"]
            if sess and request.path not in ("/api/auth/login", "/api/auth/setup"):
                sent = request.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(sent, sess["csrf"]):
                    return jerr("Neplatný CSRF token – obnovte stránku", 403)
        return await handler(request)
    return authenticate


def need(role: Optional[str] = None):
    """Decorator for API handlers that require a session (and optionally admin)."""
    def deco(fn: Callable):
        async def wrapper(request: web.Request):
            sess = request.get("session")
            if not sess:
                return jerr("Nejste přihlášen", 401)
            if role == "admin" and sess["role"] != "admin":
                return jerr("Nedostatečná oprávnění", 403)
            return await fn(request)
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


# ================================================================= web app
def build_web_app(core: Core, static_dir: str) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY,
                          middlewares=[security_headers, make_auth_middleware(core)])
    app.prepare_hooks.append(lambda req, resp: _apply_security_headers(req, resp.headers))
    st = Static(static_dir)
    db, settings = core.db, core.settings

    def no_users() -> bool:
        return db.scalar("SELECT COUNT(*) FROM users", default=0) == 0

    def set_session_cookie(request: web.Request, resp: web.Response, token: str) -> None:
        secure = is_https(request)
        resp.set_cookie(COOKIE_SECURE if secure else COOKIE_PLAIN, token, max_age=auth.SESSION_ABSOLUTE_S,
                        path="/", httponly=True, secure=secure, samesite="Lax")

    def clear_cookies(resp: web.Response) -> None:
        resp.del_cookie(COOKIE_PLAIN, path="/")
        resp.set_cookie(COOKIE_SECURE, "", max_age=0, path="/", secure=True, httponly=True, samesite="Lax")

    # ------------------------------------------------- first login (bootstrap)
    # While no user exists, the only way in is the default account "admin" with the
    # password printed on the device label (= hotspot password). It grants nothing but
    # a short-lived ticket for creating the real administrator account; afterwards the
    # default credentials stop working because users exist.
    SETUP_COOKIE = "rt_setup"
    setup_tickets: dict[str, float] = {}

    def setup_password() -> str:
        try:
            return Path(core.data_dir, "setup_code").read_text().strip()
        except OSError:
            return ""

    def valid_ticket(request) -> bool:
        tok = request.cookies.get(SETUP_COOKIE, "")
        exp = setup_tickets.get(tok)
        return bool(tok) and exp is not None and exp > time.time()

    # ----------------------------------------------------------------- pages
    async def page_index(request):
        if no_users():
            raise web.HTTPFound("/setup" if valid_ticket(request) else "/login")
        if not request["session"]:
            raise web.HTTPFound("/login")
        return st.response(request, "index.html")

    async def page_login(request):
        if request["session"] and not no_users():
            raise web.HTTPFound("/")
        return st.response(request, "login.html")

    async def page_setup(request):
        if not no_users() or not valid_ticket(request):
            raise web.HTTPFound("/login")
        return st.response(request, "setup.html")

    async def static_file(request):
        rel = request.match_info["path"]
        long_cache = rel.startswith(("fonts/", "vendor/"))
        return st.response(request, rel, "public, max-age=604800" if long_cache else "no-cache")

    async def favicon(request):
        return st.response(request, "favicon.svg", "public, max-age=604800")

    # ------------------------------------------------------------------ auth
    async def api_auth_mode(request):
        return jresp({"first_login": no_users()})

    async def api_login(request):
        ip = request["ip"]
        wait = core.ip_limiter.retry_after(ip)
        if wait:
            return jerr(f"Příliš mnoho pokusů. Zkuste to za {wait} s.", 429, retry_after=wait)
        data = await read_json(request)
        username = str(data.get("username", ""))[:64].strip()
        password = str(data.get("password", ""))[:256]
        now = int(time.time())

        if no_users():
            expected = setup_password()
            ok = (username.lower() == "admin" and bool(expected)
                  and hmac.compare_digest(password.strip().encode(), expected.encode()))
            if not ok:
                core.ip_limiter.fail(ip)
                core.audit("SETUP_FAILED", "Neplatné výchozí přihlášení", username or "-", ip)
                await asyncio.sleep(0.5 + secrets.randbelow(500) / 1000)
                return jerr("Neplatné jméno nebo heslo. Při prvním přihlášení použijte jméno admin "
                            "a heslo ze štítku zařízení.", 401)
            core.ip_limiter.success(ip)
            for t, exp in list(setup_tickets.items()):
                if exp < time.time():
                    setup_tickets.pop(t, None)
            tok = secrets.token_urlsafe(32)
            setup_tickets[tok] = time.time() + 15 * 60
            core.audit("SETUP_LOGIN", "První přihlášení výchozím účtem – vytváření vlastního účtu", "admin", ip)
            resp = jresp({"ok": True, "setup": True})
            resp.set_cookie(SETUP_COOKIE, tok, max_age=15 * 60, path="/", httponly=True,
                            secure=is_https(request), samesite="Strict")
            return resp

        row = db.one("SELECT * FROM users WHERE username = ?", (username,)) if username else None
        if row and row["locked_until"] > now:
            core.ip_limiter.fail(ip)
            core.audit("LOGIN_FAILED", f"Účet {username!r} je dočasně zamčen", username, ip)
            return jerr("Účet je dočasně zamčen kvůli opakovaným neúspěšným pokusům.", 429)
        ok = await asyncio.get_running_loop().run_in_executor(
            None, auth.verify_password, password, row["pw_hash"] if row else None)
        if not ok:
            core.ip_limiter.fail(ip)
            if row:
                fails = row["failed"] + 1
                lock = now + auth.USER_LOCK_S if fails >= auth.USER_MAX_FAILS else 0
                db.execute("UPDATE users SET failed=?, locked_until=? WHERE username=?",
                           (0 if lock else fails, lock, row["username"]))
            core.audit("LOGIN_FAILED", "Neplatné jméno nebo heslo", username or "(prázdné)", ip)
            await asyncio.sleep(0.5 + secrets.randbelow(500) / 1000)
            return jerr("Neplatné jméno nebo heslo.", 401)
        core.ip_limiter.success(ip)
        db.execute("UPDATE users SET failed=0, locked_until=0, last_login=? WHERE username=?",
                   (now, row["username"]))
        core.sessions.delete(request["token"])            # no session fixation
        token, _ = core.sessions.create(row["username"], ip, request.headers.get("User-Agent", ""))
        core.audit("LOGIN_SUCCESS", "Přihlášení", row["username"], ip)
        resp = jresp({"ok": True})
        set_session_cookie(request, resp, token)
        return resp

    async def api_logout(request):
        sess = request["session"]
        if sess:
            core.audit("LOGOUT", "Odhlášení", sess["username"], request["ip"])
        core.sessions.delete(request["token"])
        resp = jresp({"ok": True})
        clear_cookies(resp)
        return resp

    async def api_setup(request):
        """Create the first own administrator account (requires the bootstrap ticket)."""
        ip = request["ip"]
        if not no_users():
            return jerr("Systém už je nastaven.", 409)
        if not valid_ticket(request):
            return jerr("Platnost prvního přihlášení vypršela – přihlaste se znovu heslem ze štítku.", 403)
        data = await read_json(request)
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if username.lower() == "admin":
            return jerr("Zvolte si vlastní jméno – výchozí účet „admin“ bude zrušen.")
        problem = auth.username_problem(username) or auth.password_problem(username, password)
        if problem:
            return jerr(problem)
        if hmac.compare_digest(password.encode(), setup_password().encode()):
            return jerr("Nové heslo nesmí být stejné jako heslo ze štítku.")
        pw_hash = await asyncio.get_running_loop().run_in_executor(None, auth.hash_password, password)
        now = int(time.time())
        db.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES(?,?,?,?,?)",
                   (username, pw_hash, "admin", now, now))
        setup_tickets.clear()
        core.audit("USER_ADDED", f"Vytvořen administrátor {username!r}; výchozí účet admin zrušen", username, ip)
        token, _ = core.sessions.create(username, ip, request.headers.get("User-Agent", ""))
        resp = jresp({"ok": True})
        resp.del_cookie(SETUP_COOKIE, path="/")
        set_session_cookie(request, resp, token)
        return resp

    @need()
    async def api_me(request):
        s = request["session"]
        return jresp({"username": s["username"], "role": s["role"], "csrf": s["csrf"],
                      "version": __version__, "hostnames": core.hostnames()})

    @need()
    async def api_me_password(request):
        s = request["session"]
        data = await read_json(request)
        old, new = str(data.get("old_password", "")), str(data.get("new_password", ""))
        row = db.one("SELECT pw_hash FROM users WHERE username=?", (s["username"],))
        loop = asyncio.get_running_loop()
        if not row or not await loop.run_in_executor(None, auth.verify_password, old, row["pw_hash"]):
            core.ip_limiter.fail(request["ip"])
            return jerr("Současné heslo není správné.")
        problem = auth.password_problem(s["username"], new)
        if problem:
            return jerr(problem)
        h = await loop.run_in_executor(None, auth.hash_password, new)
        db.execute("UPDATE users SET pw_hash=?, pw_changed=? WHERE username=?",
                   (h, int(time.time()), s["username"]))
        core.sessions.delete_user_sessions(s["username"], except_hash=s["id_hash"])
        core.audit("PASSWORD_CHANGED", "Změna vlastního hesla (ostatní relace odhlášeny)",
                   s["username"], request["ip"])
        return jresp({"ok": True})

    # ------------------------------------------------------------------ data
    def time_range(request) -> tuple[int, int]:
        now = int(time.time())
        q = request.query
        try:
            if "from" in q:
                t_from = int(float(q["from"]))
                t_to = int(float(q.get("to", now)))
            else:
                spans = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "365d": 365 * 86400}
                t_to, t_from = now, now - spans.get(q.get("range", "24h"), 86400)
        except ValueError:
            raise web.HTTPBadRequest(text="bad range")
        t_from = max(0, t_from)
        t_to = min(max(t_to, t_from + 3600), now + 86400)
        if t_to - t_from > 3 * 366 * 86400:
            t_from = t_to - 3 * 366 * 86400
        return t_from, t_to

    def node_of(request, field: Optional[str] = None) -> int:
        """Node from ?node=XXXX (or JSON field); default primary. 404 if unknown."""
        raw = request.query.get("node") if field is None else field
        nid = core.resolve_node(raw)
        if nid is None:
            raise web.HTTPNotFound(text=dumps({"ok": False, "error": "Čidlo nenalezeno"}),
                                   content_type="application/json")
        return nid

    def user_of(request) -> tuple[str, str]:
        return request["session"]["username"], request["ip"]

    @need()
    async def api_state(request):
        nid = core.resolve_node(request.query.get("node"))
        return jresp(await core.state(nid))

    @need()
    async def api_history(request):
        nid = node_of(request)
        t_from, t_to = time_range(request)
        return jresp(await asyncio.to_thread(core.history, nid, t_from, t_to))

    @need()
    async def api_flows(request):
        nid = node_of(request)
        t_from, t_to = time_range(request)
        return jresp(await asyncio.to_thread(core.daily_flows, nid, t_from, t_to))

    @need()
    async def api_weather(request):
        s = settings.all()
        d = await core.weather.forecast(s["latitude"], s["longitude"])
        return jresp(d or {"time": [], "precipitation_sum": [], "temperature_2m_max": []})

    @need()
    async def api_export(request):
        nid = node_of(request)
        node = core.nodes.get(nid)
        t_from, t_to = time_range(request)
        resp = web.StreamResponse(headers={
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="retencka_{nid:04X}_{datetime.now():%Y%m%d}.csv"'})
        await resp.prepare(request)
        trim = float(node["battery_trim"])
        await resp.write("\ufeffcas;hladina_cm;hladina_surova_cm;objem_l;napeti_sondy_mV;teplota_C;vlhkost_pct;"
                         "baterie_V;rssi_dBm;dopousteni;priznaky\n".encode())
        last_id = 0
        while True:
            rows = await asyncio.to_thread(lambda: [tuple(r) for r in db.query(
                "SELECT id, ts, level_mm, level_raw_mm, volume_l, probe_100uv, temp_x100, hum_x100, vbat_mv, "
                "rssi_x2, refilling, flags FROM measurements WHERE node_id = ? AND ts >= ? AND ts < ? AND id > ? "
                "ORDER BY id LIMIT 2000", (nid, t_from, t_to, last_id))])
            if not rows:
                break
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            for (rid, ts, lmm, lraw, vol, pr, te, hu, vb, rs, rf, fl) in rows:
                last_id = rid
                w.writerow([
                    datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                    "" if lmm is None else f"{lmm / 10:.1f}".replace(".", ","),
                    "" if lraw is None else f"{lraw / 10:.1f}".replace(".", ","),
                    "" if vol is None else vol,
                    "" if pr is None else f"{pr / 10:.1f}".replace(".", ","),
                    "" if te is None else f"{te / 100:.2f}".replace(".", ","),
                    "" if hu is None else f"{hu / 100:.1f}".replace(".", ","),
                    "" if vb is None else f"{vb * trim / 1000:.3f}".replace(".", ","),
                    "" if rs is None else f"{rs / 2:.1f}".replace(".", ","),
                    rf, fl])
            await resp.write(buf.getvalue().encode())
        await resp.write_eof()
        return resp

    # -------------------------------------------------------------- settings
    @need("admin")
    async def api_settings_get(request):
        return jresp({"settings": settings.public(), "flags": model.FLAG_NAMES})

    @need("admin")
    async def api_settings_post(request):
        data = await read_json(request)
        try:
            changed = settings.update(data)
        except SettingsError as e:
            return jerr(str(e))
        if changed:
            core.invalidate()
            shown = [k if k != "hydrawise_api_key" else "hydrawise_api_key (skryto)" for k in changed]
            core.audit("SETTINGS_CHANGED", "Změněno: " + ", ".join(shown), *user_of(request))
        return jresp({"ok": True, "changed": changed, "settings": settings.public()})

    # ---------------------------------------------------- nodes (sensors)
    def node_public(n: dict) -> dict:
        out = core.nodes.public(n["node_id"])
        out["deadband_cm"] = round(model.deadband_mm(n) / 10.0, 2)
        out["deadband_l"] = round(model.deadband_l(n))
        return out

    @need("admin")
    async def api_nodes(request):
        now = int(time.time())
        configured = set(core.nodes.ids())
        heard = [{"id": f"{h['node_id']:04X}", "rssi": h["rssi"], "age_s": now - h["ts"], "count": h["count"],
                  "added": h["node_id"] in configured, "kind": h.get("kind")} for h in reversed(core.heard.values())]
        radio = core.radio
        return jresp({
            "nodes": [node_public(n) for n in core.nodes.all()],
            "summaries": core.node_summaries(),
            "heard": heard,
            "pairing": core.pairing_status(),
            "radio": {"ok": bool(radio and radio.hardware_ok), "mode": getattr(radio, "mode", None),
                      "chip_version": getattr(radio, "chip_version", None),
                      "resets": getattr(radio, "resets", 0), "crc_errors": getattr(radio, "crc_errors", 0),
                      "foreign": getattr(radio, "foreign_packets", 0),
                      "auth_failures": getattr(radio, "auth_failures", 0),
                      "replays": getattr(radio, "replays", 0)}})

    def parse_node_id(raw: Any) -> int:
        txt = str(raw or "").strip().upper().replace("0X", "")
        if not txt or len(txt) > 4 or any(c not in "0123456789ABCDEF" for c in txt):
            raise web.HTTPBadRequest(text=dumps({"ok": False, "error": "ID čidla jsou 4 znaky 0–9/A–F (např. 3F2A)."}),
                                     content_type="application/json")
        return int(txt, 16)

    @need("admin")
    async def api_pairing(request):
        return jresp(core.pairing_status())

    @need("admin")
    async def api_pairing_start(request):
        """Accept the next pairing request of this node (new node or a new key for a known one)."""
        data = await read_json(request)
        nid = parse_node_id(data.get("node_id"))
        if core.nodes.get(nid) is None and len(core.nodes.ids()) >= 16:
            return jerr("Maximálně 16 čidel.")
        name = str(data.get("name") or "").strip()[:40]
        core.start_pairing(nid, name, request["session"]["username"])
        core.audit("NODE_PAIRING", f"Čeká se na párování čidla {nid:04X}", *user_of(request))
        return jresp({"ok": True, "pairing": core.pairing_status()})

    @need("admin")
    async def api_pairing_cancel(request):
        data = await read_json(request)
        core.cancel_pairing(parse_node_id(data.get("node_id")))
        return jresp({"ok": True, "pairing": core.pairing_status()})

    @need("admin")
    async def api_nodes_remove(request):
        data = await read_json(request)
        nid = parse_node_id(data.get("node_id"))
        node = core.nodes.get(nid)
        if not node:
            return jerr("Čidlo neexistuje.", 404)
        if nid in core.refills:
            await core.refill_stop(nid, "Čidlo odebráno", *user_of(request))
        core.nodes.remove(nid)
        core.last.pop(nid, None)
        core.filters.pop(nid, None)
        core.invalidate()
        core.audit("NODE_REMOVED", f"Odebráno čidlo {nid:04X} ({node['name']}); naměřená data zůstávají",
                   *user_of(request))
        return jresp({"ok": True})

    @need("admin")
    async def api_node_config(request):
        nid = node_of(request)
        data = await read_json(request)
        try:
            changed = core.nodes.update(nid, data)
        except SettingsError as e:
            return jerr(str(e))
        node = core.nodes.get(nid)
        if changed:
            core.invalidate()
            core.audit("SETTINGS_CHANGED", f"Čidlo {nid:04X}: " + ", ".join(changed), *user_of(request))
        if "flow_rate_lpm" in changed:
            core.nodes.update(nid, {"real_flow_rate_lpm": None}, allow_internal=True)
        recalc = bool(set(changed) & {"cal_points", "sensor_offset_cm", "max_depth_cm", "tank_area_m2",
                                      "probe_accuracy_pct", "probe_range_cm"})
        return jresp({"ok": True, "changed": changed, "recalc_suggested": recalc, "node": node_public(node)})

    @need("admin")
    async def api_cal_point(request):
        """Calibrate from reality: 'the real level of this tank right now is X cm'.

        With two points the new point replaces the one closer in voltage, so the
        curve always consists of the two most recent, well separated readings."""
        nid = node_of(request)
        node = core.nodes.get(nid)
        data = await read_json(request)
        last = core.last.get(nid)
        if not last or last.get("probe_100uv") is None or (core.age_s(nid) or 1e9) > 1800:
            return jerr("Není k dispozici čerstvé měření sondy tohoto čidla.")
        try:
            actual_cm = float(str(data.get("actual_cm")).replace(",", "."))
        except ValueError:
            return jerr("Zadejte skutečnou hladinu v cm.")
        mv = last["probe_100uv"] / 10.0
        column = round(actual_cm - float(node["sensor_offset_cm"]), 1)
        pts = [list(p) for p in node["cal_points"]]
        nearest = min(range(len(pts)), key=lambda i: abs(pts[i][0] - mv))
        if len(pts) <= 2 or abs(pts[nearest][0] - mv) < 20:
            pts[nearest] = [mv, column]
        else:
            pts.append([mv, column])
        try:
            core.nodes.update(nid, {"cal_points": pts})
        except SettingsError as e:
            return jerr(str(e) + " Druhý bod změřte při výrazně jiné hladině.")
        core.invalidate()
        core.audit("CALIBRATION", f"Čidlo {nid:04X}: bod {mv:.1f} mV = {column:.1f} cm nad sondou", *user_of(request))
        return jresp({"ok": True, "node": node_public(core.nodes.get(nid))})

    @need("admin")
    async def api_cal_recompute(request):
        nid = node_of(request)
        n = await asyncio.to_thread(core.recompute_levels, nid)
        core.after_recompute(nid)
        core.audit("CALIBRATION", f"Čidlo {nid:04X}: historie přepočítána ({n} záznamů)", *user_of(request))
        return jresp({"ok": True, "updated": n})

    @need("admin")
    async def api_battery_cal(request):
        nid = node_of(request)
        data = await read_json(request)
        last = core.last.get(nid)
        if not last or not last.get("vbat_mv"):
            return jerr("Chybí měření baterie.")
        try:
            actual = float(str(data.get("actual_v")).replace(",", "."))
        except ValueError:
            return jerr("Zadejte změřené napětí.")
        trim = actual * 1000.0 / last["vbat_mv"]
        try:
            core.nodes.update(nid, {"battery_trim": round(trim, 4)})
        except SettingsError:
            return jerr("Korekce mimo povolený rozsah ±20 % – zkontrolujte měření.")
        core.invalidate()
        core.audit("CALIBRATION", f"Čidlo {nid:04X}: korekce baterie {trim:.4f}", *user_of(request))
        return jresp({"ok": True, "battery_trim": round(trim, 4)})

    # ---------------------------------------------------------------- refill
    @need("admin")
    async def api_refill_start(request):
        data = await read_json(request)
        try:
            target = float(data.get("target_pct"))
        except (TypeError, ValueError):
            return jerr("Zadejte cílovou hladinu v %.")
        if not 1 <= target <= 100:
            return jerr("Cíl musí být 1–100 %.")
        nid = node_of(request)
        ok, msg = await core.refill_start(nid, target, "manual", *user_of(request))
        return jresp({"ok": ok, "message": msg, "refill": core.refill_info(nid)}, 200 if ok else 409)

    @need("admin")
    async def api_refill_stop(request):
        nid = node_of(request)
        ok, msg = await core.refill_stop(nid, "Manuálně zastaveno", *user_of(request))
        return jresp({"ok": ok, "message": msg, "refill": core.refill_info(nid)}, 200 if ok else 502)

    # ----------------------------------------------------------------- users
    @need("admin")
    async def api_users(request):
        rows = db.query("SELECT username, role, created, last_login, locked_until FROM users ORDER BY username")
        return jresp([dict(r) for r in rows])

    @need("admin")
    async def api_users_add(request):
        data = await read_json(request)
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        role = data.get("role", "user")
        if role not in ("user", "admin"):
            return jerr("Neplatná role.")
        problem = auth.username_problem(username) or auth.password_problem(username, password)
        if problem:
            return jerr(problem)
        if db.one("SELECT 1 FROM users WHERE username=?", (username,)):
            return jerr("Uživatel již existuje.")
        h = await asyncio.get_running_loop().run_in_executor(None, auth.hash_password, password)
        now = int(time.time())
        db.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES(?,?,?,?,?)",
                   (username, h, role, now, now))
        core.audit("USER_ADDED", f"Přidán uživatel {username!r} ({role})",
                   request["session"]["username"], request["ip"])
        return jresp({"ok": True})

    def _target_user(data, me):
        username = str(data.get("username", "")).strip()
        row = db.one("SELECT * FROM users WHERE username=?", (username,))
        if not row:
            raise web.HTTPNotFound(text=dumps({"ok": False, "error": "Uživatel nenalezen"}),
                                   content_type="application/json")
        if row["username"].lower() == me.lower():
            raise web.HTTPBadRequest(text=dumps({"ok": False, "error": "Tuto akci nelze provést na vlastním účtu."}),
                                     content_type="application/json")
        return row

    @need("admin")
    async def api_users_role(request):
        data = await read_json(request)
        row = _target_user(data, request["session"]["username"])
        role = data.get("role")
        if role not in ("user", "admin"):
            return jerr("Neplatná role.")
        db.execute("UPDATE users SET role=? WHERE username=?", (role, row["username"]))
        core.audit("USER_ROLE_CHANGED", f"{row['username']!r}: {row['role']} → {role}",
                   request["session"]["username"], request["ip"])
        return jresp({"ok": True})

    @need("admin")
    async def api_users_delete(request):
        data = await read_json(request)
        row = _target_user(data, request["session"]["username"])
        db.execute("DELETE FROM users WHERE username=?", (row["username"],))
        core.audit("USER_DELETED", f"Smazán uživatel {row['username']!r}",
                   request["session"]["username"], request["ip"])
        return jresp({"ok": True})

    @need("admin")
    async def api_users_password(request):
        data = await read_json(request)
        row = _target_user(data, request["session"]["username"])
        password = str(data.get("password", ""))
        problem = auth.password_problem(row["username"], password)
        if problem:
            return jerr(problem)
        h = await asyncio.get_running_loop().run_in_executor(None, auth.hash_password, password)
        db.execute("UPDATE users SET pw_hash=?, pw_changed=?, failed=0, locked_until=0 WHERE username=?",
                   (h, int(time.time()), row["username"]))
        core.sessions.delete_user_sessions(row["username"])
        core.audit("PASSWORD_CHANGED", f"Administrátor nastavil nové heslo uživateli {row['username']!r}",
                   request["session"]["username"], request["ip"])
        return jresp({"ok": True})

    # ----------------------------------------------------------------- audit
    @need("admin")
    async def api_audit(request):
        try:
            page = max(1, int(request.query.get("page", 1)))
        except ValueError:
            page = 1
        action = request.query.get("action", "")
        per = 50
        where, args = ("WHERE action = ?", [action]) if action else ("", [])
        total = db.scalar(f"SELECT COUNT(*) FROM audit {where}", args, 0)
        rows = db.query(f"SELECT ts, username, ip, action, detail FROM audit {where} "
                        "ORDER BY id DESC LIMIT ? OFFSET ?", args + [per, (page - 1) * per])
        return jresp({"logs": [dict(r) for r in rows], "total": total, "page": page,
                      "total_pages": max(1, (total + per - 1) // per)})

    # --------------------------------------------------------- HA & system
    @need("admin")
    async def api_ha(request):
        return jresp({"token": settings.get("ha_token"), "port": request.app["ha_port"],
                      "hostnames": core.hostnames(),
                      "nodes": [{"id": n["id"], "name": n["name"]} for n in core.node_summaries()]})

    @need("admin")
    async def api_ha_rotate(request):
        settings.update({"ha_token": secrets.token_urlsafe(32)}, allow_internal=True)
        core.audit("SETTINGS_CHANGED", "Vygenerován nový token pro Home Assistant",
                   request["session"]["username"], request["ip"])
        return jresp({"ok": True, "token": settings.get("ha_token")})

    @need("admin")
    async def api_system(request):
        info: dict[str, Any] = {"version": __version__, "uptime_s": int(time.time() - core.started),
                                "hostnames": core.hostnames()}
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        info["rss_kb"] = int(line.split()[1])
        except OSError:
            pass
        try:
            st_ = os.statvfs(core.data_dir)
            info["disk_free_mb"] = round(st_.f_bavail * st_.f_frsize / 1048576)
            info["db_mb"] = round(os.path.getsize(db.path) / 1048576, 1)
        except OSError:
            pass
        try:
            info["update"] = json.loads(Path(core.data_dir, "update-status.json").read_text())
        except (OSError, ValueError):
            info["update"] = None
        return jresp(info)

    @need("admin")
    async def api_system_update(request):
        Path(core.data_dir, "update-request").write_text(str(int(time.time())))
        core.audit("UPDATE_REQUESTED", "Ruční kontrola aktualizací", request["session"]["username"], request["ip"])
        return jresp({"ok": True, "message": "Kontrola aktualizací spuštěna."})

    r = app.router
    r.add_get("/", page_index)
    r.add_get("/login", page_login)
    r.add_get("/setup", page_setup)
    r.add_get("/favicon.ico", favicon)
    r.add_get("/static/{path:.+}", static_file)
    r.add_get("/api/auth/mode", api_auth_mode)
    r.add_post("/api/auth/login", api_login)
    r.add_post("/api/auth/logout", api_logout)
    r.add_post("/api/auth/setup", api_setup)
    r.add_get("/api/me", api_me)
    r.add_post("/api/me/password", api_me_password)
    r.add_get("/api/state", api_state)
    r.add_get("/api/history", api_history)
    r.add_get("/api/flows", api_flows)
    r.add_get("/api/weather", api_weather)
    r.add_get("/api/export.csv", api_export)
    r.add_get("/api/settings", api_settings_get)
    r.add_post("/api/settings", api_settings_post)
    r.add_post("/api/node", api_node_config)
    r.add_post("/api/calibration/point", api_cal_point)
    r.add_post("/api/calibration/recompute", api_cal_recompute)
    r.add_post("/api/calibration/battery", api_battery_cal)
    r.add_post("/api/refill/start", api_refill_start)
    r.add_post("/api/refill/stop", api_refill_stop)
    r.add_get("/api/nodes", api_nodes)
    r.add_get("/api/pairing", api_pairing)
    r.add_post("/api/pairing/start", api_pairing_start)
    r.add_post("/api/pairing/cancel", api_pairing_cancel)
    r.add_post("/api/nodes/remove", api_nodes_remove)
    r.add_get("/api/users", api_users)
    r.add_post("/api/users/add", api_users_add)
    r.add_post("/api/users/role", api_users_role)
    r.add_post("/api/users/delete", api_users_delete)
    r.add_post("/api/users/password", api_users_password)
    r.add_get("/api/audit", api_audit)
    r.add_get("/api/ha", api_ha)
    r.add_post("/api/ha/rotate", api_ha_rotate)
    r.add_get("/api/system", api_system)
    r.add_post("/api/system/update", api_system_update)
    return app


# ================================================================== HA app
def build_ha_app(core: Core) -> web.Application:
    """Port 8081 – LAN only, never routed through the tunnel."""
    app = web.Application(client_max_size=4096, middlewares=[security_headers])

    def authorized(request: web.Request) -> bool:
        h = request.headers.get("Authorization", "")
        token = core.settings.get("ha_token") or ""
        return h.startswith("Bearer ") and bool(token) and hmac.compare_digest(h[7:].strip(), token)

    async def state(request):
        if not authorized(request):
            await asyncio.sleep(1.0)            # slow down token guessing
            return jerr("Unauthorized", 401)
        nid = core.resolve_node(request.query.get("node"))
        if nid is None:
            return jerr("Unknown node", 404)
        st = await core.state(nid)
        b = st["battery"]
        return jresp({
            "node_id": st["node"]["id"],
            "name": st["node"]["name"],
            "water_level_cm": st["water_level_cm"],
            "water_level_percent": st["water_level_percent"],
            "volume_liters": st["volume_liters"],
            "max_liters": st["max_liters"],
            "temperature": st["temperature"],
            "humidity": st["humidity"],
            "battery_voltage": b["voltage"],
            "battery_percent": b["percent"],
            "battery_low": b["low"],
            "rssi": st["node"]["rssi"],
            "last_seen_seconds_ago": st["last_seen_seconds_ago"],
            "status": st["status"],
            "is_refilling": st["refill"]["active"],
            "consumption_today": st["consumption_today"],
            "expected_rain_mm": st["expected_rain_mm"],
        })

    async def health(request):
        radio_ok = bool(core.radio and core.radio.alive())
        return jresp({"status": "ok" if radio_ok else "degraded", "version": __version__,
                      "radio": getattr(core.radio, "mode", None)}, 200 if radio_ok else 503)

    async def nodes(request):
        if not authorized(request):
            await asyncio.sleep(1.0)
            return jerr("Unauthorized", 401)
        return jresp([{"node_id": n["id"], "name": n["name"], "status": n["status"]}
                      for n in core.node_summaries()])

    app.router.add_get("/api/v1/water/state", state)
    app.router.add_get("/api/v1/nodes", nodes)
    app.router.add_get("/health", health)
    return app
