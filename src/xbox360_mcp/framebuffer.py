"""Convert the raw Xenos front buffer returned by XBDM `screenshot` into a PNG.

The 360 GPU stores render targets in a tiled layout (32x32 macro tiles).
The XGAddress2DTiled{X,Y} formulas below are the public reverse-engineered
versions used by Xenia and various 360 tools, vectorised with numpy.
"""

from __future__ import annotations

import io

import numpy as np


def _log_bpp(texel_pitch: int) -> int:
    return (texel_pitch >> 2) + ((texel_pitch >> 1) >> (texel_pitch >> 2))


def tiled_xy(offsets: np.ndarray, width: int, texel_pitch: int) -> tuple[np.ndarray, np.ndarray]:
    aligned_width = (width + 31) & ~31
    log_bpp = _log_bpp(texel_pitch)
    off_b = offsets << log_bpp
    off_t = ((off_b & ~4095) >> 3) + ((off_b & 1792) >> 2) + (off_b & 63)
    off_m = off_t >> (7 + log_bpp)

    macro_x = (off_m % (aligned_width >> 5)) << 2
    tile_x = (((off_t >> (5 + log_bpp)) & 2) + (off_b >> 6)) & 3
    macro = (macro_x + tile_x) << 3
    micro = ((((off_t >> 1) & ~15) + (off_t & 15)) & ((texel_pitch << 3) - 1)) >> log_bpp
    x = macro + micro

    macro_y = (off_m // (aligned_width >> 5)) << 2
    tile_y = ((off_t >> (6 + log_bpp)) & 1) + ((off_b & 2048) >> 10)
    macro = (macro_y + tile_y) << 3
    micro = (((off_t & (((texel_pitch << 6) - 1) & ~31)) + ((off_t & 15) << 1)) >> (3 + log_bpp)) & ~1
    y = macro + micro + ((off_t & 16) >> 4)
    return x, y


def untile(raw: bytes, width: int, height: int, pitch_px: int, bpp: int = 4) -> np.ndarray:
    """Return an (aligned_h, aligned_w, bpp) uint8 array, untiled."""
    aligned_w = (max(width, pitch_px) + 31) & ~31
    aligned_h = (height + 31) & ~31
    n = min(len(raw) // bpp, aligned_w * aligned_h)
    src = np.frombuffer(raw[: n * bpp], dtype=np.uint8).reshape(n, bpp)
    offs = np.arange(n, dtype=np.int64)
    x, y = tiled_xy(offs, aligned_w, bpp)
    out = np.zeros((aligned_h, aligned_w, bpp), dtype=np.uint8)
    valid = (x < aligned_w) & (y < aligned_h)
    out[y[valid], x[valid]] = src[valid]
    return out


def to_png(
    header: dict,
    raw: bytes,
    *,
    channel_order: str = "BGRA",
    tiled: bool = True,
    max_width: int | None = 1280,
) -> tuple[bytes, dict]:
    """Decode an XBDM screenshot into PNG bytes.

    channel_order: byte order of each 32-bit pixel in the buffer, BGRA (what a retail/RGH console returns, A in the 4th byte), ARGB, RGBA, ABGR.
    """
    from PIL import Image

    width = int(header.get("width") or header.get("sw") or 1280)
    height = int(header.get("height") or header.get("sh") or 720)
    pitch = int(header.get("pitch") or width * 4)
    pitch_px = pitch // 4
    order = channel_order.upper()
    if sorted(order) != sorted("ARGB"):
        raise ValueError("channel_order must be a permutation of ARGB")

    if tiled:
        img = untile(raw, width, height, pitch_px)
    else:
        rows = len(raw) // pitch
        img = np.frombuffer(raw[: rows * pitch], dtype=np.uint8).reshape(rows, pitch_px, 4)
    img = img[:height, :width]
    rgb = np.stack([img[:, :, order.index(c)] for c in "RGB"], axis=-1)
    pil = Image.fromarray(np.ascontiguousarray(rgb), "RGB")
    if max_width and pil.width > max_width:
        pil = pil.resize((max_width, round(pil.height * max_width / pil.width)))
    buf = io.BytesIO()
    pil.save(buf, format="PNG", optimize=True)
    meta = {"width": width, "height": height, "pitch": pitch, "format": header.get("format"), "channel_order": order, "tiled": tiled}
    return buf.getvalue(), meta
