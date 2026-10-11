"""Minimal, strict HTTP/1.1 server on asyncio streams (standard library only).

Why not aiohttp/FastAPI: on a Pi Zero 2 W every MB counts – aiohttp alone adds
~15 MB RSS, and distribution packages differ between OS releases. This module
implements exactly the subset the application needs, with conservative limits:

  * request line + headers <= 16 KiB, <= 64 headers, read timeout 15 s
  * bodies only with Content-Length (Transfer-Encoding requests are refused ->
    no request smuggling), size limited per application
  * keep-alive with 30 s idle timeout, connection cap
  * responses always with Content-Length, or chunked for StreamResponse

The API mirrors the small part of aiohttp.web used by retencka.web.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from email.utils import formatdate
from http import HTTPStatus
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qsl, unquote

log = logging.getLogger("retencka.http")

MAX_HEADER_BYTES = 16 * 1024
MAX_HEADERS = 64
HEADER_TIMEOUT = 15.0
BODY_TIMEOUT = 20.0
KEEPALIVE_TIMEOUT = 30.0
MAX_CONNECTIONS = 64
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


# ============================================================== containers
class Headers:
    """Case-insensitive single-valued header map (Set-Cookie handled separately)."""

    def __init__(self, items: Optional[dict] = None):
        self._d: dict[str, tuple[str, str]] = {}
        for k, v in (items or {}).items():
            self[k] = v

    def __setitem__(self, k: str, v: str) -> None:
        self._d[k.lower()] = (k, str(v))

    def __getitem__(self, k: str) -> str:
        return self._d[k.lower()][1]

    def __contains__(self, k: str) -> bool:
        return k.lower() in self._d

    def __delitem__(self, k: str) -> None:
        self._d.pop(k.lower(), None)

    def get(self, k: str, default: Any = None) -> Any:
        v = self._d.get(k.lower())
        return default if v is None else v[1]

    def setdefault(self, k: str, v: str) -> str:
        if k.lower() not in self._d:
            self[k] = v
        return self[k]

    def items(self):
        return self._d.values()


class Request:
    def __init__(self, app: "Application", method: str, target: str, version: str,
                 headers: Headers, body: bytes, remote: str):
        self.app = app
        self.method = method
        self.version = version
        self.headers = headers
        self.remote = remote
        self.secure = False
        self._body = body
        path, _, qs = target.partition("?")
        self.path = unquote(path)
        self.query = dict(parse_qsl(qs, keep_blank_values=True, max_num_fields=50))
        self.match_info: dict[str, str] = {}
        self._store: dict[str, Any] = {}
        self.cookies = _parse_cookies(headers.get("Cookie", ""))

    @property
    def host(self) -> str:
        return self.headers.get("Host", "")

    @property
    def content_length(self) -> Optional[int]:
        return len(self._body) if self._body else None

    async def read(self) -> bytes:
        return self._body

    async def json(self) -> Any:
        return json.loads(self._body.decode("utf-8") or "null")

    def __getitem__(self, k: str) -> Any:
        return self._store[k]

    def __setitem__(self, k: str, v: Any) -> None:
        self._store[k] = v

    def get(self, k: str, default: Any = None) -> Any:
        return self._store.get(k, default)


def _parse_cookies(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(";")[:50]:
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k and k not in out:
                out[k] = v.strip().strip('"')
    return out


# =============================================================== responses
class Response:
    def __init__(self, body: Any = b"", status: int = 200, content_type: Optional[str] = None,
                 charset: Optional[str] = None, headers: Optional[dict] = None, text: Optional[str] = None):
        self.status = status
        self.headers = Headers(headers)
        self._cookies: list[str] = []
        if text is not None:
            body = text.encode("utf-8")
            if content_type is None:
                content_type, charset = "text/plain", "utf-8"
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.body: bytes = body or b""
        if content_type:
            self.headers["Content-Type"] = content_type + (f"; charset={charset}" if charset else "")

    def set_cookie(self, name: str, value: str, max_age: Optional[int] = None, path: str = "/",
                   httponly: bool = False, secure: bool = False, samesite: Optional[str] = None) -> None:
        c = f"{name}={value}; Path={path}"
        if max_age is not None:
            c += f"; Max-Age={int(max_age)}"
        if httponly:
            c += "; HttpOnly"
        if secure:
            c += "; Secure"
        if samesite:
            c += f"; SameSite={samesite}"
        self._cookies.append(c)

    def del_cookie(self, name: str, path: str = "/") -> None:
        self.set_cookie(name, "", max_age=0, path=path)

    def _head(self, keep_alive: bool, length: Optional[int]) -> bytes:
        reason = HTTPStatus(self.status).phrase if self.status in HTTPStatus._value2member_map_ else ""
        lines = [f"HTTP/1.1 {self.status} {reason}"]
        self.headers["Date"] = formatdate(usegmt=True)
        self.headers["Server"] = "retencka"
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.headers["Connection"] = "keep-alive" if keep_alive else "close"
        for k, v in self.headers.items():
            lines.append(f"{k}: {v}")
        for c in self._cookies:
            lines.append(f"Set-Cookie: {c}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")


def json_response(data: Any, status: int = 200, dumps: Callable[[Any], str] = json.dumps) -> Response:
    return Response(body=dumps(data), status=status, content_type="application/json", charset="utf-8")


class HTTPException(Response, Exception):
    status_code = 500

    def __init__(self, text: Optional[str] = None, content_type: Optional[str] = None,
                 headers: Optional[dict] = None, **_ignored):
        Response.__init__(self, status=self.status_code, headers=headers,
                          text=text if text is not None else f"{self.status_code}: {HTTPStatus(self.status_code).phrase}")
        if content_type:
            self.headers["Content-Type"] = content_type + "; charset=utf-8"
        Exception.__init__(self, self.status_code)


class HTTPFound(HTTPException):
    status_code = 302

    def __init__(self, location: str):
        super().__init__(text="", headers={"Location": location})


class HTTPBadRequest(HTTPException):
    status_code = 400


class HTTPForbidden(HTTPException):
    status_code = 403


class HTTPNotFound(HTTPException):
    status_code = 404


class HTTPMethodNotAllowed(HTTPException):
    status_code = 405


class HTTPRequestEntityTooLarge(HTTPException):
    status_code = 413


class StreamResponse(Response):
    """Chunked response for large downloads (CSV export)."""

    def __init__(self, status: int = 200, headers: Optional[dict] = None):
        super().__init__(status=status, headers=headers)
        self._writer: Optional[asyncio.StreamWriter] = None
        self.prepared = False

    async def prepare(self, request: Request) -> None:
        self._writer = request["_writer"]
        self.headers["Transfer-Encoding"] = "chunked"
        mws = request.app.prepare_hooks
        for hook in mws:
            hook(request, self)
        self._writer.write(self._head(False, None))
        self.prepared = True

    async def write(self, data: bytes) -> None:
        if data:
            self._writer.write(b"%x\r\n%s\r\n" % (len(data), data))
            await self._writer.drain()

    async def write_eof(self) -> None:
        self._writer.write(b"0\r\n\r\n")
        await self._writer.drain()


# ============================================================== application
Handler = Callable[[Request], Awaitable[Response]]


def middleware(fn):
    return fn


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern, Handler]] = []

    def _add(self, method: str, path: str, handler: Handler) -> None:
        rx = "^" + re.sub(r"\{(\w+)(?::([^}]+))?\}",
                          lambda m: f"(?P<{m.group(1)}>{m.group(2) or '[^/]+'})", path) + "$"
        self.routes.append((method, re.compile(rx), handler))

    def add_get(self, path: str, handler: Handler) -> None:
        self._add("GET", path, handler)

    def add_post(self, path: str, handler: Handler) -> None:
        self._add("POST", path, handler)

    def resolve(self, method: str, path: str) -> tuple[Optional[Handler], dict]:
        allowed = False
        for m, rx, h in self.routes:
            mt = rx.match(path)
            if mt:
                if m == method or (m == "GET" and method == "HEAD"):
                    return h, mt.groupdict()
                allowed = True
        if allowed:
            raise HTTPMethodNotAllowed()
        raise HTTPNotFound()


class Application:
    def __init__(self, client_max_size: int = 64 * 1024, middlewares: Optional[list] = None):
        self.client_max_size = client_max_size
        self.middlewares = list(middlewares or [])
        self.router = Router()
        self.prepare_hooks: list[Callable[[Request, Response], None]] = []
        self._store: dict[str, Any] = {}

    def __getitem__(self, k: str) -> Any:
        return self._store[k]

    def __setitem__(self, k: str, v: Any) -> None:
        self._store[k] = v

    async def _dispatch(self, request: Request) -> Response:
        async def endpoint(req: Request) -> Response:
            handler, info = self.router.resolve(req.method, req.path)
            req.match_info = info
            return await handler(req)

        call = endpoint
        for mw in reversed(self.middlewares):
            call = (lambda m, nxt: (lambda r: m(r, nxt)))(mw, call)
        return await call(request)


# =================================================================== server
class Server:
    def __init__(self, app: Application):
        self.app = app
        self.connections = 0
        self._server: Optional[asyncio.base_events.Server] = None

    async def start(self, host: str, port: int, backlog: int = 32) -> None:
        self._server = await asyncio.start_server(self._client, host, port, backlog=backlog,
                                                  reuse_address=True, limit=MAX_HEADER_BYTES)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.connections >= MAX_CONNECTIONS:
            writer.close()
            return
        self.connections += 1
        peer = writer.get_extra_info("peername")
        remote = peer[0] if isinstance(peer, tuple) else ""
        try:
            first = True
            while True:
                keep = await self._one(reader, writer, remote, HEADER_TIMEOUT if first else KEEPALIVE_TIMEOUT)
                first = False
                if not keep:
                    break
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            log.exception("connection handler crashed")
        finally:
            self.connections -= 1
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 2)
            except Exception:
                pass

    async def _send_error(self, writer: asyncio.StreamWriter, status: int) -> bool:
        resp = Response(text=f"{status} {HTTPStatus(status).phrase}", status=status)
        writer.write(resp._head(False, len(resp.body)) + resp.body)
        await writer.drain()
        return False

    async def _one(self, reader, writer, remote: str, timeout: float) -> bool:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        except asyncio.LimitOverrunError:
            return await self._send_error(writer, 431)
        except asyncio.IncompleteReadError as e:
            if e.partial.strip():
                return await self._send_error(writer, 400)
            return False
        try:
            text = raw.decode("latin-1")
            lines = text.split("\r\n")
            method, target, version = lines[0].split(" ")
            if not _TOKEN.match(method) or not target.startswith("/") or version not in ("HTTP/1.1", "HTTP/1.0"):
                raise ValueError
            headers = Headers()
            count = 0
            for line in lines[1:]:
                if not line:
                    continue
                name, sep, value = line.partition(":")
                if not sep or not _TOKEN.match(name):
                    raise ValueError
                count += 1
                if count > MAX_HEADERS:
                    return await self._send_error(writer, 431)
                if name.lower() in headers and name.lower() in ("content-length", "host"):
                    raise ValueError            # duplicated critical header
                headers[name] = value.strip()
        except ValueError:
            return await self._send_error(writer, 400)

        if "Transfer-Encoding" in headers:
            return await self._send_error(writer, 501)
        try:
            length = int(headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError
        except ValueError:
            return await self._send_error(writer, 400)
        if length > self.app.client_max_size:
            return await self._send_error(writer, 413)
        if length and headers.get("Expect", "").lower() == "100-continue":
            writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            await writer.drain()
        body = await asyncio.wait_for(reader.readexactly(length), BODY_TIMEOUT) if length else b""

        conn_hdr = headers.get("Connection", "").lower()
        keep_alive = (version == "HTTP/1.1" and conn_hdr != "close") or \
                     (version == "HTTP/1.0" and conn_hdr == "keep-alive")

        request = Request(self.app, method, target, version, headers, body, remote)
        request["_writer"] = writer
        try:
            resp = await self.app._dispatch(request)
        except HTTPException as e:
            resp = e
        except Exception:
            log.exception("handler error on %s %s", method, request.path)
            resp = Response(text="500 Internal Server Error", status=500)

        if isinstance(resp, StreamResponse):
            return False                       # chunked stream already written, close
        payload = b"" if method == "HEAD" or resp.status in (204, 304) else resp.body
        writer.write(resp._head(keep_alive, len(resp.body) if resp.status not in (204, 304) else None) + payload)
        await writer.drain()
        return keep_alive
