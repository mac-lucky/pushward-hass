"""Tests for the vendored ThumbHash encoder and the Home Assistant glue around it.

The expected hashes below are not recordings of this port's own output. They were
produced by running the reference implementation (evanw/thumbhash's `thumbhash.js`)
over the exact same pixel buffers, so they pin the port to the format rather than to
itself. A ThumbHash that only this code agrees with is a ThumbHash iOS renders wrong.

What that pins is these fixtures, not every possible image: Python's `math.cos` and
V8's `Math.cos` disagree by one unit in the last place often enough that a coefficient
sitting on a quantization boundary can come out a nibble apart. Decoders render such a
pair identically, so the format-level agreement these cases assert is the thing worth
holding.
"""

from __future__ import annotations

import base64
import time
from collections import OrderedDict
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.pushward.const import IMAGE_THUMBHASH_MAX
from custom_components.pushward.image_hash import (
    CACHE_MAX,
    DATA_THUMBHASH_CACHE,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    ThumbhashError,
    async_ensure_thumbhash,
    async_thumbhash_for_path,
    async_thumbhash_for_url,
)
from custom_components.pushward.thumbhash import (
    MAX_DIMENSION,
    rgba_to_thumb_hash,
    rgba_to_thumb_hash_base64,
)

from .conftest import (
    expected_thumbhash,
    patch_image_download,
    patch_image_fetch_failure,
    png_bytes,
    png_with_declared_size,
)
from .server_contract import assert_valid_activity_content

# --- deterministic pixel fixtures ------------------------------------------


def gradient_8x6() -> bytes:
    """Opaque RGB gradient. Reference hash: 3gYKnZp4iHiAeHiHiHiId4B1CPeI"""
    return bytes(value for y in range(6) for x in range(8) for value in (x * 32, y * 42, 255 - x * 32, 255))


def alpha_5x5() -> bytes:
    """Alpha checkerboard over a two-axis gradient, which exercises the alpha channel."""
    return bytes(
        value
        for y in range(5)
        for x in range(5)
        for value in (20 + x * 40, 200 - y * 30, 90 + x * 10, 0 if (x + y) % 2 else 255)
    )


REFERENCE_HASHES = {
    "gradient_8x6": (8, 6, gradient_8x6(), "3gYKnZp4iHiAeHiHiHiId4B1CPeI"),
    "alpha_5x5": (5, 5, alpha_5x5(), "XbiFFQoYcHaMiHeYeIN/CCeIqPiIiHqL+A=="),
    "single_pixel": (1, 1, bytes([12, 200, 90, 255]), "WYgwJ1wI9wiIh4hwj3CI+AiIgIAI94cP"),
}


# --- encoder ---------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(REFERENCE_HASHES))
def test_encoder_matches_the_reference_implementation(case: str) -> None:
    width, height, rgba, expected = REFERENCE_HASHES[case]
    assert rgba_to_thumb_hash_base64(width, height, rgba) == expected


def test_hash_is_padded_standard_alphabet_base64_within_the_cap() -> None:
    """The wire form the server and Swift both require, for every fixture."""
    for width, height, rgba, expected in REFERENCE_HASHES.values():
        assert len(expected) <= IMAGE_THUMBHASH_MAX
        assert len(expected) % 4 == 0
        assert set(expected) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
        assert base64.b64decode(expected, validate=True) == rgba_to_thumb_hash(width, height, rgba)


def test_the_alpha_flag_tracks_whether_the_image_is_transparent() -> None:
    """Bit 23 of header24 (the high bit of byte 2) tells the decoder to read an alpha channel."""
    opaque = rgba_to_thumb_hash(5, 5, bytes([200, 30, 90, 255] * 25))
    transparent = rgba_to_thumb_hash(5, 5, alpha_5x5())
    assert opaque[2] & 0x80 == 0
    assert transparent[2] & 0x80 == 0x80
    # The alpha channel costs a byte of header and a block of coefficients.
    assert len(transparent) > len(opaque)


@pytest.mark.parametrize(
    ("width", "height", "rgba"),
    [
        pytest.param(0, 4, b"", id="zero_width"),
        pytest.param(4, 0, b"", id="zero_height"),
        pytest.param(MAX_DIMENSION + 1, 4, bytes(4 * (MAX_DIMENSION + 1) * 4), id="too_wide"),
        pytest.param(4, MAX_DIMENSION + 1, bytes(4 * (MAX_DIMENSION + 1) * 4), id="too_tall"),
        pytest.param(4, 4, bytes(4 * 4 * 4 - 1), id="buffer_too_short"),
    ],
)
def test_encoder_rejects_unusable_input(width: int, height: int, rgba: bytes) -> None:
    with pytest.raises(ValueError):
        rgba_to_thumb_hash(width, height, rgba)


# --- decode + downscale ----------------------------------------------------


def test_decode_downscales_before_encoding() -> None:
    """A source over MAX_DIMENSION still hashes: the encoder would refuse it raw."""
    result = expected_thumbhash(png_bytes(400, 300))
    assert len(result) <= 64
    base64.b64decode(result, validate=True)


def test_decode_rejects_input_that_is_not_an_image() -> None:
    with pytest.raises(ThumbhashError):
        expected_thumbhash(b"this is not a picture")


def test_decode_refuses_a_canvas_larger_than_the_pixel_cap() -> None:
    """A tiny file can declare enormous dimensions, and decoding it is the whole cost.

    12000x7000 fits inside Pillow's own bomb threshold, so nothing else stops it: at
    four bytes per pixel this would allocate roughly 338 MB before producing a 25 byte
    blur. The size comes from the header, so the refusal costs nothing.
    """
    width, height = 12000, 7000
    data = png_with_declared_size(width, height)
    assert len(data) < 1024, "the fixture has to stay small or it is not testing anything"
    assert width * height > MAX_IMAGE_PIXELS

    with pytest.raises(ThumbhashError, match="pixel limit"):
        expected_thumbhash(data)


# --- fetching --------------------------------------------------------------


async def test_decoding_is_offloaded_to_the_executor(hass: HomeAssistant) -> None:
    """Pillow blocks, so a decode running on the event loop stalls all of Home Assistant."""
    with (
        patch_image_download(png_bytes(40, 40)),
        patch.object(hass, "async_add_executor_job", wraps=hass.async_add_executor_job) as offload,
    ):
        await async_thumbhash_for_url(hass, "https://example.com/a.png")

    offloaded = [call.args[0].__name__ for call in offload.call_args_list if call.args]
    assert "_thumbhash_from_bytes" in offloaded


async def test_url_hash_matches_a_direct_encode(hass: HomeAssistant) -> None:
    body = png_bytes(40, 40)
    with patch_image_download(body):
        result = await async_thumbhash_for_url(hass, "https://example.com/a.png")
    assert result == expected_thumbhash(body)


async def test_url_result_is_cached_so_repeated_updates_do_not_refetch(hass: HomeAssistant) -> None:
    """Activities push the same artwork over and over; one download has to cover them."""
    with patch_image_download(png_bytes(20, 20)) as session:
        first = await async_thumbhash_for_url(hass, "https://example.com/a.png")
        second = await async_thumbhash_for_url(hass, "https://example.com/a.png")
    assert first == second
    assert session.get.call_count == 1


async def test_failures_are_cached_too(hass: HomeAssistant) -> None:
    """A broken URL must not be retried on every single push."""
    with patch_image_fetch_failure() as session:
        with pytest.raises(ThumbhashError):
            await async_thumbhash_for_url(hass, "https://example.com/gone.png")
        with pytest.raises(ThumbhashError):
            await async_thumbhash_for_url(hass, "https://example.com/gone.png")
    assert session.get.call_count == 1


async def test_a_cached_failure_expires_so_a_recovered_host_heals_itself(hass: HomeAssistant) -> None:
    """Remembering a failure forever would strand the activity once the image came back."""
    with patch_image_fetch_failure(), pytest.raises(ThumbhashError):
        await async_thumbhash_for_url(hass, "https://example.com/flaky.png")

    body = png_bytes(20, 20)
    with (
        patch_image_download(body),
        patch("custom_components.pushward.image_hash.time.monotonic", return_value=time.monotonic() + 3600),
    ):
        result = await async_thumbhash_for_url(hass, "https://example.com/flaky.png")
    assert result == expected_thumbhash(body)


async def test_use_cache_false_refetches_and_refreshes_the_entry(hass: HomeAssistant) -> None:
    """The explicit action means "try again now", so a stale failure must not stick."""
    with patch_image_fetch_failure(), pytest.raises(ThumbhashError):
        await async_thumbhash_for_url(hass, "https://example.com/a.png")

    body = png_bytes(20, 20)
    with patch_image_download(body):
        result = await async_thumbhash_for_url(hass, "https://example.com/a.png", use_cache=False)
    assert result == expected_thumbhash(body)
    # The retry's success replaced the remembered failure, so the automatic path
    # picks the hash up without a fetch of its own.
    assert hass.data[DATA_THUMBHASH_CACHE]["https://example.com/a.png"][0] == result


async def test_cache_is_bounded(hass: HomeAssistant) -> None:
    """The key is a user-supplied URL, so the map cannot be allowed to grow forever."""
    body = png_bytes(8, 8)
    with patch_image_download(body):
        for index in range(CACHE_MAX + 5):
            await async_thumbhash_for_url(hass, f"https://example.com/{index}.png")
    cache: OrderedDict = hass.data[DATA_THUMBHASH_CACHE]
    assert len(cache) == CACHE_MAX
    # Oldest evicted first.
    assert "https://example.com/0.png" not in cache
    assert f"https://example.com/{CACHE_MAX + 4}.png" in cache


async def test_oversized_download_is_refused_by_declared_length(hass: HomeAssistant) -> None:
    with (
        patch_image_download(b"x" * 10, content_length=MAX_IMAGE_BYTES + 1),
        pytest.raises(ThumbhashError, match="limit"),
    ):
        await async_thumbhash_for_url(hass, "https://example.com/huge.png")


async def test_oversized_download_is_refused_when_the_length_header_lied(hass: HomeAssistant) -> None:
    """content_length is a claim, not a guarantee, so the body is capped as well."""
    with (
        patch_image_download(b"x" * (MAX_IMAGE_BYTES + 1), content_length=10),
        pytest.raises(ThumbhashError, match="larger than"),
    ):
        await async_thumbhash_for_url(hass, "https://example.com/liar.png")


async def test_empty_body_is_refused(hass: HomeAssistant) -> None:
    with patch_image_download(b""), pytest.raises(ThumbhashError, match="empty"):
        await async_thumbhash_for_url(hass, "https://example.com/nothing.png")


# --- local files -----------------------------------------------------------


async def test_path_hash_matches_a_direct_encode(hass: HomeAssistant, tmp_path) -> None:
    body = png_bytes(30, 20)
    target = tmp_path / "cover.png"
    target.write_bytes(body)
    with patch.object(hass.config, "is_allowed_path", return_value=True):
        result = await async_thumbhash_for_path(hass, str(target))
    assert result == expected_thumbhash(body)


async def test_path_outside_the_allowlist_is_refused(hass: HomeAssistant, tmp_path) -> None:
    """hass.config.is_allowed_path is the gate; without it this reads any file on disk."""
    target = tmp_path / "cover.png"
    target.write_bytes(png_bytes(8, 8))
    with (
        patch.object(hass.config, "is_allowed_path", return_value=False),
        pytest.raises(ThumbhashError, match="allowlisted"),
    ):
        await async_thumbhash_for_path(hass, str(target))


async def test_missing_file_is_refused(hass: HomeAssistant, tmp_path) -> None:
    with patch.object(hass.config, "is_allowed_path", return_value=True), pytest.raises(ThumbhashError):
        await async_thumbhash_for_path(hass, str(tmp_path / "nope.png"))


# --- the best-effort content hook ------------------------------------------


async def test_ensure_fills_in_a_missing_hash(hass: HomeAssistant) -> None:
    body = png_bytes(20, 20)
    content = {"template": "generic", "progress": 0.0, "image_url": "https://example.com/a.png"}
    with patch_image_download(body):
        await async_ensure_thumbhash(hass, content)
    assert content["image_thumbhash"] == expected_thumbhash(body)
    assert_valid_activity_content(content)


async def test_ensure_leaves_an_explicit_hash_alone(hass: HomeAssistant) -> None:
    """A caller who supplied a hash has already decided; do not spend a request."""
    content = {
        "template": "generic",
        "image_url": "https://example.com/a.png",
        "image_thumbhash": REFERENCE_HASHES["gradient_8x6"][3],
    }
    with patch_image_download(png_bytes()) as session:
        await async_ensure_thumbhash(hass, content)
    assert content["image_thumbhash"] == REFERENCE_HASHES["gradient_8x6"][3]
    session.get.assert_not_called()


async def test_ensure_is_a_no_op_without_a_url(hass: HomeAssistant) -> None:
    content = {"template": "generic", "progress": 0.0}
    await async_ensure_thumbhash(hass, content)
    assert "image_thumbhash" not in content


async def test_ensure_swallows_failures(hass: HomeAssistant) -> None:
    """Artwork is decoration: an unreachable image must not cost the user the push."""
    content = {"template": "generic", "progress": 0.0, "image_url": "https://example.com/gone.png"}
    with patch_image_fetch_failure():
        await async_ensure_thumbhash(hass, content)
    assert "image_thumbhash" not in content
    assert_valid_activity_content(content)
