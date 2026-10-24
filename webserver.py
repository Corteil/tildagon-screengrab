"""
Web server for Screengrab: browse, download and delete shots, mirror the
screen live, and start captures from a browser.

It is a small HTTP/1.0 server built on non-blocking sockets driven from an
asyncio task. In hotspot mode it also behaves like a captive portal: a tiny DNS
responder sends every name to the badge, and the connectivity checks phones make
when they join a network (Android's generate_204, Apple's "Success" page, ...)
are answered, so the phone treats the hotspot as a usable network and keeps its
browser on it instead of falling back to mobile data. MicroPython's asyncio stream server takes 0.6 to 1 s to answer
each request on this badge (measured: a raw socket answers in 40-170 ms), so
the streams are not used.

Safe by default: off until switched on, never at boot, a PIN (unless you turn
it off) checked on every request, state changes only by POST with a custom
header (so a web page you visit can't drive it), at most MAX_CONNS clients,
small request limits, and shot paths that can only name real shots on a real
storage. The domain work (listing, deleting, moving) lives on the app, in its
web_* methods, so this file is only HTTP, auth and streaming.

License: MIT
"""
import asyncio
import errno
import gc
import hashlib
import json
import os
import socket
import sys
import time

import display

from .pngenc import iter_png

PORT = 80
FALLBACK_PORT = 8080
MAX_CONNS = 3
HEAD_MAX = 2048
BODY_MAX = 1024
READ_MS = 3000
SEND_MS = 10000
CHUNK = 8192
LOCKOUT_FAILS = 5
LOCKOUT_MS = 60000
ACCEPT_POLL_MS = 10
QR_TTL_MS = 600000  # a sign-in code stops working after this long, used or not
YIELD_MS = 60  # while encoding, hand the loop back after this much work
LOG = True  # one console line per connection and request (never the query, which holds codes), to debug on a phone

_AGAIN = (errno.EAGAIN, 11, 119)
_APPLE = b"<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"
# What phones and laptops ask when they join a network, to decide whether it has internet.
_PROBES = {
    "/generate_204": (204, None, b""),  # Android, Chrome OS
    "/gen_204": (204, None, b""),
    "/hotspot-detect.html": (200, "text/html", _APPLE),  # Apple
    "/library/test/success.html": (200, "text/html", _APPLE),
    "/connecttest.txt": (200, "text/plain", b"Microsoft Connect Test"),  # Windows
    "/ncsi.txt": (200, "text/plain", b"Microsoft NCSI"),
    "/success.txt": (200, "text/plain", b"success\n"),  # Firefox
    "/check_network_status.txt": (200, "text/plain", b"NetworkManager is online\n"),  # Linux
}
_STATUS = {200: "OK", 204: "No Content", 302: "Found", 421: "Misdirected Request", 400: "Bad Request", 401: "Unauthorized",
           403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
           413: "Payload Too Large", 429: "Too Many Requests", 431: "Header Too Large",
           500: "Server Error", 503: "Busy"}
# The session cookie is SameSite=Lax, not Strict: a link opened from a QR scanner or any other app is a
# cross-site navigation, and with Strict the browser drops the cookie on the redirect right after
# /q (it shows the PIN page). Lax is still not sent on cross-site POSTs or sub-requests, and every
# state-changing call also needs the X-Requested-With header.
_COMMON = (b"Connection: close\r\nX-Content-Type-Options: nosniff\r\n"
           b"Referrer-Policy: no-referrer\r\n")
_LOGIN = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Screengrab</title><body style="font:16px system-ui;background:#0d1020;color:#dde;text-align:center;padding-top:20vh">
<h2>Screengrab</h2><form action=/login><input name=k inputmode=numeric autofocus placeholder="PIN from the badge"
style="font-size:20px;padding:8px;text-align:center"> <button style="font-size:20px;padding:8px">Go</button></form><p>%s</p>"""


def dns_reply(query, ip):
    """The reply to one DNS query: an A record pointing at ip (4 bytes) for every
    name, and an empty answer for anything else (AAAA, TXT, ...), so a phone never
    waits on a name that has no answer. None for anything that isn't a plain query."""
    if len(query) < 17 or query[2] & 0x80 or (query[2] >> 3) & 15 or query[4:6] != b"\x00\x01":
        return None
    i = 12
    while i < len(query) and query[i]:  # walk the labels of the name
        i += query[i] + 1
    if i + 5 > len(query):
        return None
    qtype = (query[i + 1] << 8) | query[i + 2]
    answer = b""
    if qtype == 1:  # A: name pointer, type A, class IN, TTL 60, 4 bytes
        answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + bytes(ip)
    return (query[:2] + b"\x81\x80\x00\x01" + (b"\x00\x01" if answer else b"\x00\x00") + b"\x00\x00\x00\x00"
            + bytes(query[12:i + 5]) + answer)


class _Reply(Exception):
    """Stop handling a request and answer with this status and message."""

    def __init__(self, status, message=""):
        self.status = status
        self.message = message


def _same(a, b):
    """Compare two strings without stopping at the first difference."""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


def _query(qs):
    out = {}
    for pair in qs.split("&"):
        if pair:
            k, _, v = pair.partition("=")
            out[k] = v
    return out


def _here(name):
    """Path of a file shipped next to this module."""
    here = getattr(sys.modules.get(__name__), "__file__", None) or "/apps/tildagon_screengrab/webserver.py"
    return here.rsplit("/", 1)[0] + "/" + name


def _page_path():
    return _here("web.html")


# --- hotspot passwords --------------------------------------------------------

_RECORD = 6  # words.txt: each word padded to 5 letters, then a newline
_CLEAR = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 31 symbols: no 0/O or 1/I/L to misread


def _below(n):
    """A uniform random number in range(n), without modulo bias."""
    limit = 65536 - 65536 % n
    while True:
        r = int.from_bytes(os.urandom(2), "big")
        if r < limit:
            return r % n


def passphrase(count=4):
    """Random short words joined with hyphens, e.g. acid-acre-army-bath. They come
    from the EFF's Short Wordlist #1 (CC BY 3.0 US), 1295 words of 3-5 letters,
    so four are about 41 bits."""
    out = []
    with open(_here("words.txt"), "rb") as f:
        total = f.seek(0, 2) // _RECORD
        while len(out) < count:
            f.seek(_below(total) * _RECORD)
            word = f.read(5).decode().strip()
            if word and word not in out:
                out.append(word)
    return "-".join(out)


def password(length=8):
    """Random letters and digits with the look-alikes left out (about 40 bits at 8)."""
    return "".join(_CLEAR[_below(len(_CLEAR))] for _ in range(length))


class WebServer:
    def __init__(self, app, pin=None, idle_s=600, port=PORT, host="0.0.0.0", captive=False):
        self.app = app
        self.captive = captive  # hotspot: answer DNS and the phones' connectivity checks
        self.host = host  # the interface to answer on: only the badge's own hotspot, or only your Wi-Fi
        self.pin = pin
        self.idle_s = idle_s
        self.port = port
        salt = os.urandom(8)
        self.token = hashlib.sha256(salt + (pin or "").encode()).digest().hex()[:24] if pin else None
        self.running = False
        self.error = ""
        self.requests = 0
        self.conns = 0
        self.last_active = time.ticks_ms()
        self._ls = None
        self._task = None
        self._fails = 0
        self._locked_until = 0
        self._frame_busy = False
        self._dns = None
        self.qr_token = None  # the one-time sign-in code the badge is showing, if any
        self.qr_until = 0
        self.signins = 0  # phones signed in with a code, for the badge's screen

    # --- lifecycle ----------------------------------------------------------

    def start(self):
        for port in (self.port, FALLBACK_PORT):
            ls = socket.socket()
            try:
                ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                ls.bind((self.host, port))
                ls.listen(6)  # extra clients wait here until a slot frees up
                ls.setblocking(False)
            except OSError as e:
                ls.close()
                self.error = "port %d: %s" % (port, e)
                continue
            self._ls, self.port, self.error = ls, port, ""
            break
        else:
            raise OSError(self.error)
        self.running = True
        self.last_active = time.ticks_ms()
        self._task = asyncio.create_task(self._serve())
        if self.captive:
            self._start_dns()

    def _start_dns(self):
        """DNS runs in its own thread with a blocking read: the network stack queues only a
        handful of UDP packets, and a phone sends a burst of queries when it joins, so waiting
        for the (busy) event loop to come round would drop some. It needs almost no CPU."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, 53))
            s.settimeout(0.3)
        except OSError as e:
            print("Web: no DNS responder:", e)  # the page still works by IP address
            return
        import _thread

        self._dns = s
        old = _thread.stack_size(6 * 1024)
        try:
            _thread.start_new_thread(self._dns_thread, (s,))
        finally:
            _thread.stack_size(old)

    def _dns_thread(self, s):
        ip = bytes(int(x) for x in self.host.split("."))
        while self.running and self._dns is s:
            try:
                data, addr = s.recvfrom(512)
            except OSError:
                continue  # the timeout, so the loop can notice it should stop
            reply = dns_reply(data, ip)
            if reply:
                try:
                    s.sendto(reply, addr)
                except OSError:
                    pass
        s.close()

    def stop(self):
        self.running = False
        self._dns = None  # its thread sees this within 0.3 s and closes the socket itself
        if self._ls:
            self._ls.close()  # first, so nothing can queue up behind a dead server
            self._ls = None
        task, self._task = self._task, None
        try:
            me = asyncio.current_task()  # the idle timeout stops us from inside our own task
        except RuntimeError:  # called with no event loop running
            me = None
        if task and task is not me:
            task.cancel()

    def idle_left(self):
        """Seconds until the idle timeout switches the server off, or None."""
        if not self.idle_s:
            return None
        return max(0, self.idle_s - time.ticks_diff(time.ticks_ms(), self.last_active) // 1000)

    async def _serve(self):
        while self.running:
            if self.conns >= MAX_CONNS:  # leave the rest queued in the listen backlog
                await asyncio.sleep_ms(ACCEPT_POLL_MS)
                continue
            try:
                c, _ = self._ls.accept()
            except OSError:
                await asyncio.sleep_ms(ACCEPT_POLL_MS)
                if self.idle_s and self.idle_left() == 0 and not self.conns:
                    self.app.web_idle_off()
                    return
                continue
            if LOG:
                print("Web: connection opened (%d open)" % (self.conns + 1))
            asyncio.create_task(self._client(c))

    # --- connection ---------------------------------------------------------

    async def _client(self, c):
        self.conns += 1
        try:
            c.setblocking(False)
            await self._handle(c)
        except _Reply as r:
            if LOG:
                print("Web: replied %d (%s)" % (r.status, r.message))
            try:
                await self._text(c, r.status, r.message or _STATUS.get(r.status, ""))
            except Exception:
                pass
        except Exception as e:
            print("Web: error", repr(e))
            sys.print_exception(e)
            try:  # a browser that gets nothing back says ERR_EMPTY_RESPONSE: say what went wrong instead
                await self._text(c, 500, "server error: %r" % (e,))
            except Exception:
                pass
        finally:
            self.conns -= 1
            try:
                c.close()
            except Exception:
                pass

    async def _read_until(self, c, buf, enough):
        t = time.ticks_ms()
        while not enough(buf):
            try:
                ch = c.recv(512)
            except OSError as e:
                if e.errno not in _AGAIN:
                    raise
                if time.ticks_diff(time.ticks_ms(), t) > READ_MS:
                    raise _Reply(408, "timed out")
                await asyncio.sleep_ms(2)
                continue
            if not ch:
                raise _Reply(400, "closed")
            buf += ch
        return buf

    async def _handle(self, c):
        buf = await self._read_until(c, b"", lambda b: b"\r\n\r\n" in b or len(b) > HEAD_MAX)
        i = buf.find(b"\r\n\r\n")
        if i < 0:
            raise _Reply(431)
        lines = buf[:i].decode().split("\r\n")
        body = buf[i + 4:]
        try:
            method, target, _ = lines[0].split(" ")
        except ValueError:
            raise _Reply(400, "bad request line")
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        try:
            want = int(headers.get("content-length", "0") or 0)
        except ValueError:
            raise _Reply(400, "bad Content-Length")
        if want < 0 or want > BODY_MAX:
            raise _Reply(413)
        body = await self._read_until(c, body, lambda b: len(b) >= want)
        path, _, qs = target.partition("?")
        if "%" in path or ".." in path or "\\" in path or not path.startswith("/"):
            raise _Reply(400, "bad path")
        if self.captive:
            host = headers.get("host", "").split(":")[0]
            probe = _PROBES.get(path)
            if probe and method == "GET":  # a phone checking for internet: say yes, and don't count it as use
                status, ctype, data = probe
                if LOG:
                    print("Web: connectivity check", path)
                return await self._send(c, self._head(status, ctype, len(data)) + data)
            if host and host != self.host:  # any other name that reached us through our DNS
                if method == "GET":
                    return await self._redirect(c, "http://%s/" % self.host)
                raise _Reply(421, "wrong host")
        self.requests += 1
        self.last_active = time.ticks_ms()
        if LOG:
            print("Web: %s %s" % (method, path))
        await self._route(c, method, path, _query(qs), headers, body[:want])

    # --- auth ---------------------------------------------------------------

    def _authed(self, headers):
        if not self.pin:
            return True
        if _same(headers.get("x-pin", ""), self.pin):
            return True
        for part in headers.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sg" and _same(v, self.token):
                return True
        return False

    def new_qr_token(self):
        """Make the one-time code for the sign-in QR (replacing any unused one)."""
        self.qr_token = password(8)
        self.qr_until = time.ticks_add(time.ticks_ms(), QR_TTL_MS)
        return self.qr_token

    def qr_valid(self):
        return self.qr_token is not None and time.ticks_diff(self.qr_until, time.ticks_ms()) > 0

    async def _quick_login(self, c, q):
        """GET /q?t=CODE: what the sign-in QR opens. The code works once, so it
        can't be replayed from the phone's history, and wrong tries count towards
        the same lockout as a wrong PIN."""
        if not self.pin:
            return await self._redirect(c, "/")
        if self._locked():
            raise _Reply(429, "too many wrong tries, wait a minute")
        if self.qr_valid() and _same(q.get("t", ""), self.qr_token):
            self.qr_token = None  # used up
            self.signins += 1
            self._fails = 0
            return await self._redirect(c, "/", "Set-Cookie: sg=%s; Path=/; SameSite=Lax; HttpOnly\r\n" % self.token)
        self._fails += 1
        if self._fails >= LOCKOUT_FAILS:
            self._locked_until = time.ticks_add(time.ticks_ms(), LOCKOUT_MS)
        await self._html(c, 401, _LOGIN % "That code was already used or has expired. Scan a fresh one on the badge.")

    def _locked(self):
        return self._fails >= LOCKOUT_FAILS and time.ticks_diff(self._locked_until, time.ticks_ms()) > 0

    async def _login(self, c, q):
        if not self.pin:
            return await self._redirect(c, "/")
        if self._locked():
            raise _Reply(429, "too many wrong PINs, wait a minute")
        if _same(q.get("k", ""), self.pin):
            self._fails = 0
            return await self._redirect(c, "/", "Set-Cookie: sg=%s; Path=/; SameSite=Lax; HttpOnly\r\n" % self.token)
        self._fails += 1
        if self._fails >= LOCKOUT_FAILS:
            self._locked_until = time.ticks_add(time.ticks_ms(), LOCKOUT_MS)
        await self._html(c, 401, _LOGIN % "Wrong PIN")

    # --- routing ------------------------------------------------------------

    async def _route(self, c, method, path, q, headers, body):
        if path == "/favicon.ico":
            return await self._send(c, self._head(204, length=0))
        if path == "/login":
            return await self._login(c, q)
        if path == "/q":
            return await self._quick_login(c, q)
        if method not in ("GET", "POST"):
            raise _Reply(405)
        if not self._authed(headers):
            if path == "/":
                return await self._html(c, 401, _LOGIN % "")
            raise _Reply(401, "PIN needed")
        if method == "POST":
            if headers.get("x-requested-with") != "screengrab":
                raise _Reply(403, "missing X-Requested-With: screengrab")
            return await self._post(c, path, body)
        if path == "/":
            return await self._file(c, _page_path(), "text/html; charset=utf-8")
        if path == "/api/status":
            return await self._json(c, self.app.web_status(q.get("gc") == "1"))
        if path == "/api/shots":
            try:
                offset, limit = int(q.get("offset", 0)), int(q.get("limit", 60))
            except ValueError:
                raise _Reply(400, "offset and limit must be numbers")
            return await self._json(c, self.app.web_shots(q.get("storage"), offset, limit))
        if path == "/api/frame":
            return await self._frame(c, q.get("mask") == "1" if "mask" in q else self.app._mask)
        if path.startswith("/shot/"):
            _, _, rest = path[1:].partition("/")
            storage, _, name = rest.partition("/")
            real = self.app.web_shot_path(storage, name)
            if not real:
                raise _Reply(404)
            extra = ""
            if q.get("dl") == "1":
                extra = 'Content-Disposition: attachment; filename="%s"\r\n' % name
            return await self._file(c, real, "image/png", "Cache-Control: public, max-age=31536000, immutable\r\n" + extra)
        raise _Reply(404)

    async def _post(self, c, path, body):
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            raise _Reply(400, "bad JSON")
        if not isinstance(data, dict):
            raise _Reply(400, "JSON object expected")
        app = self.app
        if path == "/api/capture":
            delay = data.get("delay")
            if delay is not None and (not isinstance(delay, int) or not 0 <= delay <= 60):
                raise _Reply(400, "delay must be 0-60")
            app.trigger_capture(delay)
            return await self._json(c, {"ok": True})
        if path == "/api/delete":
            err = app.web_delete(data.get("storage"), data.get("name"))
        elif path == "/api/move":
            err = app.web_move(data.get("storage"), data.get("name"), data.get("to"))
        elif path == "/api/timelapse":
            if "interval" in data:
                err = app.set_option("interval", data["interval"])
                if err:
                    raise _Reply(400, err)
            err = app.set_timelapse(bool(data.get("on")))
        elif path == "/api/options":
            err = ""
            for k, v in data.items():
                err = app.set_option(k, v)
                if err:
                    break
        else:
            raise _Reply(404)
        if err:
            raise _Reply(400, err)
        await self._json(c, {"ok": True})

    # --- responses ----------------------------------------------------------

    def _head(self, status, ctype=None, length=None, extra=""):
        h = "HTTP/1.0 %d %s\r\n" % (status, _STATUS.get(status, ""))
        if ctype:
            h += "Content-Type: %s\r\n" % ctype
        if length is not None:
            h += "Content-Length: %d\r\n" % length
        if "Cache-Control" not in extra:
            h += "Cache-Control: no-store\r\n"
        return h.encode() + extra.encode() + _COMMON + b"\r\n"

    async def _send(self, c, data):
        mv = memoryview(data)
        if LOG and bytes(mv[:5]) == b"HTTP/":
            print("Web:   ->", bytes(mv[9:12]).decode())
        t = time.ticks_ms()
        while len(mv):
            try:
                n = c.send(mv[:CHUNK])
                mv = mv[n:]
                t = time.ticks_ms()
            except OSError as e:
                if e.errno not in _AGAIN:
                    raise
                if time.ticks_diff(time.ticks_ms(), t) > SEND_MS:
                    raise
                await asyncio.sleep_ms(1)

    async def _text(self, c, status, text, ctype="text/plain; charset=utf-8"):
        body = text.encode()
        await self._send(c, self._head(status, ctype, len(body)) + body)

    async def _html(self, c, status, text):
        await self._text(c, status, text, "text/html; charset=utf-8")

    async def _json(self, c, obj):
        await self._text(c, 200, json.dumps(obj), "application/json")

    async def _redirect(self, c, where, extra=""):
        await self._send(c, self._head(302, length=0, extra="Location: %s\r\n%s" % (where, extra)))

    async def _file(self, c, path, ctype, extra=""):
        try:
            size = os.stat(path)[6]
            f = open(path, "rb")
        except OSError:
            raise _Reply(404)
        try:
            await self._send(c, self._head(200, ctype, size, extra))
            buf = bytearray(CHUNK)
            while True:
                n = f.readinto(buf)
                if not n:
                    break
                await self._send(c, memoryview(buf)[:n])
        finally:
            f.close()

    async def _frame(self, c, mask):
        """The screen as it is now, as a PNG, streamed as it is compressed.

        Every time this code waits, the badge's event loop goes round once, and
        while a foreground app redraws continuously that is ~90 ms. So it
        batches the encoder's small pieces into big sends and only yields once
        it has done YIELD_MS of work or the socket is full."""
        if self._frame_busy:
            raise _Reply(503, "busy")
        self._frame_busy = True
        try:
            gc.collect()
            fb = display.get_framebuffer()
            await self._send(c, self._head(200, "image/png"))
            buf = bytearray(CHUNK)
            n = 0
            last = time.ticks_ms()
            for part in iter_png(fb, mask):
                size = len(part)
                if n + size > CHUNK:
                    await self._send(c, memoryview(buf)[:n])
                    n = 0
                if size > CHUNK:
                    await self._send(c, part)
                else:
                    buf[n:n + size] = part
                    n += size
                if time.ticks_diff(time.ticks_ms(), last) > YIELD_MS:
                    await asyncio.sleep_ms(0)  # let the badge's own screen update
                    last = time.ticks_ms()
            if n:
                await self._send(c, memoryview(buf)[:n])
        finally:
            self._frame_busy = False
