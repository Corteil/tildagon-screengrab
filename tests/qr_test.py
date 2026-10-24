#!/usr/bin/env python3
"""Check qr.py against the qrcode library, module for module, on a CPython host.

    pip install qrcode
    python3 tests/qr_test.py          # add -v to list every check

Each payload is encoded in byte mode at both error correction levels and every
module must match with the same mask. The mask qr.py picks on its own is
compared too (the penalty rules are easy to get subtly wrong, though any mask
gives a valid code). With zxing-cpp and Pillow installed, a rendering of each
code is also decoded.
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import qr  # noqa: E402

try:
    import qrcode
    from qrcode import constants, util
except ImportError:
    sys.exit("pip install qrcode first")
try:
    import zxingcpp
    from PIL import Image
except ImportError:
    zxingcpp = None

bad = 0
total = 0


def check(name, ok, detail=""):
    global bad, total
    total += 1
    if not ok:
        bad += 1
    if not ok or "-v" in sys.argv:
        print(("PASS  " if ok else "FAIL  ") + name + ("  (%s)" % detail if detail != "" else ""))


def reference(text, level, version, mask):
    q = qrcode.QRCode(version=version, error_correction={"L": constants.ERROR_CORRECT_L, "M": constants.ERROR_CORRECT_M}[level],
                      border=0, mask_pattern=mask)
    q.add_data(util.QRData(text.encode(), mode=util.MODE_8BIT_BYTE))
    q.make(fit=False)
    matrix = [bytes(1 if b else 0 for b in row) for row in q.modules]
    return matrix, (mask if mask is not None else q.best_mask_pattern())  # after the matrix: it scribbles on q.modules


def decode(modules, size):
    scale, border = 8, 4
    side = (size + 2 * border) * scale
    img = Image.new("L", (side, side), 255)
    px = img.load()
    for y in range(size):
        for x in range(size):
            if modules[y][x]:
                for dy in range(scale):
                    for dx in range(scale):
                        px[(x + border) * scale + dx, (y + border) * scale + dy] = 0
    out = zxingcpp.read_barcodes(img)
    return out[0].text if out else None


canon = [
    "WIFI:T:WPA;S:Tildagon-1a2b;P:acid-acre-army-bath;;",
    "WIFI:T:WPA;S:Tildagon-1a2b;P:quilt-stray-blink-trend;;",
    "WIFI:T:WPA;S:Tildagon-1a2b;P:K7P3WX9D;;",
    "http://192.168.4.1/q?t=7K2MX9QR",
    "http://192.168.68.92/q?t=7K2MX9QR",
    "http://192.168.4.1:8080/q?t=7K2MX9QR",
    "A", "hello", "0123456789",
]
random.seed(7)
alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-:;/.=?_ "
random_cases = ["".join(random.choice(alphabet) for _ in range(random.randint(1, 120))) for _ in range(150)]

auto_agree = auto_total = 0
for text in canon + random_cases:
    for level in ("L", "M"):
        try:
            v = qr._version_for(level, len(text.encode()))
        except ValueError:
            continue
        size, mine = qr.encode(text, level)
        ref_auto, ref_mask = reference(text, level, v, None)
        auto_total += 1
        auto_agree += [bytes(r) for r in mine] == ref_auto
        _, forced = qr.encode(text, level, mask=ref_mask)  # same mask: every module must agree
        ref_same, _ = reference(text, level, v, ref_mask)
        check("%s v%d mask %d %r" % (level, v, ref_mask, text[:30]), [bytes(r) for r in forced] == ref_same, "differs from qrcode")
        if zxingcpp and (text in canon or random.random() < 0.2):
            check("decodes (own mask): %s v%d %r" % (level, v, text[:30]), decode(mine, size) == text)
print("my own mask choice equals qrcode's on %d of %d codes (either mask gives a valid code)" % (auto_agree, auto_total))

for text in canon:  # best(): M if it doesn't grow the code, else L
    size, m = qr.best(text)
    vL = qr._version_for("L", len(text.encode()))
    check("best() keeps the smallest version for %r" % text[:28], size == vL * 4 + 17, "size %d vs v%d" % (size, vL))

print("%d checks, %d failed" % (total, bad))
sys.exit(1 if bad else 0)
