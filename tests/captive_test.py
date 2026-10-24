#!/usr/bin/env python3
"""Tests for the hotspot's captive-portal behaviour, against a server started with captive mode on
(the badge's own hotspot does this; a test setup can bind the same server to the home-Wi-Fi address).

    python3 tests/captive_test.py <badge ip> [none] [portal|quiet]

portal (the hotspot's default): the checks are redirected to the badge's page, so the phone shows
its sign-in screen. quiet: they are answered "online" (204, Apple's Success page, ...).

Checks the DNS responder (raw UDP queries on port 53), the answers to the connectivity checks that
Android, Apple, Windows, Firefox and Linux make when they join a network, the redirect of any other
name to the badge, and that none of this counts as use (so it can't keep the server awake).
Standard library only.
"""
import http.client
import json
import socket
import struct
import sys
import time

IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.68.92"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + (("  (%s)" % detail) if detail != "" else ""))


def query(name, qtype=1, qid=0x4242):
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + labels + struct.pack(">HH", qtype, 1)


def ask(packet, wait=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(wait)
    s.sendto(packet, (IP, 53))
    try:
        return s.recvfrom(512)[0]
    except socket.timeout:
        return None
    finally:
        s.close()


def http_get(path, host, method="GET", body=None):
    c = http.client.HTTPConnection(IP, 80, timeout=10)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", host)
    if body is not None:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    data = r.read()
    out = (r.status, data, r)
    c.close()
    return out


def requests_so_far():
    c = http.client.HTTPConnection(IP, 80, timeout=10)
    c.request("GET", "/api/status", headers={"X-PIN": PIN} if PIN else {})
    r = c.getresponse()
    d = r.read()
    c.close()
    return json.loads(d)["requests"] if r.status == 200 else None


PIN = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] not in ("none", "portal", "quiet") else ""
PORTAL = "portal" in sys.argv[2:]
ipb = bytes(int(x) for x in IP.split("."))

print("== DNS: every name goes to the badge")
for name in ("captive.apple.com", "connectivitycheck.gstatic.com", "www.msftconnecttest.com", "example.com", "a.very.long.sub.domain.name.example.org"):
    r = ask(query(name))
    ok = bool(r) and r[:2] == b"\x42\x42" and r[2] & 0x80 and (r[3] & 15) == 0 and r[6:8] == b"\x00\x01" and r[-4:] == ipb
    check("A %s -> %s" % (name, IP), ok, r and list(r[-4:]))
r = ask(query("captive.apple.com"))
check("TTL is short (60 s) so a changed address is picked up", r and r[-10:-6] == b"\x00\x00\x00\x3c", r and r[-10:-6].hex())
for qt, label in ((28, "AAAA"), (16, "TXT"), (15, "MX"), (65, "HTTPS")):
    r = ask(query("example.com", qt))
    check("%s -> an empty answer, not a wait" % label, r and (r[3] & 15) == 0 and r[6:8] == b"\x00\x00", r and r[2:8].hex())
check("a packet that is too short gets no reply", ask(b"\x00\x01\x02", wait=0.8) is None)
check("a DNS reply sent to the port is ignored", ask(b"\x12\x34\x81\x80" + b"\x00\x01" + b"\x00" * 30, wait=0.8) is None)
r = ask(query("still.works.example"))
check("and the responder is still fine afterwards", r and r[-4:] == ipb)


def burst(count, gap):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.5)
    for i in range(count):
        s.sendto(query("burst%d.example.com" % i, qid=i), (IP, 53))
        if gap:
            time.sleep(gap)
    got = set()
    try:
        while len(got) < count:
            got.add(struct.unpack(">H", s.recvfrom(512)[0][:2])[0])
    except socket.timeout:
        pass
    s.close()
    return len(got)


# The network stack queues only about 6 UDP packets, and a phone joining a network sends a few
# queries (an A and an AAAA for each of a few names), mostly one after another.
n = burst(6, 0)
check("6 queries sent at once (what the network stack can queue) are all answered", n == 6, "%d of 6" % n)
n = burst(60, 0.01)
check("60 queries 10 ms apart: nearly all answered", n >= 50, "%d of 60" % n)

print("== the phones' connectivity checks")
before = requests_so_far()
probes = [
    ("/generate_204", "connectivitycheck.gstatic.com", 204, b""),
    ("/gen_204", "www.google.com", 204, b""),
    ("/hotspot-detect.html", "captive.apple.com", 200, b"Success"),
    ("/library/test/success.html", "captive.apple.com", 200, b"Success"),
    ("/connecttest.txt", "www.msftconnecttest.com", 200, b"Microsoft Connect Test"),
    ("/ncsi.txt", "www.msftncsi.com", 200, b"Microsoft NCSI"),
    ("/success.txt", "detectportal.firefox.com", 200, b"success\n"),
    ("/check_network_status.txt", "nmcheck.gnome.org", 200, b"NetworkManager is online\n"),
]
for path, host, status, body in probes:
    s_, d, r = http_get(path, host)
    if PORTAL:
        check("%s (Host: %s) -> redirect to the page" % (path, host), s_ == 302 and r.getheader("Location") == "http://%s/" % IP and d == b"", s_)
    else:
        check("%s (Host: %s) -> %d" % (path, host, status), s_ == status and body in d and r.getheader("Content-Length") == str(len(d)), "%d %r" % (s_, d[:40]))
s_, d, r = http_get("/generate_204", host=IP)
check("the same check on the badge's own address is handled the same way", s_ == (302 if PORTAL else 204), s_)

print("== any other name is sent to the badge")
for path in ("/", "/whatever", "/api/status", "/shot/flash/x.png"):
    s_, d, r = http_get(path, "example.com")
    check("GET %s with Host: example.com -> redirect to http://%s/" % (path, IP), s_ == 302 and r.getheader("Location") == "http://%s/" % IP and d == b"", s_)
s_, d, r = http_get("/api/capture", "example.com", method="POST", body=b"{}")
check("a POST to the wrong name is refused (421)", s_ == 421, s_)
s_, d, r = http_get("/", "%s:80" % IP)
check("the badge's address with a port is the right name", s_ in (200, 401), s_)
s_, d, r = http_get("/api/status", IP)
check("and a normal request on it is untouched (needs the PIN or works)", s_ in (200, 401), s_)

print("== none of that counts as use")
after = requests_so_far()
check("the request counter didn't move for probes and redirects", before is not None and after is not None and after - before <= 3,
      "%s -> %s (a few of my own status calls are counted)" % (before, after))

bad = results.count(False)
print("\n%d checks, %d failed" % (len(results), bad))
sys.exit(1 if bad else 0)
