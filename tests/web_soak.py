#!/usr/bin/env python3
"""Soak the Screengrab web server: poll the live frame flat out, the status and
the shot list on the side, download shots, and count errors and throughput.

    python3 tests/web_soak.py http://192.168.68.92 <PIN|none> [seconds]

Run it while the badge's app is in the foreground to see how the badge's own
screen and buttons cope. It only reads, so it is safe on a badge with real shots.
"""
import http.client
import json
import sys
import threading
import time

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.68.92"
PIN = sys.argv[2] if len(sys.argv) > 2 else "none"
SECS = float(sys.argv[3]) if len(sys.argv) > 3 else 120
HOST = BASE.split("//")[1].split(":")[0]
PORT = int(BASE.rsplit(":", 1)[1]) if BASE.count(":") > 1 else 80
H = {} if PIN == "none" else {"X-PIN": PIN}
stats = {"frames": 0, "frame_s": 0.0, "status": 0, "list": 0, "shots": 0, "errors": 0, "busy": 0}
lock = threading.Lock()
errs = []
end = time.time() + SECS


def get(path, timeout=20):
    c = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    c.request("GET", path, headers=H)
    r = c.getresponse()
    d = r.read()
    c.close()
    return r.status, d


def note(key, dt=0.0):
    with lock:
        stats[key] += 1
        if key == "frames":
            stats["frame_s"] += dt


def worker(kind):
    first = None
    while time.time() < end:
        t = time.time()
        try:
            if kind == "frame":
                s, d = get("/api/frame")
                note("frames" if s == 200 else ("busy" if s == 503 else "errors"), time.time() - t)
                if s not in (200, 503):
                    errs.append("frame %s" % s)
            elif kind == "status":
                s, d = get("/api/status")
                note("status" if s == 200 else "errors")
                time.sleep(2)
            elif kind == "list":
                s, d = get("/api/shots?limit=20")
                note("list" if s == 200 else "errors")
                if s == 200 and d and json.loads(d)["shots"]:
                    first = json.loads(d)
                    st = first["storage"]
                    for shot in first["shots"][:3]:
                        s2, d2 = get("/shot/%s/%s" % (st, shot["n"]))
                        note("shots" if s2 == 200 and len(d2) == shot["s"] else "errors")
                time.sleep(3)
        except Exception as e:  # noqa
            note("errors")
            errs.append("%s: %r" % (kind, e))
            time.sleep(1)


heap0 = json.loads(get("/api/status?gc=1")[1])["heap"]
ths = [threading.Thread(target=worker, args=(k,)) for k in ("frame", "status", "list")]
t0 = time.time()
[t.start() for t in ths]
[t.join() for t in ths]
dt = time.time() - t0
st = json.loads(get("/api/status?gc=1")[1])
print("%d s: %d live frames (%.2f fps, %.2f s each), %d status, %d lists, %d shot downloads, %d busy, %d errors" % (
    dt, stats["frames"], stats["frames"] / dt, stats["frame_s"] / max(1, stats["frames"]), stats["status"], stats["list"],
    stats["shots"], stats["busy"], stats["errors"]))
print("heap after a collection: %d -> %d (%+d B)" % (heap0, st["heap"], st["heap"] - heap0))
for e in errs[:5]:
    print("  error:", e)
sys.exit(1 if stats["errors"] else 0)
