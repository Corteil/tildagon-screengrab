#!/usr/bin/env python3
"""Host-side tests for the Screengrab web server, run against a badge.

    python3 tests/web_test.py http://192.168.68.92 <PIN>          # PIN from the Web tile
    python3 tests/web_test.py http://192.168.68.92 none           # server started with the PIN off
    python3 tests/web_test.py http://192.168.68.92 <PIN> <CODE>   # also test the sign-in QR's one-time code

It needs a server with at least two storages and some shots (the tile's own
card plus flash is fine), and it saves, moves and deletes shots to exercise
the actions, so don't point it at a badge holding shots you care about. The
wrong-PIN lockout test runs last and leaves the server locked for a minute.
Needs Pillow for the image checks (it skips them without).
"""
import http.client
import io
import json
import socket
import sys
import threading
import time
import urllib.parse

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.68.92"
PIN = sys.argv[2] if len(sys.argv) > 2 else "none"
QR = sys.argv[3] if len(sys.argv) > 3 else None  # the one-time code the badge's sign-in QR shows
U = urllib.parse.urlparse(BASE)
HOST, PORT = U.hostname, U.port or 80
HDR = {"X-Requested-With": "screengrab", "Content-Type": "application/json"}

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (("  (" + str(detail) + ")") if detail != "" else ""))


def req(method, path, body=None, headers=None, cookie=None, timeout=15):
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    elif PIN != "none" and "X-PIN" not in h and "no-auth" not in h:
        h["X-PIN"] = PIN
    h.pop("no-auth", None)
    c = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    t = time.time()
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    dt = time.time() - t
    out = (r.status, data, r, dt)
    c.close()
    return out


def post(path, obj, **kw):
    return req("POST", path, json.dumps(obj), HDR, **kw)


def raw(data, timeout=6, read=True):
    s = socket.create_connection((HOST, PORT), timeout=timeout)
    out = b""
    try:
        s.sendall(data)
        while read:
            ch = s.recv(4096)
            if not ch:
                break
            out += ch
    except socket.timeout:
        pass
    except (ConnectionResetError, BrokenPipeError):
        pass  # the server refused and closed while we were still sending
    s.close()
    return out


def status_of(resp):
    return int(resp.split(b" ", 2)[1]) if resp.startswith(b"HTTP/") else None


def jget(path):
    s, d, _, _ = req("GET", path)
    return json.loads(d) if s == 200 else None


def png_ok(data, size=(240, 240)):
    if Image is None:
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    im = Image.open(io.BytesIO(data))
    im.load()
    return im.size == size and im.mode == "RGB"


# ---------------------------------------------------------------------------

print("== auth")
s, d, _, _ = req("GET", "/", headers={"no-auth": 1})
if PIN != "none":
    check("page without PIN is refused", s == 401 and b"PIN" in d, s)
    for p in ("/api/status", "/api/shots", "/api/frame", "/shot/flash/shot_0001.png"):
        s, d, _, _ = req("GET", p, headers={"no-auth": 1})
        check("GET %s without PIN -> 401" % p, s == 401, s)
    s, d, _, _ = post("/api/capture", {}, cookie="sg=wrong")
    check("POST with a wrong cookie -> 401", s == 401, s)
    s, d, r, _ = req("GET", "/login?k=%s" % PIN, headers={"no-auth": 1})
    cookie = r.getheader("Set-Cookie", "")
    check("right PIN redirects and sets a cookie", s == 302 and cookie.startswith("sg="), s)
    check("cookie is HttpOnly and SameSite=Lax", "HttpOnly" in cookie and "SameSite=Lax" in cookie)
    check("cookie is not just the PIN", PIN not in cookie)
    ck = cookie.split(";")[0]
    s, d, _, _ = req("GET", "/api/status", cookie=ck)
    check("cookie authenticates", s == 200, s)
else:
    check("page opens with the PIN off", s == 200, s)

if PIN != "none" and QR:
    print("== sign-in QR code (one-time)")
    s, d, r, _ = req("GET", "/q?t=NOTTHECODE", headers={"no-auth": 1})
    check("a wrong code is refused with no cookie", s == 401 and not r.getheader("Set-Cookie") and b"fresh" in d, s)
    s, d, r, _ = req("GET", "/q", headers={"no-auth": 1})
    check("no code at all is refused", s == 401 and not r.getheader("Set-Cookie"), s)
    s, d, r, _ = req("GET", "/q?t=%s" % QR, headers={"no-auth": 1})
    qcookie = r.getheader("Set-Cookie", "")
    check("the right code signs in: redirect and a cookie", s == 302 and r.getheader("Location") == "/" and qcookie.startswith("sg="), s)
    check("that cookie is HttpOnly, SameSite=Lax and doesn't contain the code or PIN",
          "HttpOnly" in qcookie and "SameSite=Lax" in qcookie and QR not in qcookie and PIN not in qcookie)
    s, d, _, _ = req("GET", "/api/status", cookie=qcookie.split(";")[0])
    check("the cookie works for the API", s == 200, s)
    s, d, r, _ = req("GET", "/q?t=%s" % QR, headers={"no-auth": 1})
    check("the same code can't be used twice", s == 401 and not r.getheader("Set-Cookie"), s)
    s, d, _, _ = req("GET", "/api/status", cookie=qcookie.split(";")[0])
    check("the first phone stays signed in", s == 200, s)

print("== page and read-only API")
s, d, r, dt = req("GET", "/")
check("page served", s == 200 and b"Screengrab" in d and "text/html" in r.getheader("Content-Type"), "%d bytes, %.2fs" % (len(d), dt))
check("security headers", r.getheader("X-Content-Type-Options") == "nosniff" and r.getheader("Referrer-Policy") == "no-referrer")
st = jget("/api/status?gc=1")
check("status has storages and options", bool(st) and len(st["storages"]) >= 1 and "heap" in st and "delays" in st, st and list(st))
names = [x["name"] for x in st["storages"]]
heap0 = st["heap"]
shots = {n: jget("/api/shots?storage=%s" % n) for n in names}
check("shots listing newest first", all(
    [x["n"] for x in v["shots"]] == sorted([x["n"] for x in v["shots"]], reverse=True) for v in shots.values()))
src = next((n for n in names if shots[n]["total"]), None)
check("at least one shot to test with", src is not None, {n: shots[n]["total"] for n in names})
check("paging limit respected", len(jget("/api/shots?storage=%s&limit=1" % src)["shots"]) == 1)
s, d, _, _ = req("GET", "/api/shots?limit=abc")
check("non-numeric limit -> 400", s == 400, s)
check("unknown storage lists nothing", jget("/api/shots?storage=nope")["shots"] == [])

print("== shots")
first = shots[src]["shots"][0]
s, d, r, dt = req("GET", "/shot/%s/%s" % (src, first["n"]))
check("shot downloads, valid PNG, right length", s == 200 and png_ok(d) and len(d) == first["s"] == int(r.getheader("Content-Length")), "%d B in %.2fs" % (len(d), dt))
check("shot is cacheable forever", "immutable" in (r.getheader("Cache-Control") or ""))
s, d, r, _ = req("GET", "/shot/%s/%s?dl=1" % (src, first["n"]))
check("?dl=1 gives an attachment", "attachment" in (r.getheader("Content-Disposition") or ""))

print("== path traversal and bad input")
bad = ["/shot/%s/../../boot.py" % src, "/shot/%s/..%%2f..%%2fboot.py" % src, "/shot/..%2fboot.py/x", "/shot/%s/boot.py" % src,
       "/shot/%s/shot_0001.png/../../boot.py" % src, "/shot/%s/shot_.png" % src, "/shot/nosuch/%s" % first["n"],
       "/shot//%s" % first["n"], "/shot/%s/" % src, "/shot/%s/%s" % (src, "a" * 200 + ".png"), "/../boot.py", "/api/../boot.py",
       "/shot/%s/shot_0001\\..\\boot.png" % src]
for p in bad:
    s, d, _, _ = req("GET", p)
    check("refused: %s" % p[:60], s in (400, 404) and b"def " not in d and b"import " not in d, s)
check("unknown path -> 404", req("GET", "/nope")[0] == 404)
for m in ("PUT", "DELETE", "HEAD", "PATCH"):
    r_ = raw(("%s / HTTP/1.0\r\nX-PIN: %s\r\n\r\n" % (m, PIN)).encode())
    check("%s -> 405" % m, status_of(r_) == 405, status_of(r_))
check("garbage request line -> 400", status_of(raw(b"HELLO\r\n\r\n")) == 400)
check("huge header refused (431, or the connection reset)", status_of(raw(b"GET / HTTP/1.0\r\nX-A: " + b"a" * 5000 + b"\r\n\r\n")) in (431, 400, None))
check("oversized body -> 413", status_of(raw(b"POST /api/capture HTTP/1.0\r\nX-Requested-With: screengrab\r\nX-PIN: %s\r\nContent-Length: 5000\r\n\r\n" % PIN.encode())) == 413)
check("bad Content-Length -> 400", status_of(raw(b"POST /api/capture HTTP/1.0\r\nContent-Length: abc\r\n\r\n")) == 400)

print("== POST protections")
s, d, _, _ = req("POST", "/api/capture", "{}", {"Content-Type": "application/json"})
check("POST without X-Requested-With -> 403", s == 403, s)
s, d, _, _ = req("POST", "/api/capture", "{not json", HDR)
check("bad JSON -> 400", s == 400, s)
s, d, _, _ = req("POST", "/api/capture", "[1,2]", HDR)
check("JSON array rather than object -> 400", s == 400, s)
s, d, _, _ = post("/api/capture", {"delay": 9999})
check("absurd delay -> 400", s == 400, s)
s, d, _, _ = post("/api/options", {"delay": 7})
check("delay off the ladder -> 400", s == 400, s)
s, d, _, _ = post("/api/options", {"mystery": 1})
check("unknown option -> 400", s == 400, s)
s, d, _, _ = post("/api/delete", {"storage": src, "name": "../boot.py"})
check("delete with a traversal name -> 400", s == 400, s)
s, d, _, _ = post("/api/delete", {"storage": "nope", "name": first["n"]})
check("delete on unknown storage -> 400", s == 400, s)
s, d, _, _ = post("/api/move", {"storage": src, "name": first["n"], "to": src})
check("move onto itself -> 400", s == 400, s)

print("== actions")
before = jget("/api/shots?storage=%s" % st["target"])["total"]
s, d, _, _ = post("/api/capture", {"delay": 0})
check("capture accepted", s == 200, s)
t0 = time.time()
after = before
while time.time() - t0 < 10 and after == before:
    time.sleep(0.5)
    after = jget("/api/shots?storage=%s" % st["target"])["total"]
check("a new shot appears (~%.1fs)" % (time.time() - t0), after == before + 1, "%d -> %d" % (before, after))
newest = jget("/api/shots?storage=%s" % st["target"])["shots"][0]
check("the new shot is a valid PNG", png_ok(req("GET", "/shot/%s/%s" % (st["target"], newest["n"]))[1]))
if len(names) > 1:
    other = next(n for n in names if n != st["target"])
    oc = jget("/api/shots?storage=%s" % other)["total"]
    s, d, _, dt = post("/api/move", {"storage": st["target"], "name": newest["n"], "to": other})
    check("move to %s" % other, s == 200 and jget("/api/shots?storage=%s" % other)["total"] == oc + 1, "%.2fs" % dt)
    moved = jget("/api/shots?storage=%s" % other)["shots"][0]
    s, d, _, _ = post("/api/delete", {"storage": other, "name": moved["n"]})
    check("delete the moved shot", s == 200 and jget("/api/shots?storage=%s" % other)["total"] == oc)
else:
    s, d, _, _ = post("/api/delete", {"storage": st["target"], "name": newest["n"]})
    check("delete the new shot", s == 200)
s, d, _, _ = post("/api/delete", {"storage": src, "name": "shot_9999_nosuchshot.png"})
check("delete of a missing shot -> 400", s == 400, s)
s, d, _, _ = post("/api/options", {"mask": True})
check("set mask on", s == 200 and jget("/api/status")["mask"] is True)
s, d, _, _ = post("/api/options", {"mask": False})
check("set mask off", s == 200 and jget("/api/status")["mask"] is False)
s, d, _, _ = post("/api/options", {"delay": 3, "interval": 20})
check("set delay and interval", s == 200 and jget("/api/status")["delay"] == 3 and jget("/api/status")["interval"] == 20)
post("/api/options", {"delay": 0, "interval": 10})
orig_name = jget("/api/status")["name"]
for bad_name in ("Upper", "", "-x", "x-", "a" * 25, "sp ace", "dot.ted", 7, None):
    s, d, _, _ = post("/api/options", {"name": bad_name})
    check("badge name %r is refused" % (bad_name,), s == 400 and jget("/api/status")["name"] == orig_name, s)
s, d, _, _ = post("/api/options", {"name": "tildagon-test"})
st2 = jget("/api/status")
check("a good badge name is accepted and shown in the status", s == 200 and st2["name"] == "tildagon-test" and "name_live" in st2, s)
post("/api/options", {"name": orig_name})
check("and it can be put back", jget("/api/status")["name"] == orig_name)

print("== live frame")
s, d, r, dt = req("GET", "/api/frame")
check("frame is a valid 240x240 PNG", s == 200 and png_ok(d), "%d B in %.2fs" % (len(d), dt))
s, d2, _, dt2 = req("GET", "/api/frame?mask=1")
check("masked frame is valid and has black corners", s == 200 and png_ok(d2) and (Image is None or Image.open(io.BytesIO(d2)).convert("RGB").getpixel((0, 0)) == (0, 0, 0)), "%.2fs" % dt2)
times = []
for _ in range(5):
    t = time.time()
    req("GET", "/api/frame")
    times.append(time.time() - t)
check("5 frames in a row (%.2fs each, ~%.1f fps)" % (sum(times) / 5, 5 / sum(times)), sum(times) / 5 < 3)

print("== concurrency and robustness")
out = []


def worker(path):
    try:
        s, d, _, dt = req("GET", path, timeout=30)
        out.append((s, png_ok(d) if s == 200 else None, dt))
    except Exception as e:  # noqa
        out.append(("err", str(e), 0))


ths = [threading.Thread(target=worker, args=("/shot/%s/%s" % (src, first["n"]),)) for _ in range(8)]
t = time.time()
[x.start() for x in ths]
[x.join() for x in ths]
check("8 parallel shot downloads all succeed (%.1fs)" % (time.time() - t), all(o[0] == 200 and o[1] for o in out), out)
out.clear()
ths = [threading.Thread(target=worker, args=("/api/frame",)) for _ in range(3)]
[x.start() for x in ths]
[x.join() for x in ths]
check("3 parallel frames: ok or busy, never an error", all(o[0] in (200, 503) for o in out) and any(o[0] == 200 for o in out), [o[0] for o in out])
t = time.time()
s = socket.create_connection((HOST, PORT), timeout=10)  # connects and says nothing
res = req("GET", "/api/status")[0]
check("a silent client doesn't block others", res == 200)
try:
    s.settimeout(8)
    resp = s.recv(200)
    check("the silent client gets 408 after ~3s", status_of(resp) == 408 and 2 < time.time() - t < 6, "%.1fs" % (time.time() - t))
except socket.timeout:
    check("the silent client gets 408 after ~3s", False, "no answer in 8 s")
s.close()
time.sleep(1)
for _ in range(30):
    req("GET", "/api/status")
for _ in range(100):
    req("GET", "/api/shots?storage=%s&limit=5" % src)
heap1 = jget("/api/status?gc=1")["heap"]
check("no leak over ~250 requests (heap measured after a collection)", heap0 - heap1 < 40000, "%d -> %d, %+d B" % (heap0, heap1, heap1 - heap0))

print("== timings")
for p in ("/api/status", "/api/shots?storage=%s" % src):
    ts = [req("GET", p)[3] for _ in range(5)]
    check("%s: %.0f ms average" % (p.split("?")[0], 1000 * sum(ts) / 5), sum(ts) / 5 < 1.5)

if PIN != "none":
    print("== lockout (leaves the server locked for a minute)")
    req("GET", "/login?k=%s" % PIN, headers={"no-auth": 1})  # a good login clears any earlier wrong tries
    codes = [req("GET", "/login?k=%06d" % (i + 1), headers={"no-auth": 1})[0] for i in range(6)]
    check("five wrong PINs, then 429", codes[:5] == [401] * 5 and codes[5] == 429, codes)
    check("even the right PIN is refused while locked", req("GET", "/login?k=%s" % PIN, headers={"no-auth": 1})[0] == 429)
    check("wrong sign-in codes share that lockout", req("GET", "/q?t=ANYTHING", headers={"no-auth": 1})[0] == 429)

bad = results.count(False)
print("\n%d checks, %d failed" % (len(results), bad))
sys.exit(1 if bad else 0)
