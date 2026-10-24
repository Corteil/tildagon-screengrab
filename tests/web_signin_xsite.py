#!/usr/bin/env python3
"""A link opened from a QR scanner or another app is a cross-site navigation, and a browser drops
SameSite=Strict cookies on it, so the sign-in QR landed on the PIN page. This opens the badge's
one-time sign-in link by clicking it on a page from a different site, in real headless Chromium,
and checks that the browser ends up signed in.

    python3 tests/web_signin_xsite.py http://192.168.68.92 <ONE-TIME CODE>

Needs chromium and `pip install websocket-client`. The code is the one the Sign in QR shows
(in the link, after t=), and it is used up by the test.
"""
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import websocket

BADGE, CODE = sys.argv[1].rstrip("/"), sys.argv[2]
PAGE = ('<!doctype html><title>another site</title><a href="%s/q?t=%s">sign in</a>' % (BADGE, CODE)).encode()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


site = http.server.HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=site.serve_forever, daemon=True).start()
other = "http://127.0.0.1:%d/" % site.server_port

proc = subprocess.Popen(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=9336",
                         "--remote-allow-origins=*", "--user-data-dir=" + tempfile.mkdtemp(), "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ok = False
try:
    tab = None
    for _ in range(40):
        try:
            tab = next(t for t in json.load(urllib.request.urlopen("http://127.0.0.1:9336/json")) if t["type"] == "page")
            break
        except Exception:
            time.sleep(0.5)
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30)
    n = 0

    def call(method, **params):
        global n
        n += 1
        ws.send(json.dumps({"id": n, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == n:
                return msg.get("result", {})

    def ev(js):
        return call("Runtime.evaluate", expression=js, returnByValue=True).get("result", {}).get("value")

    call("Page.enable")
    call("Runtime.enable")
    call("Page.navigate", url=other)
    time.sleep(2)
    ev("document.querySelector('a').click()")
    time.sleep(8)
    text = ev("document.body && document.body.innerText") or ""
    ok = "Gallery" in text and "PIN" not in text
    print("%s  opening the sign-in link from another site %s" % (
        "PASS" if ok else "FAIL", "signs in" if ok else "ended on: %r" % text[:80]))
finally:
    proc.terminate()
    time.sleep(1)
    proc.kill()
sys.exit(0 if ok else 1)
