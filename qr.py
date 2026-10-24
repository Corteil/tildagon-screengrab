"""
A small QR code encoder for the badge: byte mode, error correction L or M,
versions 1 to 6 (up to 134 bytes), which is plenty for a Wi-Fi join code or a
short sign-in link. The firmware has none, so this is written out in plain
Python, following the QR standard (ISO/IEC 18004): Reed-Solomon over GF(256),
the zig-zag data placement, the eight masks and the four penalty rules that
pick between them. tests/qr_test.py checks it module for module against the
segno library.

    size, modules = encode("WIFI:T:WPA;S:name;P:pass;;")   # modules[y][x] is 1 for dark
    size, modules = best("http://192.168.4.1/q?t=ABCD")     # the strongest level that stays smallest

License: MIT
"""

# error correction per version: (EC codewords per block, ((blocks, data codewords per block), ...))
_BLOCKS = {
    "L": {1: (7, ((1, 19),)), 2: (10, ((1, 34),)), 3: (15, ((1, 55),)), 4: (20, ((1, 80),)),
          5: (26, ((1, 108),)), 6: (18, ((2, 68),))},
    "M": {1: (10, ((1, 16),)), 2: (16, ((1, 28),)), 3: (26, ((1, 44),)), 4: (18, ((2, 32),)),
          5: (24, ((2, 43),)), 6: (16, ((4, 27),))},
}
_LEVEL_BITS = {"L": 1, "M": 0}
MAX_VERSION = 6

_EXP = [0] * 512  # GF(256) with the QR polynomial 0x11D
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]
del _x, _i


def _gf_mul(a, b):
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def _divisor(degree):
    """The Reed-Solomon generator polynomial, without its leading 1."""
    out = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            out[j] = _gf_mul(out[j], root)
            if j + 1 < degree:
                out[j] ^= out[j + 1]
        root = _gf_mul(root, 2)
    return out


def _remainder(data, divisor):
    out = [0] * len(divisor)
    for b in data:
        factor = b ^ out.pop(0)
        out.append(0)
        for i, coef in enumerate(divisor):
            out[i] ^= _gf_mul(coef, factor)
    return out


def _data_codewords(level, version):
    ec, groups = _BLOCKS[level][version]
    return sum(n * d for n, d in groups)


def capacity(level, version):
    """How many bytes fit: 4 bits of mode and 8 of length come off the front."""
    return (_data_codewords(level, version) * 8 - 12) // 8


def _version_for(level, length):
    for v in range(1, MAX_VERSION + 1):
        if length <= capacity(level, v):
            return v
    raise ValueError("too long for a version 1-%d QR code: %d bytes" % (MAX_VERSION, length))


def _bits(data, version, level):
    out = []

    def put(value, count):
        for i in range(count - 1, -1, -1):
            out.append((value >> i) & 1)

    put(0b0100, 4)  # byte mode
    put(len(data), 8)  # the length takes 8 bits up to version 9
    for b in data:
        put(b, 8)
    total = _data_codewords(level, version) * 8
    put(0, min(4, total - len(out)))  # terminator
    while len(out) % 8:
        out.append(0)
    pad = 0xEC
    while len(out) < total:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    return [int("".join(map(str, out[i:i + 8])), 2) for i in range(0, total, 8)]


def _interleave(codewords, version, level):
    ec_len, groups = _BLOCKS[level][version]
    divisor = _divisor(ec_len)
    blocks, pos = [], 0
    for n, d in groups:
        for _ in range(n):
            chunk = codewords[pos:pos + d]
            pos += d
            blocks.append((chunk, _remainder(chunk, divisor)))
    out = []
    for i in range(max(len(b[0]) for b in blocks)):
        for chunk, _ in blocks:
            if i < len(chunk):
                out.append(chunk[i])
    for i in range(ec_len):
        for _, ec in blocks:
            out.append(ec[i])
    return out


def _alignment_positions(version):
    if version == 1:
        return []
    count = version // 7 + 2
    size = version * 4 + 17
    step = (version * 4 + count * 2 + 1) // (count * 2 - 2) * 2
    return [6] + [size - 7 - (count - 2 - i) * step for i in range(count - 1)]


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _format_bits(level, mask):
    data = _LEVEL_BITS[level] << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


def _add(h, run, size):
    if h[0] == 0:
        run += size  # the light border before the first run counts as part of it
    h.insert(0, run)
    h.pop()


def _count(h):
    """Finder-like 1:1:3:1:1 runs with a light run of 4 or more on a side."""
    n = h[1]
    core = n > 0 and h[2] == n and h[3] == n * 3 and h[4] == n and h[5] == n
    return (1 if core and h[0] >= n * 4 and h[6] >= n else 0) + (1 if core and h[6] >= n * 4 and h[0] >= n else 0)


def _penalty(m, size):
    """The four QR penalty rules; the mask with the lowest total wins."""
    score = 0
    for transpose in (False, True):  # rows, then columns
        for a in range(size):
            color, run, h = 0, 0, [0] * 7
            for b in range(size):
                v = m[b][a] if transpose else m[a][b]
                if v == color:
                    run += 1
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
                else:
                    _add(h, run, size)
                    if not color:
                        score += _count(h) * 40
                    color, run = v, 1
            if color:
                _add(h, run, size)
                run = 0
            run += size  # and the border after the last run
            _add(h, run, size)
            score += _count(h) * 40
    for y in range(size - 1):  # 2x2 blocks of one colour
        for x in range(size - 1):
            v = m[y][x]
            if v == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(r) for r in m)
    total = size * size
    score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
    return score


def encode(text, level="L", version=None, mask=None):
    """Make a QR code for text (str or bytes). Returns (size, modules) with
    modules[y][x] equal to 1 for a dark module. version and mask are chosen
    for you unless given."""
    data = text.encode() if isinstance(text, str) else bytes(text)
    version = version or _version_for(level, len(data))
    if len(data) > capacity(level, version):
        raise ValueError("%d bytes don't fit version %d-%s" % (len(data), version, level))
    size = version * 4 + 17
    words = _interleave(_bits(data, version, level), version, level)

    mod = [bytearray(size) for _ in range(size)]
    fn = [bytearray(size) for _ in range(size)]  # 1 where a function pattern lives

    def setfn(x, y, dark):
        mod[y][x] = 1 if dark else 0
        fn[y][x] = 1

    for i in range(size):  # timing patterns
        setfn(6, i, i % 2 == 0)
        setfn(i, 6, i % 2 == 0)

    def finder(cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setfn(x, y, max(abs(dx), abs(dy)) not in (2, 4))

    finder(3, 3)
    finder(size - 4, 3)
    finder(3, size - 4)
    pos = _alignment_positions(version)
    for i, ay in enumerate(pos):
        for j, ax in enumerate(pos):
            if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setfn(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def put_format(bits):
        for i in range(6):
            setfn(8, i, (bits >> i) & 1)
        setfn(8, 7, (bits >> 6) & 1)
        setfn(8, 8, (bits >> 7) & 1)
        setfn(7, 8, (bits >> 8) & 1)
        for i in range(9, 15):
            setfn(14 - i, 8, (bits >> i) & 1)
        for i in range(8):
            setfn(size - 1 - i, 8, (bits >> i) & 1)
        for i in range(8, 15):
            setfn(8, size - 15 + i, (bits >> i) & 1)
        setfn(8, size - 8, 1)  # the always-dark module

    put_format(0)  # reserve the format areas

    i = 0  # the zig-zag, two columns at a time, from the bottom right
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if (right + 1) & 2 == 0 else vert
                if not fn[y][x] and i < len(words) * 8:
                    mod[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1
                    i += 1
        right -= 2

    def masked(k):
        out = [bytearray(r) for r in mod]
        f = _MASKS[k]
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and f(x, y):
                    out[y][x] ^= 1
        return out

    def finish(k):
        out = masked(k)
        mod_backup = [bytearray(r) for r in mod]
        for y in range(size):
            mod[y][:] = out[y]
        put_format(_format_bits(level, k))
        done = [bytearray(r) for r in mod]
        for y in range(size):
            mod[y][:] = mod_backup[y]
        return done

    if mask is not None:
        return size, finish(mask)
    best_score, best_mask, best_matrix = None, 0, None
    for k in range(8):
        cand = finish(k)
        s = _penalty(cand, size)
        if best_score is None or s < best_score:
            best_score, best_mask, best_matrix = s, k, cand
    return size, best_matrix


def best(text):
    """Error correction M if that doesn't make the code any bigger than L, since
    bigger modules are easier to scan than extra error correction is useful."""
    data = text.encode() if isinstance(text, str) else bytes(text)
    v = _version_for("L", len(data))
    return encode(text, "M" if len(data) <= capacity("M", v) else "L", version=v)


def to_rows(modules):
    """The matrix as a tuple of ints, one per row, bit x set for a dark module."""
    return tuple(sum(b << x for x, b in enumerate(row)) for row in modules)
