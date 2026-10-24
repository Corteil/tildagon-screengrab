"""
PNG writer for the Tildagon framebuffer.

The firmware can draw PNGs (ctx.image) but has no deflate compressor, so this
carries a small one. It emits a single deflate block with the fixed Huffman
code and finds only two kinds of match, which suits flat-colour UI screens:

  * the pixel to the left (distance 3), for runs of one colour
  * the pixel above (distance one scanline), for areas that repeat down

Everything per byte runs in @viper. Rows are compressed 8 at a time into one
IDAT chunk, so only a few KB are in memory besides the 115 KB framebuffer.

License: MIT
"""
import array
import binascii
import math
import micropython
import struct

W = H = 240
LINE = 1 + W * 3  # a filter byte, then RGB888
ROWS_PER_CHUNK = 8
OUT_SIZE = ROWS_PER_CHUNK * 900  # worst case is ~9 bits a byte: 811 per row

# Offsets into the state array shared with the viper code.
_BITS, _NBITS, _LINE, _HAS_PREV, _OUT_POS = 0, 1, 2, 3, 4
_LITLEN = 8  # 288 entries: (nbits << 16) | bit-reversed code
_LENGTH = 296  # indexed by match length 3..258: symbol | extra bits << 12 | extra value << 16
_DIST_LEFT = 560  # (nbits << 24) | distance code and extra bits, ready to emit
_DIST_UP = 561
_STATE_SIZE = 562

_LBASE = (3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258)
_LEXTRA = (0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0)
_DBASE = (1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577)
_DEXTRA = (0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13)

_state = None


def _rev(code, n):
    r = 0
    for _ in range(n):
        r = (r << 1) | (code & 1)
        code >>= 1
    return r


def _dist(d):
    i = max(k for k in range(30) if _DBASE[k] <= d)
    return ((5 + _DEXTRA[i]) << 24) | _rev(i, 5) | ((d - _DBASE[i]) << 5)


def _new_state():
    global _state
    if _state is None:
        _state = array.array("I", bytes(4 * _STATE_SIZE))
        for s in range(288):  # RFC 1951 fixed literal/length codes
            if s < 144:
                c, n = 0x30 + s, 8
            elif s < 256:
                c, n = 0x190 + s - 144, 9
            elif s < 280:
                c, n = s - 256, 7
            else:
                c, n = 0xC0 + s - 280, 8
            _state[_LITLEN + s] = (n << 16) | _rev(c, n)
        for length in range(3, 259):
            i = max(k for k in range(29) if _LBASE[k] <= length)
            _state[_LENGTH + length] = (257 + i) | (_LEXTRA[i] << 12) | ((length - _LBASE[i]) << 16)
        _state[_DIST_LEFT] = _dist(3)
        _state[_DIST_UP] = _dist(LINE)
    st = _state
    st[_BITS], st[_NBITS] = 3, 3  # BFINAL = 1, BTYPE = 01 (fixed Huffman)
    st[_LINE], st[_HAS_PREV], st[_OUT_POS] = LINE, 0, 0
    return st


def _inset(y):
    """Pixels at each end of row y that fall outside the display's circle."""
    dy = y + 0.5 - H / 2
    return max(0, math.ceil(W / 2 - 0.5 - math.sqrt((W / 2) ** 2 - dy * dy)))


@micropython.viper
def _fb_row(src: ptr8, row: int, dst: ptr8, inset: int):
    # One row of the big-endian RGB565 framebuffer -> filter byte 0 + RGB888,
    # with the first and last `inset` pixels blacked out (the round mask).
    i = row * 480
    dst[0] = 0
    o = 1
    for x in range(240):
        p = (src[i] << 8) | src[i + 1]
        i += 2
        r = (p >> 11) & 31
        g = (p >> 5) & 63
        b = p & 31
        dst[o] = (r << 3) | (r >> 2)
        dst[o + 1] = (g << 2) | (g >> 4)
        dst[o + 2] = (b << 3) | (b >> 2)
        o += 3
    for x in range(inset):
        o = 1 + x * 3
        dst[o] = 0
        dst[o + 1] = 0
        dst[o + 2] = 0
        o = 1 + (239 - x) * 3
        dst[o] = 0
        dst[o + 1] = 0
        dst[o + 2] = 0


@micropython.viper
def _adler32_update(buf: ptr8, n: int, state: ptr32):
    # Running Adler-32; state[0] is the low sum, state[1] the high. The modulo
    # is deferred to every 2500 bytes, as any longer could overflow the high
    # sum in a signed 32-bit viper int.
    a = int(state[0])
    b = int(state[1])
    i = 0
    while i < n:
        m = n - i
        if m > 2500:
            m = 2500
        for j in range(m):
            a += buf[i + j]
            b += a
        a %= 65521
        b %= 65521
        i += m
    state[0] = a
    state[1] = b


@micropython.viper
def _deflate_row(cur: ptr8, prev: ptr8, out: ptr8, st: ptr32):
    bb = int(st[0])
    bc = int(st[1])
    n = int(st[2])
    has_prev = int(st[3])
    o = int(st[4])
    i = 0
    while i < n:
        best = 0
        dist_at = 560  # state index of the chosen distance
        if i >= 4:  # i - 3 must not reach the filter byte
            k = 0
            while i + k < n and k < 258 and cur[i + k] == cur[i + k - 3]:
                k += 1
            if k > best:
                best = k
        if has_prev:
            k = 0
            while i + k < n and k < 258 and cur[i + k] == prev[i + k]:
                k += 1
            if k > best:
                best = k
                dist_at = 561
        if best >= 3:
            e = int(st[296 + best])
            sym = e & 4095
            nx = (e >> 12) & 15
            ent = int(st[8 + sym])
            bb |= (ent & 65535) << bc  # the length symbol
            bc += ent >> 16
            while bc >= 8:
                out[o] = bb & 255
                o += 1
                bb >>= 8
                bc -= 8
            if nx:
                bb |= (e >> 16) << bc  # its extra bits
                bc += nx
                while bc >= 8:
                    out[o] = bb & 255
                    o += 1
                    bb >>= 8
                    bc -= 8
            d = int(st[dist_at])
            bb |= (d & 0xFFFFFF) << bc  # distance code and extra bits
            bc += d >> 24
            while bc >= 8:
                out[o] = bb & 255
                o += 1
                bb >>= 8
                bc -= 8
            i += best
        else:
            ent = int(st[8 + int(cur[i])])
            bb |= (ent & 65535) << bc
            bc += ent >> 16
            while bc >= 8:
                out[o] = bb & 255
                o += 1
                bb >>= 8
                bc -= 8
            i += 1
    st[0] = bb
    st[1] = bc
    st[4] = o


def write_png(path, fb, mask=False):
    """Write the 240x240 RGB565 (big-endian) framebuffer fb to path as a PNG.
    mask=True blacks out everything outside the display's circle."""
    st = _new_state()
    cur = bytearray(LINE)
    prev = bytearray(LINE)
    out = bytearray(OUT_SIZE)
    adler = array.array("I", (1, 0))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
        f.write(struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr)
        f.write(struct.pack(">I", binascii.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF))
        for y in range(H):
            _fb_row(fb, y, cur, _inset(y) if mask else 0)
            _adler32_update(cur, LINE, adler)
            st[_HAS_PREV] = 1 if y else 0
            _deflate_row(cur, prev, out, st)
            cur, prev = prev, cur
            last = y == H - 1
            if last or (y + 1) % ROWS_PER_CHUNK == 0:
                head = b"\x78\x01" if y < ROWS_PER_CHUNK else b""  # zlib header
                tail = b""
                if last:  # end-of-block (seven 0 bits), flush, then the Adler-32
                    bits, nbits = st[_BITS], st[_NBITS] + 7
                    t = bytearray()
                    while nbits > 0:
                        t.append(bits & 255)
                        bits >>= 8
                        nbits -= 8
                    tail = bytes(t) + struct.pack(">I", (adler[1] << 16) | adler[0])
                body = memoryview(out)[: st[_OUT_POS]]
                f.write(struct.pack(">I", len(head) + len(body) + len(tail)) + b"IDAT")
                crc = binascii.crc32(b"IDAT")
                for part in (head, body, tail):
                    if len(part):
                        f.write(part)
                        crc = binascii.crc32(part, crc)
                f.write(struct.pack(">I", crc & 0xFFFFFFFF))
                st[_OUT_POS] = 0
        f.write(struct.pack(">I", 0) + b"IEND" + struct.pack(">I", binascii.crc32(b"IEND") & 0xFFFFFFFF))
