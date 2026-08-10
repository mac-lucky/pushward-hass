"""ThumbHash encoder, vendored from the reference implementation.

Port of ``rgbaToThumbHash`` from https://github.com/evanw/thumbhash
Copyright (c) 2023 Evan Wallace, MIT licence:

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to
    deal in the Software without restriction, including without limitation the
    rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
    sell copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
    FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
    DEALINGS IN THE SOFTWARE.

Vendored rather than taken from PyPI. Neither published port is a fit: the
``thumbhash`` package has not been released since March 2023, and
``thumbhash-python`` pins ``pillow<11`` plus a ``typer`` CLI, which collides with
the Pillow that Home Assistant itself ships. Keeping the encoder in-tree also
keeps ``manifest.json`` requirements empty, so installing PushWard still pulls
nothing into the user's Home Assistant environment.

A ThumbHash is a ~25 byte approximation of an image: an average colour plus a
handful of DCT coefficients, small enough to ride inline in an activity payload.
That is what makes it worth computing here: the phone renders it without
fetching anything, so an image on a LAN address Home Assistant can reach but iOS
cannot still shows up.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Sequence

# The reference implementation refuses anything larger: the transform below costs
# O(width * height) per coefficient, and the format discards that much detail
# anyway. Callers downscale before encoding.
MAX_DIMENSION = 100


def _round_half_up(value: float) -> int:
    """Round the way JavaScript's ``Math.round`` does: halves go toward +infinity.

    Python's built-in ``round`` is banker's rounding, which would land individual
    coefficients on a different integer and yield a hash no other ThumbHash
    implementation agrees with.
    """
    return math.floor(value + 0.5)


def _encode_channel(
    channel: Sequence[float], nx: int, ny: int, width: int, height: int
) -> tuple[float, list[float], float]:
    """Transform one channel into its DC term, normalized AC terms and their scale.

    The accumulation order (x inner, y outer, one ``channel * fx * fy`` product at a
    time) is the reference's and is kept that way on purpose. Factoring the sum
    differently is mathematically identical but shifts the last bit, and a coefficient
    landing on an exact ``.5`` then quantizes to a different nibble. Cross-checking
    against the reference implementation showed that on smooth images. Only the cosine
    tables are hoisted, which changes nothing: the reference recomputes the same values
    per coefficient.

    Matching the order does not buy bit-for-bit agreement on every input, and this
    port does not claim it. ``math.cos`` and V8's ``Math.cos`` differ by one unit in
    the last place on roughly 3.6% of arguments, so a coefficient sitting on a
    quantization boundary can land a nibble away from what thumbhash.js produces. The
    pinned fixtures in ``tests/test_thumbhash.py`` agree exactly; other images may
    differ in a final nibble, which every decoder renders the same blur.
    """
    cos_x = [[math.cos(math.pi / width * cx * (x + 0.5)) for x in range(width)] for cx in range(nx)]
    cos_y = [[math.cos(math.pi / height * cy * (y + 0.5)) for y in range(height)] for cy in range(ny)]

    area = width * height
    dc = 0.0
    ac: list[float] = []
    scale = 0.0
    for cy in range(ny):
        row_cos = cos_y[cy]
        cx = 0
        while cx * ny < nx * (ny - cy):
            fx = cos_x[cx]
            f = 0.0
            for y in range(height):
                fy = row_cos[y]
                base = y * width
                for x in range(width):
                    f += channel[base + x] * fx[x] * fy
            f /= area
            if cx or cy:
                ac.append(f)
                scale = max(scale, abs(f))
            else:
                dc = f
            cx += 1
    if scale:
        ac = [0.5 + 0.5 / scale * value for value in ac]
    return dc, ac, scale


def rgba_to_thumb_hash(width: int, height: int, rgba: bytes | bytearray) -> bytes:
    """Encode a raw RGBA buffer (row-major, 4 bytes per pixel) as a ThumbHash."""
    if width < 1 or height < 1:
        raise ValueError("image must be at least 1x1")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValueError(f"{width}x{height} does not fit in {MAX_DIMENSION}x{MAX_DIMENSION}")
    if len(rgba) < width * height * 4:
        raise ValueError("rgba buffer is shorter than width * height * 4")

    count = width * height

    # Average colour, weighted by alpha so a mostly-transparent image is not pulled
    # toward whatever happens to sit under its transparent pixels.
    avg_r = avg_g = avg_b = avg_a = 0.0
    for i in range(count):
        j = i * 4
        alpha = rgba[j + 3] / 255
        avg_r += alpha / 255 * rgba[j]
        avg_g += alpha / 255 * rgba[j + 1]
        avg_b += alpha / 255 * rgba[j + 2]
        avg_a += alpha
    if avg_a:
        avg_r /= avg_a
        avg_g /= avg_a
        avg_b /= avg_a

    has_alpha = avg_a < count
    # Alpha needs coefficients of its own, so luminance gives some up to stay inside
    # the byte budget.
    l_limit = 5 if has_alpha else 7
    longest = max(width, height)
    lx = max(1, _round_half_up(l_limit * width / longest))
    ly = max(1, _round_half_up(l_limit * height / longest))

    # RGBA -> LPQA, compositing each pixel over the average colour.
    l_channel: list[float] = []
    p_channel: list[float] = []
    q_channel: list[float] = []
    a_channel: list[float] = []
    for i in range(count):
        j = i * 4
        alpha = rgba[j + 3] / 255
        r = avg_r * (1 - alpha) + alpha / 255 * rgba[j]
        g = avg_g * (1 - alpha) + alpha / 255 * rgba[j + 1]
        b = avg_b * (1 - alpha) + alpha / 255 * rgba[j + 2]
        l_channel.append((r + g + b) / 3)
        p_channel.append((r + g) / 2 - b)
        q_channel.append(r - g)
        a_channel.append(alpha)

    l_dc, l_ac, l_scale = _encode_channel(l_channel, max(3, lx), max(3, ly), width, height)
    p_dc, p_ac, p_scale = _encode_channel(p_channel, 3, 3, width, height)
    q_dc, q_ac, q_scale = _encode_channel(q_channel, 3, 3, width, height)
    a_dc, a_ac, a_scale = 0.0, [], 0.0
    if has_alpha:
        a_dc, a_ac, a_scale = _encode_channel(a_channel, 5, 5, width, height)

    is_landscape = width > height
    header24 = (
        _round_half_up(63 * l_dc)
        | (_round_half_up(31.5 + 31.5 * p_dc) << 6)
        | (_round_half_up(31.5 + 31.5 * q_dc) << 12)
        | (_round_half_up(31 * l_scale) << 18)
        | (int(has_alpha) << 23)
    )
    header16 = (
        (ly if is_landscape else lx)
        | (_round_half_up(63 * p_scale) << 3)
        | (_round_half_up(63 * q_scale) << 9)
        | (int(is_landscape) << 15)
    )
    digest = [
        header24 & 255,
        (header24 >> 8) & 255,
        (header24 >> 16) & 255,
        header16 & 255,
        (header16 >> 8) & 255,
    ]
    if has_alpha:
        digest.append(_round_half_up(15 * a_dc) | (_round_half_up(15 * a_scale) << 4))

    # Every AC term is a nibble; two share a byte, low nibble first.
    ac_start = 6 if has_alpha else 5
    ac_index = 0
    for group in (l_ac, p_ac, q_ac, a_ac) if has_alpha else (l_ac, p_ac, q_ac):
        for value in group:
            index = ac_start + (ac_index >> 1)
            while len(digest) <= index:
                digest.append(0)
            digest[index] |= _round_half_up(15 * value) << ((ac_index & 1) << 2)
            ac_index += 1
    return bytes(digest)


def rgba_to_thumb_hash_base64(width: int, height: int, rgba: bytes | bytearray) -> str:
    """Encode a raw RGBA buffer as the padded standard-alphabet base64 the API wants."""
    return base64.b64encode(rgba_to_thumb_hash(width, height, rgba)).decode("ascii")
