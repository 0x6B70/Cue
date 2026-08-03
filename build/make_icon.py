"""Generate build/icon.ico (and a PNG preview) for the installer and shortcuts.

Deliberately dependency-free: an icon is a build asset that has to be
regenerable years from now, and pulling in an imaging library for one file
would be the only reason this project needed one. The artwork is drawn once at
1024x1024 and box-downsampled to each icon size, which is where the
anti-aliasing comes from -- no per-pixel coverage maths required.

    python build/make_icon.py

Colours come from frontend/ui/styles.css so the icon matches the UI.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

# --- palette (frontend/ui/styles.css) --------------------------------------
ACCENT = (0x5B, 0x8C, 0xFF)
TEXT = (0xE7, 0xE9, 0xF2)
TOP = (0x26, 0x2B, 0x45)  # slightly lifted so the tile reads on a dark taskbar
BOTTOM = (0x11, 0x12, 0x1B)

RENDER = 1024
SIZES = [16, 24, 32, 48, 64, 128, 256]


def _rounded_rect(x, y, x0, y0, x1, y1, r):
    dx = max(x0 + r - x, 0.0, x - (x1 - r))
    dy = max(y0 + r - y, 0.0, y - (y1 - r))
    return dx * dx + dy * dy <= r * r


def _triangle(x, y, ax, ay, bx, by, cx, cy):
    d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by)
    d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy)
    d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def _over(dst, src, alpha):
    """Composite src over dst at the given alpha."""
    return tuple(round(s * alpha + d * (1 - alpha)) for s, d in zip(src, dst))


def render(size: int = RENDER, simplified: bool = False) -> bytearray:
    """Draw the icon: a queue of bars beside a play triangle.

    `simplified` drops to two chunkier bars for 16/24px, where three bars and
    their gaps collapse into a single smudge.
    """
    buf = bytearray(size * size * 4)
    s = float(size)

    # Tile geometry, in normalised units scaled by `s`.
    m, radius = 0.035 * s, 0.20 * s
    x0, y0, x1, y1 = m, m, s - m, s - m

    if simplified:
        bar_left, bar_right = 0.145 * s, 0.445 * s
        bar_h, bar_r = 0.135 * s, 0.067 * s
        bar_centres = (0.355 * s, 0.645 * s)
        tri = (0.545 * s, 0.225 * s, 0.545 * s, 0.775 * s, 0.895 * s, 0.5 * s)
    else:
        bar_left, bar_right = 0.155 * s, 0.455 * s
        bar_h, bar_r = 0.088 * s, 0.044 * s
        bar_centres = (0.295 * s, 0.5 * s, 0.705 * s)
        # Play triangle, nudged right so its optical centre sits near the middle.
        tri = (0.545 * s, 0.255 * s, 0.545 * s, 0.745 * s, 0.875 * s, 0.5 * s)

    for py in range(size):
        y = py + 0.5
        # Vertical gradient across the tile.
        t = y / s
        base = tuple(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3))
        row = py * size * 4
        for px in range(size):
            x = px + 0.5
            if not _rounded_rect(x, y, x0, y0, x1, y1, radius):
                continue  # stays transparent

            colour = base
            if _triangle(x, y, *tri):
                colour = ACCENT
            else:
                for index, cy in enumerate(bar_centres):
                    if _rounded_rect(
                        x, y, bar_left, cy - bar_h / 2, bar_right, cy + bar_h / 2, bar_r
                    ):
                        # The first bar is the one playing, so it takes the
                        # accent; the rest are queued and sit back.
                        colour = ACCENT if index == 0 else _over(base, TEXT, 0.82)
                        break

            offset = row + px * 4
            buf[offset : offset + 4] = bytes((*colour, 255))
    return buf


def downsample(src: bytearray, src_size: int, dst_size: int) -> bytearray:
    """Box-filter down. This is what produces the anti-aliased edges."""
    if src_size == dst_size:
        return src
    factor = src_size // dst_size
    area = factor * factor
    out = bytearray(dst_size * dst_size * 4)
    for y in range(dst_size):
        for x in range(dst_size):
            r = g = b = a = 0
            for sy in range(y * factor, (y + 1) * factor):
                base = sy * src_size * 4
                for sx in range(x * factor, (x + 1) * factor):
                    o = base + sx * 4
                    alpha = src[o + 3]
                    # Weight colour by alpha so transparent pixels do not drag
                    # the edges toward black.
                    r += src[o] * alpha
                    g += src[o + 1] * alpha
                    b += src[o + 2] * alpha
                    a += alpha
            o = (y * dst_size + x) * 4
            if a:
                out[o : o + 4] = bytes((r // a, g // a, b // a, a // area))
            else:
                out[o : o + 4] = b"\0\0\0\0"
    return out


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def png_bytes(rgba: bytearray, size: int) -> bytes:
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type: none
        raw += rgba[y * stride : (y + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def ico_bytes(images: list[tuple[int, bytes]]) -> bytes:
    """Pack PNG-compressed entries into an .ico (supported since Vista)."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, payload = b"", b""
    for size, png in images:
        entries += struct.pack(
            "<BBBBHHII", size if size < 256 else 0, size if size < 256 else 0,
            0, 0, 1, 32, len(png), offset,
        )
        payload += png
        offset += len(png)
    return header + entries + payload


def main() -> None:
    here = Path(__file__).resolve().parent
    print(f"rendering {RENDER}x{RENDER}...")
    master = render(RENDER)
    small = render(RENDER, simplified=True)

    images = []
    for size in SIZES:
        source = small if size <= 24 else master
        print(f"  downsampling -> {size}px{' (simplified)' if source is small else ''}")
        images.append((size, png_bytes(downsample(source, RENDER, size), size)))

    ico = here / "icon.ico"
    ico.write_bytes(ico_bytes(images))
    print(f"wrote {ico} ({ico.stat().st_size:,} bytes, sizes: {SIZES})")

    # A PNG copy is handy for the Linux target and for eyeballing the result.
    preview = here / "icon.png"
    preview.write_bytes(png_bytes(downsample(master, RENDER, 256), 256))
    print(f"wrote {preview}")


if __name__ == "__main__":
    main()
