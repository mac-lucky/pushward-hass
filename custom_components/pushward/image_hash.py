"""Turn an image into the ThumbHash an activity carries inline.

The point of doing this in Home Assistant rather than on the phone: iOS downloads
``image_url`` itself and refuses private hosts, so an image living on the LAN never
loads there. Home Assistant sits inside that network and can read it, and the
resulting ThumbHash rides in the activity payload, so the phone renders it without
reaching anything.

Two entry points, deliberately different about failure:

- ``async_ensure_thumbhash`` fills a gap on the way out to the server and swallows
  everything. Artwork is decoration; an unreachable image must never cost the user
  the activity itself.
- ``async_thumbhash_for_url`` / ``async_thumbhash_for_path`` back the
  ``generate_thumbhash`` action, where the user asked for a hash and wants to be
  told why it did not work.

Decoding never happens on the event loop: Pillow is blocking, so every decode goes
through ``async_add_executor_job``.
"""

from __future__ import annotations

import io
import logging
import time
from collections import OrderedDict
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_IMAGE_THUMBHASH, CONF_IMAGE_URL
from .thumbhash import MAX_DIMENSION, rgba_to_thumb_hash_base64

_LOGGER = logging.getLogger(__name__)

# The hash is a ~25 byte blur, so a large source buys nothing. Anything past this is
# refused rather than downloaded and decoded.
MAX_IMAGE_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 5

# A byte cap is not a memory cap: compressed formats decide how much they expand to,
# and a 257 KiB PNG declaring 12000x7000 costs roughly 338 MB of RGBA to decode. The
# dimensions are read from the header before any pixel is touched, so a canvas past
# this is refused for free. Deliberately a local limit rather than Pillow's global
# ``Image.MAX_IMAGE_PIXELS``, which is shared with the rest of Home Assistant.
MAX_IMAGE_PIXELS = 24_000_000

# Activities re-push the same artwork over and over, so both outcomes are worth
# remembering: without a negative entry a broken URL would be re-fetched on every
# single update. Bounded because the key is a user-supplied URL.
CACHE_MAX = 32
DATA_THUMBHASH_CACHE = "pushward_thumbhash_cache"
# A failure is only remembered for this long: long enough that a broken URL is not
# re-downloaded on every push, short enough that an image host coming back heals
# itself without a Home Assistant restart.
FAILURE_RETRY_SECONDS = 600
# A success expires too. The same URL routinely serves a different picture over time
# (a camera snapshot is rewritten in place), so a hash remembered forever would pin
# the blur to whatever the image happened to be at Home Assistant's last restart.
SUCCESS_TTL_SECONDS = 1800


class ThumbhashError(Exception):
    """An image could not be read or decoded into a ThumbHash."""


def _safe_url(url: str) -> str:
    """The URL with any embedded credentials stripped, for logs and error messages.

    A LAN camera URL routinely carries ``user:password@``, and these strings surface
    in the Home Assistant error toast and the log, where a password must not land.
    """
    try:
        parts = urlsplit(url)
        if "@" not in parts.netloc:
            return url
        return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))
    except ValueError:
        return "<unparsable image url>"


def _cache(hass: HomeAssistant) -> OrderedDict[str, tuple[str | None, float]]:
    """The per-hass URL cache: ``(hash or None, expiry)``, where None means a failure.

    Kept under its own ``hass.data`` key rather than inside ``hass.data[DOMAIN]``,
    which is a map of config entries that other code iterates over blindly.
    """
    return hass.data.setdefault(DATA_THUMBHASH_CACHE, OrderedDict())


def _remember(cache: OrderedDict[str, tuple[str | None, float]], url: str, result: str | None, ttl: float) -> None:
    cache.pop(url, None)
    cache[url] = (result, time.monotonic() + ttl)
    while len(cache) > CACHE_MAX:
        cache.popitem(last=False)


def _cached(cache: OrderedDict[str, tuple[str | None, float]], key: str, label: str) -> str | None:
    """The live cached hash for ``key``, or None when nothing usable is stored.

    Raises when the live entry is a remembered failure. ``label`` is what the
    message may name: a cache key is a URL or an ``entity_picture`` path, and both
    can carry a credential that must not reach the log.
    """
    entry = cache.get(key)
    if entry is None:
        return None
    result, expires_at = entry
    if time.monotonic() >= expires_at:
        del cache[key]
        return None
    cache.move_to_end(key)
    if result is None:
        raise ThumbhashError(f"a recent attempt to hash {label} failed; not retrying yet")
    return result


def cached_thumbhash(hass: HomeAssistant, key: str, label: str) -> str | None:
    """The live cached hash for ``key``, computing and fetching nothing.

    None means nothing usable is stored. Raises like ``_cached`` when a recent
    attempt failed, so a caller can skip re-reading the source bytes entirely
    instead of retrying a hash that just failed.
    """
    return _cached(_cache(hass), key, label)


def clear_thumbhash_cache(hass: HomeAssistant) -> None:
    """Forget every remembered hash.

    Called when a config entry unloads so reloading the integration re-reads the
    images, the way reloading re-reads every other piece of cached state. The cache
    is keyed by URL alone rather than per entry, so this clears it for all of them -
    acceptable for a cache whose entries expire on their own anyway.
    """
    hass.data.pop(DATA_THUMBHASH_CACHE, None)


def _thumbhash_from_bytes(data: bytes) -> str:
    """Decode, downscale and encode. Blocking - callers hand this to the executor."""
    try:
        # Imported here, not at module scope: it keeps Pillow off the integration's
        # import path and out of the event loop, and it is only needed when an image
        # is actually hashed. Home Assistant depends on Pillow itself, so nothing is
        # added to manifest.json requirements for this. Inside the try because a
        # missing or broken Pillow is a decode failure like any other, not a crash on
        # the push path.
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as opened:
            # Image.open parses the header only, so the declared size is known before
            # a single pixel is allocated - the one moment a decode bomb is still
            # cheap to refuse.
            width, height = opened.size
            if width * height > MAX_IMAGE_PIXELS:
                raise ThumbhashError(f"image is {width}x{height}, over the {MAX_IMAGE_PIXELS} pixel limit")
            # JPEG can downscale inside the DCT pass, which is far cheaper than
            # decoding full-res and shrinking afterwards. It has to run before
            # exif_transpose: that loads the image and freezes the decode size, which
            # is exactly what draft() is trying to choose. A no-op on other formats.
            opened.draft("RGB", (MAX_DIMENSION, MAX_DIMENSION))
            # A phone-shot photo carries its rotation in EXIF; without this the
            # placeholder comes out sideways relative to the real image.
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
            rgba = image.convert("RGBA")
    except ThumbhashError:
        raise
    except Exception as err:
        # Pillow raises a wide family on malformed input (UnidentifiedImageError,
        # OSError, ValueError, DecompressionBombError, MemoryError). A decoder
        # surprise must surface as a ThumbhashError, never as a crash on the push
        # path that called us.
        raise ThumbhashError(f"could not decode the image: {err}") from err

    width, height = rgba.size
    if not width or not height:
        raise ThumbhashError("image has no pixels")
    return rgba_to_thumb_hash_base64(width, height, rgba.tobytes())


def _read_file(path: str) -> bytes:
    """Read a local image with the same size cap as a download. Blocking."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError as err:
        raise ThumbhashError(f"could not read {path}: {err}") from err
    if len(data) > MAX_IMAGE_BYTES:
        raise ThumbhashError(f"{path} is larger than {MAX_IMAGE_BYTES} bytes")
    if not data:
        raise ThumbhashError(f"{path} is empty")
    return data


def _thumbhash_from_file(path: str) -> str:
    """Read + decode + encode in one executor hop."""
    return _thumbhash_from_bytes(_read_file(path))


async def _async_download(hass: HomeAssistant, url: str) -> bytes:
    """Fetch an image, bounded by FETCH_TIMEOUT and MAX_IMAGE_BYTES."""
    session = async_get_clientsession(hass)
    # Every message below names the credential-free form: these reach the user as a
    # Home Assistant error toast and are written to the log.
    shown = _safe_url(url)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT)) as response:
            response.raise_for_status()
            declared = response.content_length
            if declared is not None and declared > MAX_IMAGE_BYTES:
                raise ThumbhashError(f"{shown} declares {declared} bytes, over the {MAX_IMAGE_BYTES} byte limit")
            # One byte past the cap is enough to tell "at the limit" from "over it"
            # without buffering a response that lies about its length.
            data = await response.content.read(MAX_IMAGE_BYTES + 1)
    except (aiohttp.ClientError, TimeoutError, OSError) as err:
        raise ThumbhashError(f"could not fetch {shown}: {err}") from err
    if len(data) > MAX_IMAGE_BYTES:
        raise ThumbhashError(f"{shown} is larger than {MAX_IMAGE_BYTES} bytes")
    if not data:
        raise ThumbhashError(f"{shown} returned an empty body")
    return data


async def async_thumbhash_for_url(hass: HomeAssistant, url: str, *, use_cache: bool = True) -> str:
    """ThumbHash an image URL, raising ``ThumbhashError`` when it cannot be done.

    Results are cached per URL, both outcomes and both with an expiry: a hash for
    SUCCESS_TTL_SECONDS because the same URL routinely serves a new picture later, a
    failure for FAILURE_RETRY_SECONDS so a broken URL costs one request rather than
    one per push while still healing on its own once the host is back.

    ``use_cache=False`` skips the lookup but still records the outcome: the
    ``generate_thumbhash`` action means "try this now", so a user who has just fixed
    a camera is not made to wait out the cooldown.
    """
    cache = _cache(hass)
    if use_cache and (hit := _cached(cache, url, _safe_url(url))) is not None:
        return hit
    try:
        data = await _async_download(hass, url)
        result = await hass.async_add_executor_job(_thumbhash_from_bytes, data)
    except ThumbhashError:
        _remember(cache, url, None, FAILURE_RETRY_SECONDS)
        raise
    _remember(cache, url, result, SUCCESS_TTL_SECONDS)
    return result


async def async_thumbhash_for_bytes(hass: HomeAssistant, data: bytes, cache_key: str, label: str) -> str:
    """ThumbHash image bytes already in hand, remembered under ``cache_key``.

    For artwork Home Assistant hands over directly (a media_player's cover art)
    there is nothing to download, but decoding is the expensive half and the same
    track is re-pushed on every playhead update - so the result shares the cache
    with the URL path. The key spaces overlap only when ``entity_picture`` is
    itself an absolute URL, and then both names point at the same picture, so a
    shared entry is correct rather than a collision.
    """
    cache = _cache(hass)
    if (hit := _cached(cache, cache_key, label)) is not None:
        return hit
    if len(data) > MAX_IMAGE_BYTES:
        _remember(cache, cache_key, None, FAILURE_RETRY_SECONDS)
        raise ThumbhashError(f"{label} is larger than {MAX_IMAGE_BYTES} bytes")
    try:
        result = await hass.async_add_executor_job(_thumbhash_from_bytes, data)
    except ThumbhashError:
        _remember(cache, cache_key, None, FAILURE_RETRY_SECONDS)
        raise
    _remember(cache, cache_key, result, SUCCESS_TTL_SECONDS)
    return result


async def async_thumbhash_for_path(hass: HomeAssistant, path: str) -> str:
    """ThumbHash a local image file.

    Deliberately uncached: a local path is usually a camera snapshot that is
    rewritten in place, so a remembered hash would go stale silently.
    """
    if not hass.config.is_allowed_path(path):
        raise ThumbhashError(f"{path} is not in an allowlisted directory (see allowlist_external_dirs)")
    return await hass.async_add_executor_job(_thumbhash_from_file, path)


async def async_ensure_thumbhash(hass: HomeAssistant, content: dict) -> None:
    """Fill in a missing ``image_thumbhash`` from ``image_url``, in place.

    Best effort by design: on any failure the activity goes out with the URL alone,
    which still renders on every phone that can reach it. Callers must run this
    *before* any content-equality check, or the pushed frame and the next mapped one
    would never compare equal and the update throttle would stop deduplicating.
    """
    url = content.get(CONF_IMAGE_URL)
    if not url or content.get(CONF_IMAGE_THUMBHASH):
        return
    try:
        content[CONF_IMAGE_THUMBHASH] = await async_thumbhash_for_url(hass, url)
    except ThumbhashError as err:
        _LOGGER.debug("Sending %s without a thumbhash: %s", _safe_url(url), err)
