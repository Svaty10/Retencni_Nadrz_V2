"""Telegram bot: notifications, status, level chart and remote control from a phone.

Each installation has its own bot (token from @BotFather in the settings). The hub
only polls api.telegram.org (long polling) – no public IP, no open port.

Security:
  * a chat is linked to a hub user by a one-time code (10 min) created in the web UI;
    the chat then has that user's role (user = read, admin = refill + service);
  * only private chats, a few wrong codes per chat are allowed;
  * deleting the hub user removes its chats (foreign key).
"""

from __future__ import annotations

import asyncio
import html
import http.client
import json
import logging
import math
import os
import secrets
import signal
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Any, Optional

from . import __version__, model, pngchart

log = logging.getLogger("retencka.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_S = 25
CODE_TTL_S = 600
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_BAD_CODES = 5

COMMANDS = [
    ("stav", "Stav všech nádrží"),
    ("graf", "Graf hladiny za 7 dní"),
    ("dopustit", "Spustit dopouštění (např. dopustit 60)"),
    ("stop", "Zastavit dopouštění"),
    ("upozorneni", "Upozornění zap / vyp"),
    ("diag", "Servisní diagnostika"),
    ("pomoc", "Nápověda"),
]


def _plain(s: str) -> str:
    """Lower case without diacritics – commands work with or without háčky."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(ch))


def _n(v: Optional[float], digits: int = 0, unit: str = "") -> str:
    if v is None:
        return "–"
    s = f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")
    return s + unit


def _call(token: str, method: str, payload: Optional[dict] = None, files: Optional[dict] = None,
          timeout: float = 20.0) -> dict:
    """Blocking Bot API call (worker thread). Never raises for API errors."""
    url = API.format(token=token, method=method)
    if files:
        boundary = "----retencka" + secrets.token_hex(8)
        body = bytearray()
        for k, v in (payload or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                     f"{v if isinstance(v, str) else json.dumps(v)}\r\n").encode()
        for k, (fname, data, ctype) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                     f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=bytes(body),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(payload or {}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(1024 * 1024))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read(64 * 1024))
        except ValueError:
            return {"ok": False, "error_code": e.code, "description": f"HTTP {e.code}"}
    except (OSError, ValueError, http.client.HTTPException) as e:
        return {"ok": False, "error_code": 0, "description": type(e).__name__}


class TelegramBot:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.token: Optional[str] = None
        self.username: Optional[str] = None
        self.ok = False
        self.error: Optional[str] = None
        self._offset = 0
        self._codes: dict[str, tuple[str, float]] = {}
        self._bad: dict[int, list[float]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._thread = threading.Thread(target=self._poll_thread, name="telegram", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        configured = bool((self.core.settings.get("telegram_token") or "").strip())
        return {"configured": configured, "connected": self.ok and configured, "bot": self.username,
                "error": self.error if configured else None}

    # -------------------------------------------------------------- polling
    def _poll_thread(self) -> None:
        """Long polling in its own daemon thread; whatever goes wrong, the thread survives."""
        crash_wait = 5.0
        while not self._stop.is_set():
            try:
                self._poll_loop()
                return
            except Exception:
                log.exception("telegram poll loop crashed - restarting")
                self.ok, self.error = False, "Telegram není dostupný"
                self._stop.wait(crash_wait)
                crash_wait = min(crash_wait * 2, 300.0)

    def _poll_loop(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            token = (self.core.settings.get("telegram_token") or "").strip()
            if not token:
                self.ok, self.token, self.username = False, None, None
                self._stop.wait(10)
                continue
            if token != self.token:
                me = _call(token, "getMe")
                if not me.get("ok"):
                    self.ok = False
                    self.error = ("Neplatný token bota" if me.get("error_code") in (401, 404)
                                  else "Telegram není dostupný")
                    self._stop.wait(60)
                    continue
                self.token, self.username, self._offset = token, me["result"].get("username"), 0
                _call(token, "deleteWebhook")
                _call(token, "setMyCommands", {"commands": [{"command": c, "description": d}
                                                            for c, d in COMMANDS]})
                log.info("Telegram bot @%s connected", self.username)
            res = _call(token, "getUpdates", {"offset": self._offset, "timeout": POLL_TIMEOUT_S,
                                              "allowed_updates": ["message", "callback_query"]},
                        timeout=POLL_TIMEOUT_S + 15)
            if not res.get("ok"):
                self.ok = False
                code = res.get("error_code")
                self.error = ("Neplatný token bota" if code in (401, 404) else
                              "Bota používá jiné zařízení se stejným tokenem" if code == 409 else
                              "Telegram není dostupný")
                if code in (401, 404):
                    self.token = None
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300.0)
                continue
            self.ok, self.error, backoff = True, None, 5.0
            for upd in res.get("result", []):
                self._offset = max(self._offset, upd.get("update_id", 0) + 1)
                if self.loop is None:
                    continue
                fut = asyncio.run_coroutine_threadsafe(self.handle(upd), self.loop)
                try:
                    fut.result(timeout=60)
                except Exception:
                    log.exception("telegram update failed")

    # ----------------------------------------------------------- sending
    async def api(self, method: str, payload: Optional[dict] = None, files: Optional[dict] = None) -> dict:
        if not self.token:
            return {"ok": False}
        return await asyncio.to_thread(_call, self.token, method, payload, files)

    async def send(self, chat_id: int, text: str, buttons: Optional[list] = None) -> None:
        p: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": True}
        if buttons:
            p["reply_markup"] = {"inline_keyboard": buttons}
        await self.api("sendMessage", p)

    def notify(self, text: str, admin_only: bool = False) -> None:
        """Broadcast to linked chats with notifications on (called from the loop thread)."""
        if not self.token or self.loop is None:
            return
        rows = self.db.query("SELECT c.chat_id FROM telegram_chats c JOIN users u ON u.username = c.username "
                             "WHERE c.notify = 1" + (" AND u.role = 'admin'" if admin_only else ""))
        for r in rows:
            asyncio.ensure_future(self.send(r["chat_id"], text))

    # ----------------------------------------------------------- linking
    def new_code(self, username: str) -> dict:
        now = time.time()
        for k in [k for k, v in self._codes.items() if v[1] < now]:
            self._codes.pop(k, None)
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        self._codes[code] = (username, now + CODE_TTL_S)
        url = f"https://t.me/{self.username}?start={code}" if self.username else None
        return {"code": code, "url": url, "expires_in": CODE_TTL_S, "bot": self.username}

    def chats(self, username: Optional[str] = None) -> list[dict]:
        q = ("SELECT chat_id, username, title, created, notify FROM telegram_chats"
             + (" WHERE username = ? COLLATE NOCASE" if username else "") + " ORDER BY created")
        return [dict(r) for r in self.db.query(q, (username,) if username else ())]

    def unlink(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM telegram_chats WHERE chat_id = ?", (chat_id,))

    def _user(self, chat_id: int) -> Optional[dict]:
        r = self.db.one("SELECT c.username, c.notify, u.role FROM telegram_chats c "
                        "JOIN users u ON u.username = c.username WHERE c.chat_id = ?", (chat_id,))
        return dict(r) if r else None

    async def _link(self, chat: dict, code: str) -> None:
        cid = chat["id"]
        now = time.time()
        bad = [t for t in self._bad.get(cid, []) if now - t < CODE_TTL_S]
        if len(bad) >= MAX_BAD_CODES:
            await self.send(cid, "Příliš mnoho pokusů. Zkuste to za 10 minut.")
            return
        hit = self._codes.pop(code.strip().upper(), None)
        if not hit or hit[1] < now:
            self._bad[cid] = bad + [now]
            await self.send(cid, "Kód je neplatný nebo vypršel. Vytvořte nový na webu: Můj účet → Telegram.")
            return
        username = hit[0]
        title = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or ""
        self.db.execute("INSERT OR REPLACE INTO telegram_chats(chat_id, username, title, created, notify) "
                        "VALUES(?,?,?,?,1)", (cid, username, title[:64], int(now)))
        self.core.audit("TELEGRAM_LINKED", f"Telegram propojen s účtem {username} ({title[:40]})", username)
        await self.send(cid, f"✅ Propojeno s účtem <b>{html.escape(username)}</b>.\n\n" + self._help(self._user(cid)))

    # ----------------------------------------------------------- dispatch
    async def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            await self.api("answerCallbackQuery", {"callback_query_id": cq["id"]})
            msg = cq.get("message") or {}
            chat = msg.get("chat") or {}
            if chat.get("type") != "private":
                return
            user = self._user(chat["id"])
            if user is None:
                return
            cmd, _, arg = (cq.get("data") or "").partition(":")
            await self._command(chat["id"], user, cmd, arg.split() if arg else [])
            return
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if not text or "id" not in chat:
            return
        if chat.get("type") != "private":
            await self.send(chat["id"], "Bot odpovídá jen v soukromém chatu.")
            return
        parts = text.split()
        cmd = _plain(parts[0].lstrip("/").split("@")[0])
        args = parts[1:]
        if cmd == "start" and args:
            await self._link(chat, args[0])
            return
        user = self._user(chat["id"])
        if user is None:
            await self.send(chat["id"], "Tento chat zatím není propojený s hubem.\n"
                                        "Na webu hubu otevřete <b>Můj účet → Telegram → Propojit</b>.")
            return
        await self._command(chat["id"], user, cmd, args)

    def _help(self, user: Optional[dict]) -> str:
        admin = bool(user and user["role"] == "admin")
        lines = ["<b>Příkazy</b>", "stav – stav nádrží", "graf – hladina za 7 dní",
                 "upozorneni zap / vyp – upozornění"]
        if admin:
            lines += ["dopustit [nádrž] [%] – spustit dopouštění", "stop [nádrž] – zastavit dopouštění",
                      "", "<b>Servis</b>", "diag – diagnostika", "log – poslední záznamy",
                      "restart – restart aplikace", "reboot – restart zařízení",
                      "aktualizovat – zkontrolovat aktualizace",
                      "servis zap / 24h / vyp – vzdálený servisní přístup"]
        lines += ["", "odhlasit – odpojit tento chat"]
        return "\n".join(lines)

    def _resolve(self, words: list[str]) -> tuple[Optional[int], list[str]]:
        """Pick a node from the words (ID or start of the name); default the only/first node."""
        ids = self.core.nodes.ids()
        if not ids:
            return None, words
        rest = list(words)
        for i, w in enumerate(words):
            for nid in ids:
                n = self.core.nodes.get(nid) or {}
                if _plain(w) in (f"{nid:04x}", ) or _plain(n.get("name", "")).startswith(_plain(w)):
                    rest.pop(i)
                    return nid, rest
        return ids[0], rest

    async def _command(self, cid: int, user: dict, cmd: str, args: list[str]) -> None:
        admin = user["role"] == "admin"
        who = f"{user['username']} (Telegram)"
        if cmd in ("pomoc", "help", "start", "napoveda"):
            await self.send(cid, self._help(user))
        elif cmd == "stav":
            await self._status(cid, admin)
        elif cmd == "graf":
            await self._chart(cid, args)
        elif cmd in ("upozorneni", "notify"):
            on = not args or _plain(args[0]) in ("zap", "ano", "on", "1")
            self.db.execute("UPDATE telegram_chats SET notify=? WHERE chat_id=?", (1 if on else 0, cid))
            await self.send(cid, "🔔 Upozornění zapnuta." if on else "🔕 Upozornění vypnuta.")
        elif cmd in ("odhlasit", "odpojit"):
            self.unlink(cid)
            self.core.audit("TELEGRAM_UNLINKED", "Telegram odpojen (z chatu)", user["username"])
            await self.send(cid, "Chat byl odpojen od hubu.")
        elif not admin:
            await self.send(cid, "Na tento příkaz nemáte oprávnění (jen administrátor).")
        elif cmd == "dopustit":
            nid, rest = self._resolve(args)
            node = self.core.nodes.get(nid) if nid is not None else None
            if node is None:
                await self.send(cid, "Žádná nádrž.")
                return
            target = float(node["auto_refill_target_pct"])
            for w in rest:
                try:
                    v = float(w.rstrip("%").replace(",", "."))
                except ValueError:
                    continue
                if not (math.isfinite(v) and 1 <= v <= 100):
                    await self.send(cid, "⚠️ Cíl musí být 1–100 %.")
                    return
                target = v
            ok, msg = await self.core.refill_start(nid, target, "manual", who)
            await self.send(cid, ("💧 " if ok else "⚠️ ") + f"<b>{html.escape(node['name'])}</b>: {html.escape(msg)}")
        elif cmd == "stop":
            nid, _ = self._resolve(args)
            node = self.core.nodes.get(nid) if nid is not None else None
            if node is None:
                await self.send(cid, "Žádná nádrž.")
                return
            ok, msg = await self.core.refill_stop(nid, "Zastaveno z Telegramu", who)
            await self.send(cid, ("⏹ " if ok else "⚠️ ") + f"<b>{html.escape(node['name'])}</b>: {html.escape(msg)}")
        elif cmd == "diag":
            await self.send(cid, self._diag())
        elif cmd == "log":
            rows = self.db.query("SELECT ts, username, detail FROM audit ORDER BY id DESC LIMIT 12")
            lines = [f"{time.strftime('%d.%m. %H:%M', time.localtime(r['ts']))} · {html.escape(r['detail'] or '')}"
                     for r in rows]
            await self.send(cid, "<b>Poslední záznamy</b>\n" + "\n".join(lines or ["–"]))
        elif cmd == "restart":
            if args and args[0] == "ano":
                self.core.audit("SERVICE_RESTART", "Restart aplikace z Telegramu", who)
                await self.send(cid, "🔄 Aplikace se restartuje, za chvíli bude zpět.")
                self.loop.call_later(1.0, os.kill, os.getpid(), signal.SIGTERM)
            else:
                await self.send(cid, "Restartovat aplikaci hubu?", [[{"text": "Ano, restartovat",
                                                                      "callback_data": "restart:ano"}]])
        elif cmd == "reboot":
            if args and args[0] == "ano":
                self.core.audit("SERVICE_REBOOT", "Restart zařízení z Telegramu", who)
                self.core.control_request("reboot")
                await self.send(cid, "🔄 Zařízení se restartuje (asi 1 minuta).")
            else:
                await self.send(cid, "Restartovat celé zařízení?", [[{"text": "Ano, restartovat zařízení",
                                                                      "callback_data": "reboot:ano"}]])
        elif cmd in ("aktualizovat", "update"):
            self.core.control_request("update")
            self.core.audit("UPDATE_REQUESTED", "Kontrola aktualizací z Telegramu", who)
            await self.send(cid, "⬆️ Kontrola aktualizací spuštěna.")
        elif cmd == "servis":
            mode = {"zap": "24h", "24h": "24h", "trvale": "on", "vyp": "off", "off": "off"}.get(
                _plain(args[0]) if args else "", None)
            if mode is None:
                st = self.core.servis_state()
                await self.send(cid, "<b>Servisní přístup</b>: " + self._servis_text(st)
                                + "\n\nservis zap (24 h) · servis trvale · servis vyp")
                return
            st = self.core.set_servis(mode, who)
            await self.send(cid, "🛠 Servisní přístup: " + self._servis_text(st))
        else:
            await self.send(cid, "Neznámý příkaz.\n\n" + self._help(user))

    # ----------------------------------------------------------- content
    async def _status(self, cid: int, admin: bool) -> None:
        nodes = self.core.node_summaries()
        if not nodes:
            await self.send(cid, "Zatím žádné čidlo.")
            return
        st_txt = {"online": "", "stale": " · ⚠️ zpožděná data", "offline": " · ❌ čidlo nehlásí data"}
        lines, buttons = [], []
        for n in nodes:
            nid = int(n["id"], 16)
            b = n["battery"] or {}
            rf = self.core.refill_info(nid)
            out = self.core.cached(f"tg-out:{nid}", 600, lambda nid=nid: self._outlook(nid))
            lines.append(f"<b>{html.escape(n['name'])}</b>: {_n(n['level_pct'], 0, ' %')} · {_n(n['volume_l'], 0, ' L')}"
                         + st_txt.get(n["status"], ""))
            sub = [f"🔋 {_n(b.get('percent'), 0, ' %')}" + (" (vybitá)" if b.get("low") else "")]
            if out:
                sub.append(out)
            if rf.get("configured"):
                sub.append(("💧 " if rf.get("active") else "") + rf.get("status", ""))
            lines.append("   " + " · ".join(sub))
            row = [{"text": f"📈 {n['name'][:16]}", "callback_data": f"graf:{n['id']}"}]
            if admin and rf.get("configured"):
                row.append({"text": "⏹ Stop" if rf.get("active") else "💧 Dopustit",
                            "callback_data": ("stop:" if rf.get("active") else "dopustit:") + n["id"]})
            buttons.append(row)
        await self.send(cid, "\n".join(lines), buttons)

    def _outlook(self, nid: int) -> str:
        node = self.core.nodes.get(nid)
        lv = self.core.level_now(nid)
        if node is None or lv is None:
            return ""
        now = int(time.time())
        flows = self.core.daily_flows(nid, now - 8 * 86400, now)
        o = self.core.outlook(node, lv, flows)
        if o.get("days_left") is None:
            return ""
        return "vystačí > rok" if o["days_left"] > 365 else f"vystačí ~{_n(o['days_left'], 0)} dní"

    async def _chart(self, cid: int, args: list[str]) -> None:
        ids = self.core.nodes.ids()
        if len(ids) > 1 and not args:
            await self.send(cid, "Kterou nádrž?", [[{"text": (self.core.nodes.get(i) or {}).get("name", f"{i:04X}")[:30],
                                                       "callback_data": f"graf:{i:04X}"}] for i in ids])
            return
        nid, _ = self._resolve(args)
        node = self.core.nodes.get(nid) if nid is not None else None
        if node is None:
            await self.send(cid, "Žádná nádrž.")
            return
        now = int(time.time())
        h = await asyncio.to_thread(self.core.history, nid, now - 7 * 86400, now)
        png = await asyncio.to_thread(pngchart.level_chart, h["t"], h["level_pct"], now - 7 * 86400, now, h["bands"])
        lv = self.core.level_now(nid)
        cap = (f"{node['name']} – hladina (%) za 7 dní"
               + ("" if lv is None else f" · teď {_n(lv.percent, 0, ' %')}, {_n(lv.volume_l, 0, ' L')}")
               + "\nzeleně dopouštění, modře přítok z deště")
        await self.api("sendPhoto", {"chat_id": str(cid), "caption": cap},
                       files={"photo": ("hladina.png", png, "image/png")})

    def _servis_text(self, st: dict) -> str:
        if not st.get("installed"):
            return "NetBird není nainstalován"
        mode = st.get("mode")
        if mode == "on":
            txt = "zapnut trvale"
        elif mode == "24h" and st.get("until"):
            txt = "zapnut do " + time.strftime("%d.%m. %H:%M", time.localtime(st["until"]))
        else:
            txt = "vypnut"
        if st.get("connected"):
            txt += f" · připojeno ({st.get('ip') or '?'})"
        return txt

    def _diag(self) -> str:
        c = self.core
        r = c.radio
        up = int(time.time() - c.started)
        lines = [f"<b>Hub {html.escape(', '.join(c.hostnames()))}</b>",
                 f"verze {__version__} · běží {up // 86400} d {up % 86400 // 3600} h",
                 f"rádio: {'OK' if r and r.hardware_ok else 'CHYBA'} ({getattr(r, 'mode', '?')}), "
                 f"resetů {getattr(r, 'resets', 0)}, CRC {getattr(r, 'crc_errors', 0)}, "
                 f"podpis {getattr(r, 'auth_failures', 0)}, duplikáty {getattr(r, 'duplicates', 0)}"]
        try:
            lines.append(f"volné místo: {_n(c._free_mb(), 0, ' MB')}")
        except (OSError, AttributeError):          # statvfs is POSIX only
            pass
        lines.append("servisní přístup: " + self._servis_text(c.servis_state()))
        for n in c.node_summaries():
            nid = int(n["id"], 16)
            last = c.last.get(nid) or {}
            lq = c.link_quality(nid)
            b = n["battery"] or {}
            lines.append("")
            lines.append(f"<b>{html.escape(n['name'])}</b> ({n['id']}) · {n['status']}, před {_n((n['age_s'] or 0) / 60, 0)} min")
            lines.append(f"signál {_n(n['rssi'], 0, ' dBm')} · ztráty {_n(lq.get('loss_pct'), 1, ' %')}"
                         f" · rušení {_n(lq.get('busy_pct'), 0, ' %')}")
            lines.append(f"baterie {_n(b.get('voltage'), 2, ' V')} ({_n(b.get('percent'), 0, ' %')})"
                         f", při vysílání −{_n(b.get('sag_mv'), 0, ' mV')}, ohřev {_n(b.get('heats_per_day'), 1)}×/den")
            warn = model.flag_list(last.get("flags", 0) & ~(model.FLAG_BOOT | model.FLAG_BAT_LOW))
            if warn:
                lines.append("⚠️ " + html.escape(", ".join(warn)))
        return "\n".join(lines)
