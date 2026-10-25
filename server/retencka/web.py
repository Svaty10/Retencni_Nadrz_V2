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
import re
import logging
import mimetypes
import os
import shutil
import secrets
import signal
import time
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import httpserver as web

from . import __version__, auth, model, reset, updates
from .core import BACKUP_KEEP, LOW_DISK_MB, Core
from .settings import INHERITABLE, SettingsError

log = logging.getLogger("retencka.web")

COOKIE_PLAIN = "rt_sid"
MAX_BODY = 64 * 1024

FIELD_LABELS = {
    "name": "název", "tank_shape": "tvar nádrže", "max_depth_cm": "hloubka nádrže", "tank_area_m2": "plocha dna",
    "tank_diameter_cm": "průměr nádrže", "tank_length_cm": "délka nádrže", "tank_width_cm": "šířka nádrže",
    "catchment_area_m2": "plocha střechy", "runoff_coef": "odtokový součinitel",
    "sensor_offset_cm": "výška sondy", "cal_points": "kalibrační body", "probe_accuracy_pct": "přesnost sondy",
    "probe_range_cm": "rozsah sondy", "battery_trim": "korekce baterie", "battery_type": "typ baterie", "refill_provider": "ventil",
    "hydrawise_relay_id": "zóna", "hydrawise_api_key": "Hydrawise klíč", "auto_refill_enabled": "automatické dopouštění",
    "refill_threshold_pct": "dopouštět pod", "auto_refill_target_pct": "dopustit na",
    "max_refill_pct": "nejvýš dopustit na", "flow_rate_lpm": "průtok", "max_refill_minutes": "nejdelší dopouštění",
    "rain_wait_enabled": "čekat na déšť", "rain_wait_hours": "výhled deště",
    "rain_wait_max_below_pct": "nečekat pod prahem o", "rain_wait_max_drop_pct": "přestat čekat po poklesu o",
    "latitude": "poloha", "longitude": "poloha", "location_name": "poloha", "battery_capacity_mah": "kapacita baterie", "battery_capacity_alk_mah": "kapacita alkalické baterie",
    "retention_days": "mazání starých dat", "stale_timeout_s": "limit zpoždění dat",
    "telegram_token": "Telegram bot", "notify_low_pct": "upozornit pod",
}


def labels(keys: list[str]) -> str:
    out: list[str] = []
    for k in keys:
        lbl = FIELD_LABELS.get(k, k)
        if lbl not in out:
            out.append(lbl)
    return ", ".join(out)


AUDIT_CATEGORIES = {
    "login":    ["LOGIN_SUCCESS", "LOGIN_FAILED", "LOGOUT", "SETUP_LOGIN", "SETUP_FAILED", "PASSWORD_CHANGED"],
    "users":    ["USER_ADDED", "USER_DELETED", "USER_ROLE_CHANGED"],
    "settings": ["SETTINGS_CHANGED", "CALIBRATION"],
    "refill":   ["MANUAL_REFILL", "AUTO_REFILL", "REFILL_ERROR", "REFILL_FAULT", "RAIN_WAIT"],
    "sensors":  ["NODE_PAIRED", "NODE_PAIRING", "NODE_REMOVED", "NODE_UNPAIR_REQUESTED", "NODE_UNPAIRED",
                 "NODE_UNPAIR_CANCELLED", "NODE_BOOT", "NODE_PAIRING_CANCELLED"],
    "system":   ["TIME_FIXED", "UPDATE_REQUESTED", "UPDATE_AVAILABLE", "UPDATE_INSTALLED", "UPDATE_ROLLED_BACK",
                 "UPDATE_ERROR", "UPDATE_UP_TO_DATE", "BACKUP_DOWNLOADED", "BACKUP_IMPORTED", "DISK_LOW",
                 "TELEGRAM_LINKED", "TELEGRAM_UNLINKED", "SERVICE_ACCESS", "SERVICE_RESTART", "SERVICE_REBOOT", "SERVICE_POWEROFF",
                 "DATA_DELETED", "FACTORY_RESET", "TLS_CERT_ISSUED"],      # TLS_*: old entries (1.7–1.9)
    "problems": ["LOGIN_FAILED", "SETUP_FAILED", "REFILL_ERROR", "REFILL_FAULT", "DISK_LOW",
                 "UPDATE_ROLLED_BACK", "UPDATE_ERROR"],
}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def jresp(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=dumps)


def jerr(msg: str, status: int = 400, **extra) -> web.Response:
    resp = jresp({"ok": False, "error": msg, **extra}, status)
    if status == 429 and extra.get("retry_after"):
        resp.headers["Retry-After"] = str(extra["retry_after"])
    return resp


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
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(self)"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
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
        if request.get("_stream_started"):
            raise                       # response already half-sent: the server just drops the connection
        resp = jerr("Interní chyba serveru", 500)
    _apply_security_headers(request, resp.headers)
    return resp


def make_auth_middleware(core: Core):
    async def authenticate(request: web.Request, handler):
        token = request.cookies.get(COOKIE_PLAIN)
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
                if not hmac.compare_digest(sent.encode("utf-8"), sess["csrf"].encode("utf-8")):
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
    db, settings = core.db, core.settings

    def no_users() -> bool:
        return db.scalar("SELECT COUNT(*) FROM users", default=0) == 0

    def find_user(name: str):
        """The account for a typed name in any case (auth.name_key); its row carries
        the stored spelling, which sessions and the audit log then use."""
        key = auth.name_key(name)
        if not key:
            return None
        return next((r for r in db.query("SELECT * FROM users") if auth.name_key(r["username"]) == key), None)

    app = web.Application(client_max_size=MAX_BODY,
                          middlewares=[security_headers, make_auth_middleware(core)])
    app.prepare_hooks.append(lambda req, resp: _apply_security_headers(req, resp.headers))
    st = Static(static_dir)
    login_locks = app["login_locks"] = auth.LoginLocks()
    setup_lock = asyncio.Lock()

    def set_session_cookie(request: web.Request, resp: web.Response, token: str) -> None:
        # plain http on the LAN (no Secure flag / __Host- prefix; a session cookie of
        # 1.7–1.9 with that prefix is never sent over http -> one new login)
        resp.set_cookie(COOKIE_PLAIN, token, max_age=auth.SESSION_ABSOLUTE_S,
                        path="/", httponly=True, samesite="Lax")

    def clear_cookies(resp: web.Response) -> None:
        resp.del_cookie(COOKIE_PLAIN, path="/")

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
                            samesite="Strict")
            return resp

        row = find_user(username)
        # locked (username, IP) pair: verify against nothing (same cost) and answer exactly
        # like a wrong password - no oracle for existing / locked accounts
        locked = login_locks.locked(username, ip)
        ok = await asyncio.get_running_loop().run_in_executor(
            None, auth.verify_password, password, row["pw_hash"] if row and not locked else None)
        if not ok or locked:
            core.ip_limiter.fail(ip)
            if not locked:
                login_locks.fail(username, ip)
            core.audit("LOGIN_FAILED", "Neplatné jméno nebo heslo", username or "(prázdné)", ip)
            await asyncio.sleep(0.5 + secrets.randbelow(500) / 1000)
            return jerr("Neplatné jméno nebo heslo.", 401)
        core.ip_limiter.success(ip)
        login_locks.success(username, ip)
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
        username = auth.clean_username(str(data.get("username", "")))
        password = str(data.get("password", ""))
        if username.lower() == "admin":
            return jerr("Zvolte si vlastní jméno – výchozí účet „admin“ bude zrušen.")
        problem = auth.username_problem(username) or auth.password_problem(username, password)
        if problem:
            return jerr(problem)
        if hmac.compare_digest(password.encode(), setup_password().encode()):
            return jerr("Nové heslo nesmí být stejné jako heslo ze štítku.")
        async with setup_lock:
            pw_hash = await asyncio.get_running_loop().run_in_executor(None, auth.hash_password, password)
            if not no_users():                    # another request finished setup meanwhile
                return jerr("Systém už je nastaven.", 409)
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
        row = db.one("SELECT theme, lang FROM users WHERE username=?", (s["username"],))
        theme = (row["theme"] if row and row["theme"] else "auto") or "auto"
        lang = (row["lang"] if row and row["lang"] else "cs") or "cs"
        return jresp({"username": s["username"], "role": s["role"], "csrf": s["csrf"],
                      "version": __version__, "hostnames": core.hostnames(), "theme": theme, "lang": lang})

    @need()
    async def api_me_theme(request):
        """Colour theme of the signed-in user (stored with the account, any device)."""
        data = await read_json(request)
        theme = data.get("theme")
        if theme not in ("auto", "light", "dark"):
            return jerr("Motiv musí být auto, light nebo dark.")
        db.execute("UPDATE users SET theme=? WHERE username=?", (theme, request["session"]["username"]))
        return jresp({"ok": True, "theme": theme})

    @need()
    async def api_me_lang(request):
        """UI language of the signed-in user ('cs' or 'en', stored with the account)."""
        data = await read_json(request)
        lang = data.get("lang")
        if lang not in ("cs", "en"):
            return jerr("Jazyk musí být cs nebo en.")
        db.execute("UPDATE users SET lang=? WHERE username=?", (lang, request["session"]["username"]))
        return jresp({"ok": True, "lang": lang})

    @need()
    async def api_me_password(request):
        s = request["session"]
        wait = core.ip_limiter.retry_after(request["ip"])
        if wait:
            return jerr(f"Příliš mnoho pokusů. Zkuste to za {wait} s.", 429, retry_after=wait)
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
        except (ValueError, OverflowError):
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
        state = await core.state(nid)
        if isinstance(state, dict):
            free = core.disk_free_mb()
            state = {**state, "disk_low": core.disk_low(), "disk_free_mb": None if free is None else round(free)}
        return jresp(state)

    @need()
    async def api_history(request):
        nid = node_of(request)
        t_from, t_to = time_range(request)
        out = await asyncio.to_thread(core.history, nid, t_from, t_to)
        return jresp(out)

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

    @need("admin")
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
            core.audit("SETTINGS_CHANGED", "Obecné: " + labels(changed), *user_of(request))
        return jresp({"ok": True, "changed": changed, "settings": settings.public()})

    # ---------------------------------------------------- nodes (sensors)
    def node_public(n: dict) -> dict:
        out = core.nodes.public(n["node_id"])
        out["deadband_cm"] = round(model.deadband_mm(n) / 10.0, 2)
        out["deadband_l"] = round(model.deadband_l(n))
        out["effective_max_depth_cm"] = model.tank_height_cm(n)
        return out

    @need("admin")
    async def api_nodes(request):
        now = int(time.time())
        configured = set(core.nodes.ids())
        heard = []
        for h in reversed(list(core.heard.values())):
            arch = core.archive_info(h["node_id"])
            heard.append({"id": f"{h['node_id']:04X}", "rssi": h["rssi"], "age_s": now - h["ts"], "count": h["count"],
                          "added": h["node_id"] in configured, "kind": h.get("kind"),
                          # cizí čidlo: the hub still holds an old key of it -> can be re-paired
                          "archived": arch["archived"], "key_match": bool(h.get("key_match")),
                          "archived_name": arch["archived_name"]})
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
                      "replays": getattr(radio, "replays", 0),
                      "duplicates": getattr(radio, "duplicates", 0)}})

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
        if core.nodes.get(nid) is None and not name:
            # a new sensor needs a name; a re-paired removed one keeps its old one
            name = core.archive_info(nid)["archived_name"]
            if not name:
                return jerr("Zadejte název čidla.")
        core.start_pairing(nid, name, request["session"]["username"])
        core.audit("NODE_PAIRING", f"Čeká se na párování čidla {nid:04X}", *user_of(request))
        return jresp({"ok": True, "pairing": core.pairing_status()})

    @need("admin")
    async def api_pairing_cancel(request):
        data = await read_json(request)
        nid = parse_node_id(data.get("node_id"))
        waiting = nid in core.pair_targets
        core.cancel_pairing(nid)
        if waiting:
            core.audit("NODE_PAIRING_CANCELLED", f"Zrušeno čekání na párování čidla {nid:04X}", *user_of(request))
        return jresp({"ok": True, "pairing": core.pairing_status()})

    @need("admin")
    async def api_nodes_remove(request):
        """Unpair a sensor. Default: two-step (the sensor must confirm after a battery
        re-insertion). force=true removes the sensor at once – for a lost or broken
        sensor; its key is archived, so if it turns up, pairing its ID again releases
        it with a signed UNPAIR (no UPDI erase needed)."""
        data = await read_json(request)
        nid = parse_node_id(data.get("node_id"))
        node = core.nodes.get(nid)
        if not node:
            return jerr("Čidlo neexistuje.", 404)
        if nid in core.refills:
            await core.refill_stop(nid, "Čidlo odpárováno", *user_of(request))
        if data.get("force") or not node.get("_secret"):
            core.forget_node(nid, "NODE_REMOVED", f"Odebráno čidlo {nid:04X} ({node['name']}) bez potvrzení čidlem",
                             *user_of(request))
            return jresp({"ok": True, "removed": True})
        core.request_unpair(nid, *user_of(request))
        return jresp({"ok": True, "removed": False})

    @need("admin")
    async def api_nodes_unpair_cancel(request):
        data = await read_json(request)
        nid = parse_node_id(data.get("node_id"))
        node = core.nodes.get(nid)
        if not node or not node.get("_unpair"):
            return jerr("Čidlo nečeká na odpárování.", 404)
        core.cancel_unpair(nid, *user_of(request))
        return jresp({"ok": True})

    @need("admin")
    async def api_node_config(request):
        nid = node_of(request)
        data = await read_json(request)
        reset = data.pop("reset", None)
        if reset is not None and (not isinstance(reset, list) or not all(isinstance(k, str) for k in reset)):
            return jerr("reset: seznam klíčů")
        try:
            changed = core.nodes.update(nid, data, reset=reset)
        except SettingsError as e:
            return jerr(str(e))
        node = core.nodes.get(nid)
        if changed:
            core.invalidate()
            core.audit("SETTINGS_CHANGED", f"Čidlo {nid:04X}: " + labels(changed), *user_of(request))
        if "flow_rate_lpm" in changed:
            core.nodes.update(nid, {"real_flow_rate_lpm": None}, allow_internal=True)
        recalc = bool(set(changed) & {"cal_points", "sensor_offset_cm", "max_depth_cm", "tank_area_m2",
                                      "tank_shape", "tank_diameter_cm", "tank_length_cm", "tank_width_cm",
                                      "probe_accuracy_pct", "probe_range_cm"})
        return jresp({"ok": True, "changed": changed, "recalc_suggested": recalc, "node": node_public(node)})

    # ------------------------------------------- place search, Hydrawise helpers
    @need("admin")
    async def api_geocode(request):
        q = " ".join(str(request.query.get("q") or "").split())
        if not 2 <= len(q) <= 80 or any(ord(ch) < 32 for ch in q):
            return jerr("Zadejte název obce nebo adresu (2–80 znaků).")
        res, err = await core.geocoder.search(q)
        if res is None:
            return jerr(err, 502)
        return jresp({"results": res})

    def hydrawise_key(nid: Optional[int] = None) -> str:
        node = core.nodes.get(nid) if nid is not None else None
        return str((node or {}).get("hydrawise_api_key") or settings.get("hydrawise_api_key") or "").strip()

    @need("admin")
    async def api_hydrawise_test(request):
        """Test the account key (or a key typed but not saved yet); the key is never returned."""
        data = await read_json(request)
        typed = str(data.get("api_key") or "").strip()
        key = typed if typed and not typed.startswith("•") else hydrawise_key()
        if not key:
            return jerr("Nejdřív zadejte API klíč Hydrawise.")
        if len(key) > 128 or any(ord(ch) < 33 for ch in key):
            return jerr("Neplatný API klíč.")
        ok, msg, names = await core.hydrawise_account.controller(key)
        if not ok:
            return jerr(msg, 502)
        return jresp({"ok": True, "message": msg, "controllers": names})

    @need("admin")
    async def api_hydrawise_zones(request):
        nid = node_of(request)
        if nid is None:
            return jerr("Neznámé čidlo.", 404)
        key = hydrawise_key(nid)
        if not key:
            return jerr("Chybí API klíč Hydrawise – zadejte ho v Nastavení → Obecné → Hunter Hydrawise.")
        zones, err = await core.hydrawise_account.zones(key)
        if zones is None:
            return jerr(err, 502)
        return jresp({"zones": zones})

    @need("admin")
    async def api_node_defaults_get(request):
        return jresp({"defaults": core.nodes.defaults(), "keys": list(INHERITABLE)})

    @need("admin")
    async def api_node_defaults_post(request):
        """Shared defaults of all tanks; tanks with their own value (override) keep it."""
        data = await read_json(request)
        try:
            changed = core.nodes.set_defaults(data)
        except SettingsError as e:
            return jerr(str(e))
        if changed:
            core.invalidate()
            core.audit("SETTINGS_CHANGED", "Výchozí nastavení nádrží: " + labels(changed), *user_of(request))
        return jresp({"ok": True, "changed": changed, "defaults": core.nodes.defaults(),
                      "nodes": [node_public(n) for n in core.nodes.all()]})

    @need("admin")
    async def api_cal_point(request):
        """Calibrate from reality: 'the real level of this tank right now is X cm'.

        With two points the new point replaces the one closer in voltage, so the
        curve always consists of the two most recent, well separated readings."""
        nid = node_of(request)
        node = core.nodes.get(nid)
        data = await read_json(request)
        last = core.last.get(nid)
        if not last or last.get("probe_100uv") is None or core.data_age(nid) > 1800:
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
        ok, msg = await core.refill_cancel(nid, "Manuálně zastaveno", *user_of(request))
        return jresp({"ok": ok, "message": msg, "refill": core.refill_info(nid)}, 200 if ok else 502)

    @need("admin")
    async def api_refill_resume(request):
        nid = node_of(request)
        ok, msg = core.refill_resume_auto(nid, *user_of(request))
        return jresp({"ok": ok, "message": msg, "refill": core.refill_info(nid)}, 200 if ok else 409)

    # ----------------------------------------------------------------- users
    @need("admin")
    async def api_users(request):
        rows = db.query("SELECT username, role, created, last_login, locked_until FROM users ORDER BY username")
        return jresp([dict(r) for r in rows])

    @need("admin")
    async def api_users_add(request):
        data = await read_json(request)
        username = auth.clean_username(str(data.get("username", "")))
        password = str(data.get("password", ""))
        role = data.get("role", "user")
        if role not in ("user", "admin"):
            return jerr("Neplatná role.")
        problem = auth.username_problem(username) or auth.password_problem(username, password)
        if problem:
            return jerr(problem)
        if find_user(username):
            return jerr("Uživatel již existuje (velikost písmen se nerozlišuje).")
        h = await asyncio.get_running_loop().run_in_executor(None, auth.hash_password, password)
        now = int(time.time())
        db.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES(?,?,?,?,?)",
                   (username, h, role, now, now))
        core.audit("USER_ADDED", f"Přidán uživatel {username!r} ({role})",
                   request["session"]["username"], request["ip"])
        return jresp({"ok": True})

    def _target_user(data, me):
        row = find_user(str(data.get("username", "")))
        if not row:
            raise web.HTTPNotFound(text=dumps({"ok": False, "error": "Uživatel nenalezen"}),
                                   content_type="application/json")
        if auth.name_key(row["username"]) == auth.name_key(me):
            raise web.HTTPBadRequest(text=dumps({"ok": False, "error": "Tuto akci nelze provést na vlastním účtu."}),
                                     content_type="application/json")
        return row

    def last_admin(row) -> bool:
        return row["role"] == "admin" and db.scalar(
            "SELECT COUNT(*) FROM users WHERE role='admin'", default=0) <= 1

    @need("admin")
    async def api_users_role(request):
        data = await read_json(request)
        row = _target_user(data, request["session"]["username"])
        role = data.get("role")
        if role not in ("user", "admin"):
            return jerr("Neplatná role.")
        if role != "admin" and last_admin(row):
            return jerr("Musí zůstat alespoň jeden administrátor.")
        db.execute("UPDATE users SET role=? WHERE username=?", (role, row["username"]))
        core.audit("USER_ROLE_CHANGED", f"{row['username']!r}: {row['role']} → {role}",
                   request["session"]["username"], request["ip"])
        return jresp({"ok": True})

    @need("admin")
    async def api_users_delete(request):
        data = await read_json(request)
        row = _target_user(data, request["session"]["username"])
        if last_admin(row):
            return jerr("Musí zůstat alespoň jeden administrátor.")
        db.execute("DELETE FROM users WHERE username=?", (row["username"],))
        core.sessions.delete_user_sessions(row["username"])
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
        """Audit log with filters: category, who (people/system/<username>), text, date range."""
        q = request.query
        try:
            page = min(max(1, int(q.get("page", 1))), 1_000_000)
        except (ValueError, OverflowError):
            page = 1
        per = 50
        where, args = [], []
        cat = q.get("category", "")
        if cat in AUDIT_CATEGORIES:
            acts = AUDIT_CATEGORIES[cat]
            where.append(f"action IN ({','.join('?' * len(acts))})")
            args += acts
        elif q.get("action"):
            where.append("action = ?")
            args.append(q["action"])
        who = q.get("who", "")
        if who == "people":
            where.append("username NOT LIKE 'system%'")
        elif who == "system":
            where.append("username LIKE 'system%'")
        elif who:
            where.append("username = ? COLLATE NOCASE")
            args.append(who[:64])
        text = q.get("q", "").strip()[:100]
        if text:
            where.append("(detail LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\' OR ip LIKE ? ESCAPE '\\')")
            like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [like, like, like]
        for key, op in (("from", ">="), ("to", "<")):
            try:
                v = int(q.get(key, ""))
                if not -2**62 < v < 2**62:
                    continue
                where.append(f"ts {op} ?")
                args.append(v)
            except (ValueError, OverflowError):
                pass
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = db.scalar(f"SELECT COUNT(*) FROM audit {w}", args, 0)
        rows = db.query(f"SELECT ts, username, ip, action, detail FROM audit {w} "
                        "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", args + [per, (page - 1) * per])
        users = [r[0] for r in db.query("SELECT DISTINCT username FROM audit WHERE username NOT LIKE 'system%' "
                                        "AND username != '' ORDER BY username COLLATE NOCASE LIMIT 50")]
        return jresp({"logs": [dict(r) for r in rows], "total": total, "page": page,
                      "total_pages": max(1, (total + per - 1) // per), "users": users})

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

    _cpu_prev: list[Optional[tuple[float, float]]] = [None]

    def _read_cpu_raw() -> Optional[tuple[float, float]]:
        try:
            with open("/proc/stat") as f:
                line = f.readline()
            if line.startswith("cpu "):
                parts = [float(x) for x in line.split()[1:]]
                if len(parts) >= 4:
                    idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
                    total = sum(parts)
                    return idle, total
        except Exception:
            pass
        try:
            import ctypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]
            idle, kern, user = FILETIME(), FILETIME(), FILETIME()
            if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
                i = (idle.dwHighDateTime << 32) | idle.dwLowDateTime
                k = (kern.dwHighDateTime << 32) | kern.dwLowDateTime
                u = (user.dwHighDateTime << 32) | user.dwLowDateTime
                return float(i), float(k + u)
        except Exception:
            pass
        return None

    async def get_cpu_pct() -> Optional[float]:
        s = _read_cpu_raw()
        if s is None:
            try:
                load = os.getloadavg()[0]
                cores = os.cpu_count() or 1
                return round(max(0.0, min(100.0, (load / cores) * 100.0)), 1)
            except Exception:
                return None
        prev = _cpu_prev[0]
        if prev is None or (s[1] - prev[1]) <= 0:
            await asyncio.sleep(0.05)
            s2 = _read_cpu_raw()
            if s2 is None:
                return None
            idle_delta = s2[0] - s[0]
            total_delta = s2[1] - s[1]
            _cpu_prev[0] = s2
        else:
            idle_delta = s[0] - prev[0]
            total_delta = s[1] - prev[1]
            _cpu_prev[0] = s
        if total_delta <= 0:
            try:
                load = os.getloadavg()[0]
                cores = os.cpu_count() or 1
                return round(max(0.0, min(100.0, (load / cores) * 100.0)), 1)
            except Exception:
                return 0.0
        busy_delta = max(0.0, total_delta - idle_delta)
        return round(max(0.0, min(100.0, (busy_delta / total_delta) * 100.0)), 1)

    def get_ram_info() -> tuple[Optional[int], Optional[int], Optional[float]]:
        try:
            mem: dict[str, int] = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        k = parts[0].strip()
                        if k in ("MemTotal", "MemAvailable"):
                            mem[k] = int(parts[1].split()[0])
            if "MemTotal" in mem and "MemAvailable" in mem:
                tot_kb = mem["MemTotal"]
                avail_kb = mem["MemAvailable"]
                used_kb = max(0, tot_kb - avail_kb)
                return int(round(used_kb / 1024)), int(round(tot_kb / 1024)), round((used_kb / tot_kb) * 100.0, 1)
        except Exception:
            pass
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sllAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                tot_b = stat.ullTotalPhys
                avail_b = stat.ullAvailPhys
                used_b = max(0, tot_b - avail_b)
                return int(round(used_b / (1024 * 1024))), int(round(tot_b / (1024 * 1024))), round((used_b / tot_b) * 100.0, 1)
        except Exception:
            pass
        return None, None, None

    @need("admin")
    async def api_system(request):
        info: dict[str, Any] = {"version": __version__, "uptime_s": int(time.time() - core.started),
                                "hostnames": core.hostnames()}
        cpu = await get_cpu_pct()
        if cpu is not None:
            info["cpu_pct"] = cpu
        ram_used, ram_total, ram_pct = get_ram_info()
        if ram_used is not None:
            info["ram_used_mb"] = ram_used
            info["ram_total_mb"] = ram_total
            info["ram_pct"] = ram_pct
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        info["rss_kb"] = int(line.split()[1])
        except OSError:
            pass
        try:
            info["disk_free_mb"] = round(shutil.disk_usage(core.data_dir).free / 1048576)
            info["db_mb"] = round(os.path.getsize(db.path) / 1048576, 1)
        except OSError:
            pass
        info["disk_low"] = core.disk_low()
        info["low_disk_mb"] = LOW_DISK_MB
        info["update"] = updates.read_status(core.data_dir)
        info["update_prefs"] = updates.read_prefs(core.data_dir)
        return jresp(info)

    @need("admin")
    async def api_backup_list(request):
        return jresp({"backups": await asyncio.to_thread(core.backups), "keep": BACKUP_KEEP,
                      "dir": core.backup_dir()})

    @need("admin")
    async def api_backup_download(request):
        """Fresh consistent snapshot, gzip-compressed while streaming (low RAM).
        Contains sensor keys, password hashes and API keys – keep it private."""
        tmp = os.path.join(core.backup_dir(), f"download-{secrets.token_hex(4)}.tmp")
        await asyncio.to_thread(core.snapshot, tmp)
        host = (core.hostnames() or ["retencka"])[0].split(".")[0]
        resp = web.StreamResponse(headers={
            "Content-Type": "application/gzip",
            "Content-Disposition": f'attachment; filename="{host}-zaloha-{datetime.now():%Y%m%d-%H%M}.db.gz"',
            "Cache-Control": "no-store"})
        try:
            await resp.prepare(request)
            comp = zlib.compressobj(6, zlib.DEFLATED, 31)          # 31 = gzip container
            with open(tmp, "rb") as f:
                while True:
                    chunk = await asyncio.to_thread(f.read, 65536)
                    if not chunk:
                        break
                    data = comp.compress(chunk)
                    if data:
                        await resp.write(data)
            await resp.write(comp.flush())
            await resp.write_eof()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        core.audit("BACKUP_DOWNLOADED", "Stažena záloha databáze", *user_of(request))
        return resp

    @need("admin")
    async def api_backup_import(request):
        """Body = the backup file itself (streamed to disk, never held in RAM)."""
        tmp = os.path.join(core.backup_dir(), f"upload-{secrets.token_hex(4)}.tmp")
        try:
            size = 0
            with open(tmp, "wb") as f:
                async for chunk in request.iter_body():
                    size += len(chunk)
                    await asyncio.to_thread(f.write, chunk)
            if size < 100:
                return jerr("Prázdný soubor.")
            full = request.query.get("full") == "1"
            try:
                stats = await asyncio.to_thread(core.import_backup, tmp, full)
            except ValueError as e:
                return jerr(str(e))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        core.audit("BACKUP_IMPORTED",
                   f"Import zálohy: {stats['measurements']} nových měření, {stats['skipped']} duplicit přeskočeno, "
                   f"{stats['nodes']} čidel" + (", nastavení a uživatelé" if stats["settings"] else ""),
                   *user_of(request))
        return jresp({"ok": True, **stats})

    # ------------------------------------------------------------- telegram
    def tg_status() -> dict:
        bot = core.telegram
        return bot.status() if bot else {"configured": False, "connected": False, "bot": None, "error": None}

    @need("admin")
    async def api_telegram(request):
        bot = core.telegram
        return jresp({**tg_status(), "chats": bot.chats() if bot else []})

    @need()
    async def api_telegram_mine(request):
        bot = core.telegram
        return jresp({**tg_status(), "chats": bot.chats(request["session"]["username"]) if bot else []})

    @need()
    async def api_telegram_link(request):
        bot = core.telegram
        if not bot or not tg_status()["connected"]:
            return jerr("Telegram bot není nastavený nebo připojený – nastaví ho administrátor.")
        return jresp({"ok": True, **bot.new_code(request["session"]["username"])})

    @need()
    async def api_telegram_unlink(request):
        data = await read_json(request)
        bot = core.telegram
        try:
            cid = int(data.get("chat_id"))
        except (TypeError, ValueError):
            return jerr("Neplatný chat.")
        sess = request["session"]
        own = bot is not None and any(c["chat_id"] == cid for c in bot.chats(sess["username"]))
        if not own and sess["role"] != "admin":
            return jerr("Nedostatečná oprávnění", 403)
        if bot:
            bot.unlink(cid)
        core.audit("TELEGRAM_UNLINKED", "Telegram chat odpojen", *user_of(request))
        return jresp({"ok": True})

    # ----------------------------------------------- remote service access
    @need("admin")
    async def api_servis(request):
        return jresp(core.servis_state())

    @need("admin")
    async def api_servis_install(request):
        data = await read_json(request)
        try:
            st = core.netbird_install(data.get("setup_key"), data.get("management_url") or "", *user_of(request))
        except ValueError as e:
            return jerr(str(e))
        except OSError:
            return jerr("Žádost se nepodařilo uložit.", 500)
        return jresp({"ok": True, "install": st})

    @need("admin")
    async def api_servis_set(request):
        data = await read_json(request)
        mode = str(data.get("mode") or "")
        if mode not in ("off", "24h", "on"):
            return jerr("Neplatná volba.")
        return jresp({"ok": True, **core.set_servis(mode, *user_of(request))})

    # ------------------------------------------------------------ updates
    # The root updater (system/bin/retencka-update.py) is driven by files in the data
    # directory, see updates.py. A check never installs; install is a separate request.
    def update_request(request, action: str, version: Optional[str] = None):
        st = updates.read_status(core.data_dir)
        if updates.busy(st) and (action == "install" or st.get("state") != "checking"):
            return jerr("Aktualizace právě probíhá.", 409)
        try:
            updates.request(core.data_dir, action, version)
        except ValueError as e:
            return jerr(str(e))
        except OSError:
            return jerr("Žádost se nepodařilo uložit.", 500)
        if action == "install":
            core.audit("UPDATE_REQUESTED", f"Instalace verze {version}", *user_of(request))
        else:
            core.audit("UPDATE_REQUESTED", "Ruční kontrola aktualizací", *user_of(request))
        return jresp({"ok": True, **updates.summary(core.data_dir, __version__),
                      "message": "Kontrola aktualizací spuštěna." if action == "check" else
                                 f"Instalace verze {version} spuštěna."})

    # ---------------------------------------------------------- power controls
    def restart_app() -> None:
        """systemd (Restart=always) starts the app again after SIGTERM."""
        os.kill(os.getpid(), signal.SIGTERM)

    @need("admin")
    async def api_system_restart(request):
        core.audit("SERVICE_RESTART", "Restart aplikace z webu", *user_of(request))
        asyncio.get_running_loop().call_later(1.0, request.app.get("restart", restart_app))     # let the response go out first
        return jresp({"ok": True, "message": "Aplikace se restartuje, za chvíli bude zpět."})

    @need("admin")
    async def api_system_reboot(request):
        try:
            core.control_request("reboot")
        except OSError:
            return jerr("Žádost se nepodařilo uložit.", 500)
        core.audit("SERVICE_REBOOT", "Restart zařízení z webu", *user_of(request))
        return jresp({"ok": True, "message": "Zařízení se restartuje (asi 1 minuta)."})

    @need("admin")
    async def api_system_poweroff(request):
        try:
            core.control_request("poweroff")
        except OSError:
            return jerr("Žádost se nepodařilo uložit.", 500)
        core.audit("SERVICE_POWEROFF", "Vypnutí zařízení z webu", *user_of(request))
        return jresp({"ok": True, "message": "Zařízení se vypíná. Znovu ho zapnete odpojením a připojením napájení."})

    @need("admin")
    async def api_factory_reset(request):
        """Wipe everything back to the first-install state (see reset.py); needs the
        administrator's password and the typed word OBNOVIT."""
        data = await read_json(request)
        if data.get("confirm") != "OBNOVIT":
            return jerr("Pro potvrzení napište OBNOVIT.")
        me = request["session"]["username"]
        row = db.one("SELECT pw_hash FROM users WHERE username=?", (me,))
        ok = row is not None and await asyncio.get_running_loop().run_in_executor(
            None, auth.verify_password, str(data.get("password", "")), row["pw_hash"])
        if not ok:
            core.audit("FACTORY_RESET", "Obnova továrního nastavení odmítnuta – nesprávné heslo", *user_of(request))
            return jerr("Nesprávné heslo.", 403)
        try:
            reset.request(core.data_dir, me)
        except OSError:
            return jerr("Žádost se nepodařilo uložit.", 500)
        core.audit("FACTORY_RESET", "Zahájena obnova továrního nastavení – data, nastavení, uživatelé "
                                    "a klíče čidel budou smazány", *user_of(request))
        asyncio.get_running_loop().call_later(1.5, request.app.get("restart", restart_app))
        return jresp({"ok": True, "message": "Obnovuji tovární nastavení, zařízení bude za chvíli zpět. "
                                             "Pak se přihlaste jménem admin a heslem ze štítku."})

    # ------------------------------------------------------- deleting data
    @need("admin")
    async def api_data_delete(request):
        """{node: "XXXX"|null, from: epoch|null, to: epoch|null, dry_run: bool} – measured data
        (raw, hourly, finished refills) of a tank or all tanks; the audit log stays."""
        data = await read_json(request)
        raw_node = data.get("node")
        nid = None
        if raw_node not in (None, "", "all"):
            nid = core.resolve_node(str(raw_node))
            if nid is None:
                return jerr("Neznámá nádrž.", 404)

        def ts(key):
            v = data.get(key)
            if v in (None, ""):
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < 2 ** 40:
                raise ValueError(key)
            return int(v)
        try:
            t_from, t_to = ts("from"), ts("to")
        except ValueError:
            return jerr("Neplatný rozsah.")
        if t_from is not None and t_to is not None and t_to <= t_from:
            return jerr("Konec rozsahu musí být po začátku.")
        if data.get("dry_run", True) is not False:
            counts = await asyncio.to_thread(core.count_data, nid, t_from, t_to)
            return jresp({"ok": True, "dry_run": True, "counts": counts, "total": sum(counts.values())})
        counts = await asyncio.to_thread(core.delete_data, nid, t_from, t_to)
        core.after_delete(nid)
        fmt = lambda t: datetime.fromtimestamp(t).strftime("%d.%m.%Y %H:%M")   # noqa: E731
        rng = ("vše" if t_from is None and t_to is None else
               f"{fmt(t_from) if t_from is not None else 'začátek'} – {fmt(t_to) if t_to is not None else 'teď'}")
        who = f"nádrž {(core.nodes.get(nid) or {}).get('name', '')} ({nid:04X})" if nid is not None else "všechna čidla"
        core.audit("DATA_DELETED", f"Smazána naměřená data: {who}, {rng}; {counts['measurements']} měření, "
                                   f"{counts['hourly']} hodinových souhrnů, {counts['refills']} dopouštění",
                   *user_of(request))
        return jresp({"ok": True, "dry_run": False, "counts": counts, "total": sum(counts.values())})

    @need("admin")
    async def api_system_update(request):          # old route = check (kept for older clients)
        return update_request(request, "check")

    @need("admin")
    async def api_update_check(request):
        return update_request(request, "check")

    @need("admin")
    async def api_update_install(request):
        data = await read_json(request)
        ver = data.get("version")
        if not updates.valid_version(ver):
            return jerr("Neplatná verze.")
        st = updates.read_status(core.data_dir) or {}
        if st.get("state") not in ("available", "error", "rolled_back") or st.get("latest") != ver:
            return jerr("Tato verze není k dispozici – nejprve zkontrolujte aktualizace.", 409)
        return update_request(request, "install", ver)

    @need("admin")
    async def api_update_status(request):
        return jresp(updates.summary(core.data_dir, __version__))

    @need("admin")
    async def api_update_prefs(request):
        data = await read_json(request)
        try:
            p = updates.write_prefs(core.data_dir, data.get("auto"), data.get("channel"))
        except ValueError as e:
            return jerr(str(e))
        except OSError:
            return jerr("Nastavení se nepodařilo uložit.", 500)
        core.audit("SETTINGS_CHANGED", "Aktualizace: " + ("automaticky" if p["auto"] else "jen ručně")
                   + (", kanál beta" if p["channel"] == "beta" else ", kanál stabilní"), *user_of(request))
        return jresp({"ok": True, "prefs": p})

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
    r.add_post("/api/me/theme", api_me_theme)
    r.add_post("/api/me/lang", api_me_lang)
    r.add_post("/api/me/password", api_me_password)
    r.add_get("/api/state", api_state)
    r.add_get("/api/history", api_history)
    r.add_get("/api/flows", api_flows)
    r.add_get("/api/weather", api_weather)
    r.add_get("/api/export.csv", api_export)
    r.add_get("/api/settings", api_settings_get)
    r.add_post("/api/settings", api_settings_post)
    r.add_post("/api/node", api_node_config)
    r.add_get("/api/node-defaults", api_node_defaults_get)
    r.add_get("/api/geocode", api_geocode)
    r.add_post("/api/hydrawise/test", api_hydrawise_test)
    r.add_get("/api/hydrawise/zones", api_hydrawise_zones)
    r.add_post("/api/node-defaults", api_node_defaults_post)
    r.add_post("/api/calibration/point", api_cal_point)
    r.add_post("/api/calibration/recompute", api_cal_recompute)
    r.add_post("/api/calibration/battery", api_battery_cal)
    r.add_post("/api/refill/start", api_refill_start)
    r.add_post("/api/refill/stop", api_refill_stop)
    r.add_post("/api/refill/resume", api_refill_resume)
    r.add_get("/api/nodes", api_nodes)
    r.add_get("/api/pairing", api_pairing)
    r.add_post("/api/pairing/start", api_pairing_start)
    r.add_post("/api/pairing/cancel", api_pairing_cancel)
    r.add_post("/api/nodes/remove", api_nodes_remove)
    r.add_post("/api/nodes/unpair/cancel", api_nodes_unpair_cancel)
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
    r.add_post("/api/system/restart", api_system_restart)
    r.add_post("/api/system/reboot", api_system_reboot)
    r.add_post("/api/system/poweroff", api_system_poweroff)
    r.add_post("/api/system/factory-reset", api_factory_reset)
    r.add_post("/api/data/delete", api_data_delete)
    r.add_post("/api/update/check", api_update_check)
    r.add_post("/api/update/install", api_update_install)
    r.add_get("/api/update/status", api_update_status)
    r.add_post("/api/update/prefs", api_update_prefs)
    r.add_get("/api/telegram", api_telegram)
    r.add_get("/api/telegram/mine", api_telegram_mine)
    r.add_post("/api/telegram/link", api_telegram_link)
    r.add_post("/api/telegram/unlink", api_telegram_unlink)
    r.add_get("/api/servis", api_servis)
    r.add_post("/api/servis", api_servis_set)
    r.add_post("/api/servis/install", api_servis_install)
    r.add_get("/api/backup", api_backup_list)
    r.add_get("/api/backup/download", api_backup_download)
    r.add_post("/api/backup/import", api_backup_import)
    app.stream_paths["/api/backup/import"] = 1024 * 1024 * 1024
    return app


# ================================================================== HA app
def build_ha_app(core: Core) -> web.Application:
    """Port 8081 – LAN only, never routed through the tunnel."""
    app = web.Application(client_max_size=4096, middlewares=[security_headers])

    def authorized(request: web.Request) -> bool:
        h = request.headers.get("Authorization", "")
        token = core.settings.get("ha_token") or ""
        return h.startswith("Bearer ") and bool(token) and hmac.compare_digest(h[7:].strip().encode("utf-8"),
                                                                              token.encode("utf-8"))

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
