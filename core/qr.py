"""A small QR code encoder (byte mode, error correction level M, versions 1-10), standard library only.

Used to show the two-factor setup link as a QR code that authenticator apps can scan, without
installing anything or sending the secret to an outside service. It follows ISO/IEC 18004 and
the structure of Project Nayuki's public-domain-style reference encoder: data codewords, Reed-
Solomon error correction over GF(256), function patterns, then the lowest-penalty mask.

    svg(text) -> an <svg> string (black modules on white, 4-module quiet zone)
    matrix(text) -> list of rows of bools (True = dark)
"""

# Level M, versions 1..10 (index 0 unused). Up to 213 bytes, plenty for an otpauth:// link.
_ECC_PER_BLOCK = [None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]
_NUM_BLOCKS = [None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
_ECL_BITS_M = 0
MAX_VERSION = 10


class QRError(ValueError):
    pass


def _raw_modules(ver):
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        align = ver // 7 + 2
        n -= (25 * align - 10) * align - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]


# --- Reed-Solomon over GF(2^8), polynomial 0x11D ---
def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


# --- Data and error correction codewords ---
def _codewords(data, ver):
    cap = _data_codewords(ver)
    bits = []
    def put(val, n):
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))
    put(0b0100, 4)                                  # byte mode
    put(len(data), 8 if ver <= 9 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, cap * 8 - len(bits)))             # terminator
    put(0, -len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < cap:
        words.append(pad)
        pad ^= 0xEC ^ 0x11

    nblocks, ecc_len = _NUM_BLOCKS[ver], _ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    num_short = nblocks - raw % nblocks
    short_len = raw // nblocks
    divisor = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nblocks):
        n = short_len - ecc_len + (0 if i < num_short else 1)
        dat = words[k:k + n]
        k += n
        ecc = _rs_remainder(dat, divisor)
        if i < num_short:
            dat = dat + [0]
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= num_short:
                out.append(blk[i])
    return out


# --- Module placement ---
class _Grid:
    def __init__(self, ver):
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.func = [[False] * self.size for _ in range(self.size)]

    def set_func(self, x, y, dark):
        self.mod[y][x] = bool(dark)
        self.func[y][x] = True

    def draw_function_patterns(self):
        s = self.size
        for i in range(s):
            self.set_func(6, i, i % 2 == 0)
            self.set_func(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        dist = max(abs(dx), abs(dy))
                        self.set_func(x, y, dist not in (2, 4))
        pos = self._align_positions()
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i, j) in ((0, 0), (0, n - 1), (n - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_func(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1
                a, b = s - 11 + i % 3, i // 3
                self.set_func(a, b, bit)
                self.set_func(b, a, bit)

    def _align_positions(self):
        if self.ver == 1:
            return []
        n = self.ver // 7 + 2
        step = (self.ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
        result = [6]
        pos = self.size - 7
        while len(result) < n:
            result.insert(1, pos)
            pos -= step
        return result

    def draw_format(self, mask):
        data = _ECL_BITS_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1
        s = self.size
        for i in range(6):
            self.set_func(8, i, bit(i))
        self.set_func(8, 7, bit(6))
        self.set_func(8, 8, bit(7))
        self.set_func(7, 8, bit(8))
        for i in range(9, 15):
            self.set_func(14 - i, 8, bit(i))
        for i in range(8):
            self.set_func(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_func(8, s - 15 + i, bit(i))
        self.set_func(8, s - 8, True)

    def draw_codewords(self, words):
        s, i, total = self.size, 0, len(words) * 8
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = s - 1 - vert if upward else vert
                    if not self.func[y][x] and i < total:
                        self.mod[y][x] = bool((words[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        fn = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.func[y][x] and fn(x, y):
                    self.mod[y][x] = not self.mod[y][x]


_MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]

_FINDER_LIKE = ([1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1])


def _penalty(m):
    """The four penalty rules of the standard (lower is easier to scan)."""
    s = len(m)
    score = 0
    lines = [row for row in m] + [[m[y][x] for y in range(s)] for x in range(s)]
    for line in lines:
        run, prev = 0, None
        for v in line:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run, prev = 1, v
        if run >= 5:
            score += 3 + run - 5
        ints = [int(v) for v in line]
        for i in range(s - 10):
            if ints[i:i + 11] in _FINDER_LIKE:
                score += 40
    for y in range(s - 1):
        for x in range(s - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(row) for row in m)
    total = s * s
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    return score + max(k, 0) * 10


def matrix(text):
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    ver = next((v for v in range(1, MAX_VERSION + 1)
                if 4 + (8 if v <= 9 else 16) + len(data) * 8 <= _data_codewords(v) * 8), None)
    if ver is None:
        raise QRError("Too much text for a QR code here.")
    words = _codewords(data, ver)
    best = None
    for mask in range(8):
        g = _Grid(ver)
        g.draw_function_patterns()
        g.draw_codewords(words)
        g.apply_mask(mask)
        g.draw_format(mask)
        p = _penalty(g.mod)
        if best is None or p < best[0]:
            best = (p, g)
    return best[1].mod


def svg(text, border=4):
    m = matrix(text)
    s = len(m) + border * 2
    path = "".join(f"M{x + border},{y + border}h1v1h-1z"
                   for y, row in enumerate(m) for x, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}" shape-rendering="crispEdges" '
            f'role="img" aria-label="QR code"><rect width="{s}" height="{s}" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')
