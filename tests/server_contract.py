"""PushWard REST API contract assertions for tests.

This module encodes the **public** PushWard API validation contract — the same
constraints advertised by the public OpenAPI spec and already mirrored by the
caps in ``custom_components/pushward/const.py``. It exists so realistic tests can
assert that whatever the integration's mappers emit would be *accepted* by the
PushWard server, not merely that it has the shape we expected locally.

It is a faithful re-statement of documented limits (template-required fields,
length caps, colour/slug/tap-action rules), built from the public constants the
integration already ships. It contains no server implementation — only the
public contract that the integration is required to honour. Caps that exist in
``const.py`` are imported from there (single source of truth); the few public
limits ``const.py`` does not yet name are defined once at the top of this module.

Usage::

    from .server_contract import assert_valid_activity_content, assert_valid_widget_content

    content = map_content(state, config)
    assert_valid_activity_content(content)
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import time
from datetime import datetime, timedelta
from typing import NoReturn
from urllib.parse import urlparse

from custom_components.pushward.const import (
    ACTIVITY_UNIT_MAX,
    BOARD_MAX_TILES,
    BOARD_TILE_ICON_MAX,
    BOARD_TILE_LABEL_MAX,
    BOARD_TILE_UNIT_MAX,
    BOARD_TILE_VALUE_MAX,
    BOARD_TRENDS,
    IMAGE_SHAPES,
    IMAGE_THUMBHASH_MAX,
    IMAGE_URL_MAX,
    LOG_LEVELS,
    LOG_LINE_TEXT_MAX,
    LOG_MAX_LINES,
    MAX_LONG_TEXT_LEN,
    MAX_SEVERITY_LABEL_LEN,
    MAX_TAP_ACTION_ICON_LEN,
    MAX_TAP_ACTION_TITLE_LEN,
    MAX_TEXT_LEN,
    MAX_URL_LEN,
    MEDIA_CONTROL_SLOTS,
    MEDIA_DURATION_MAX,
    MEDIA_EXTRA_CONTROLS_MAX,
    MEDIA_POSITION_MAX_AGE,
    MEDIA_TITLE_MAX,
    PLAYBACK_STATES,
    PRIORITY_MAX,
    PRIORITY_MIN,
    SCALES,
    SCHEDULE_LEVELS,
    SERVICE_TEMPLATES,
    SEVERITIES,
    SNOOZE_SECONDS_MAX,
    SNOOZE_SECONDS_MIN,
    SOUNDS,
    STEP_LABEL_MAX,
    STEP_ROW_MAX,
    STEP_ROW_MIN,
    THRESHOLD_LABEL_MAX,
    THRESHOLDS_MAX,
    TIMELINE_MAX_SERIES,
    TIMELINE_SERIES_LABEL_MAX,
    TIMER_STYLES,
    TOTAL_STEPS_MAX,
    WARNING_THRESHOLD_MAX,
    WIDGET_DATE_FLOOR_TS,
    WIDGET_DATE_HORIZON_DAYS,
    WIDGET_DEVICE_SORT_DIRECTIONS,
    WIDGET_DEVICE_SORT_FIELDS,
    WIDGET_EXPIRED_TEXT_MAX,
    WIDGET_LABEL_MAX,
    WIDGET_MAX_BATTERY_DEVICES,
    WIDGET_MAX_DEVICE_SORT_KEYS,
    WIDGET_MAX_FLOW_INPUTS,
    WIDGET_MAX_SCHEDULE_PERIODS,
    WIDGET_MAX_STAT_ROWS,
    WIDGET_MAX_TREND_POINTS,
    WIDGET_MIN_TREND_POINTS,
    WIDGET_NODE_ICON_MAX,
    WIDGET_NODE_NAME_MAX,
    WIDGET_SEVERITIES,
    WIDGET_STAT_LABEL_MAX,
    WIDGET_STAT_UNIT_MAX,
    WIDGET_STAT_VALUE_MAX,
    WIDGET_SUBTITLE_MAX,
    WIDGET_TEMPLATES,
    WIDGET_TREND_DOWN,
    WIDGET_TREND_FLAT,
    WIDGET_TREND_UP,
    WIDGET_UNIT_MAX,
)
from custom_components.pushward.content_mapper import _COLOR_HEX_RE, _COLOR_NAMED

# --- Public caps that const.py does not (yet) name --------------------------------
# Real server limits the integration honours but has no named constant for; defined
# once here so the contract validator can't drift. Do NOT alias these onto unrelated
# const.py symbols that merely share a value (that would couple distinct fields).
ICON_MAX = 128
TIMELINE_DECIMALS_MIN = 0
TIMELINE_DECIMALS_MAX = 10
# Promoted to const.py (single source of truth); aliased here for the assertions
# below, which read as the server-contract names.
TIMELINE_VALUE_KEY_MAX = TIMELINE_SERIES_LABEL_MAX
MAX_TIMELINE_SERIES = TIMELINE_MAX_SERIES
# Countdowns may be scheduled up to ~5 years out; the extra 30 h is slack for leap
# days / timezone offsets so a legitimately-far countdown isn't wrongly rejected.
# "Historical" timestamps (fired_at / history points) may drift at most a few
# minutes into the future to tolerate clock skew.
MAX_FUTURE_OFFSET = 5 * 365 * 24 * 3600 + 30 * 3600
MAX_CLOCK_SKEW = 5 * 60

# Restated rather than imported from const.py: this module deliberately re-states
# the public REST contract from the outside, not the integration's own view of it.
# The caps these two gates enforce are imported above, so a limit still has exactly
# one definition; only the template allowlists are written out again.
LIVE_PROGRESS_TEMPLATES = ("generic", "steps")
IMAGE_TEMPLATES = ("generic", "steps", "media")
# The media-only content fields; the server 422s them on every other template.
MEDIA_FIELDS = (
    "media_title",
    "playback_state",
    "position_seconds",
    "duration_seconds",
    "position_at",
    "volume",
    "favorite",
    "controls",
)

# Padded standard-alphabet base64 - the only form Swift's Data(base64Encoded:) reads.
_THUMBHASH_B64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")

# Written out here rather than imported so the contract still says what it says even
# if const.py loosens: a URL carrying whitespace or a control byte, or a host outside
# dot-separated letters/digits/hyphens, is refused by the server whatever urlparse
# makes of it.
_URL_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f]")
_URL_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_URL_HOST_RE = re.compile(rf"^{_URL_HOST_LABEL}(?:\.{_URL_HOST_LABEL})*\.?$")

_HTTP_SCHEMES = ("http", "https")
_TRENDS = ("", WIDGET_TREND_UP, WIDGET_TREND_DOWN, WIDGET_TREND_FLAT)


class PushWardContractError(AssertionError):
    """Raised when a content payload violates the public PushWard API contract."""


def _fail(where: str, msg: str) -> NoReturn:
    prefix = f"[{where}] " if where else ""
    raise PushWardContractError(f"{prefix}{msg}")


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_finite_number(v: object) -> bool:
    return _is_number(v) and math.isfinite(float(v))


def _is_int(v: object) -> bool:
    # bool is an int subclass, but the server types these fields as integers and
    # rejects a JSON boolean — so a bool is not a valid int here.
    return isinstance(v, int) and not isinstance(v, bool)


def _check_color(value: object, field: str, where: str) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str):
        _fail(where, f"{field} colour must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if _COLOR_HEX_RE.match(stripped) or stripped.lower() in _COLOR_NAMED:
        return
    _fail(where, f"{field} colour {value!r} is not a named colour or #RRGGBB/#RRGGBBAA hex")


def _check_len(value: object, limit: int, field: str, where: str) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str):
        _fail(where, f"{field} must be a string, got {type(value).__name__}")
    if len(value) > limit:
        _fail(where, f"{field} must be at most {limit} chars, got {len(value)}")


def _check_tap_action(action: object, field: str, where: str) -> None:
    if action is None:
        return
    if not isinstance(action, dict):
        _fail(where, f"{field} must be an object, got {type(action).__name__}")
    url = action.get("url", "")
    if not url:
        _fail(where, f"{field}.url is required when {field} is present")
    _check_len(url, MAX_URL_LEN, f"{field}.url", where)
    _check_len(action.get("title", ""), MAX_TAP_ACTION_TITLE_LEN, f"{field}.title", where)
    _check_len(action.get("icon", ""), MAX_TAP_ACTION_ICON_LEN, f"{field}.icon", where)
    # method/headers/body are HTTP-only — they may only accompany an http(s) URL.
    if any(k in action for k in ("method", "headers", "body")):
        scheme = urlparse(str(url)).scheme.lower()
        if scheme not in _HTTP_SCHEMES:
            _fail(where, f"{field} sets method/headers/body but url scheme {scheme!r} is not http(s)")


def assert_valid_activity_content(content: dict, *, where: str = "activity") -> None:
    """Assert ``content`` satisfies the public Live-Activity content contract.

    Raises ``PushWardContractError`` describing the first violation. Mirrors the
    template-specific required fields, length caps, colour rules and tap-action
    rules the server enforces — so a passing assertion means the payload would be
    accepted by ``POST/PATCH /activities``.
    """
    if not isinstance(content, dict):
        _fail(where, f"content must be a dict, got {type(content).__name__}")

    template = content.get("template")
    if template not in SERVICE_TEMPLATES:
        _fail(where, f"template {template!r} is not one of {SERVICE_TEMPLATES}")

    progress = content.get("progress", 0.0)
    if not _is_finite_number(progress) or not (0.0 <= float(progress) <= 1.0):
        _fail(where, f"progress must be a finite number in [0.0, 1.0], got {progress!r}")

    rt = content.get("remaining_time")
    if rt is not None and (not _is_int(rt) or rt < 0):
        _fail(where, f"remaining_time must be a non-negative int, got {rt!r}")

    _check_len(content.get("state"), MAX_TEXT_LEN, "state", where)
    _check_len(content.get("subtitle"), MAX_TEXT_LEN, "subtitle", where)
    _check_len(content.get("completion_message"), MAX_LONG_TEXT_LEN, "completion_message", where)
    _check_len(content.get("icon"), ICON_MAX, "icon", where)

    for field in ("accent_color", "background_color", "text_color"):
        _check_color(content.get(field), field, where)

    for field in ("tap_action", "url_action", "secondary_url_action"):
        _check_tap_action(content.get(field), field, where)

    # Mirrors the server's Content.Validate() allowlist. Checked before the
    # per-template dispatch, since the per-template asserts only run for the two
    # templates that accept it -- a mapper leaking live_progress onto alert or
    # countdown would otherwise pass here and 422 in production.
    if content.get("live_progress") and template not in LIVE_PROGRESS_TEMPLATES:
        _fail(where, f"live_progress is only supported by {sorted(LIVE_PROGRESS_TEMPLATES)}, got {template!r}")

    # Same shape of rule for the image trio, and checked here for the same reason:
    # the per-template asserts below only run for the two templates that accept it,
    # so a mapper leaking image_url onto alert would pass and then 422 in production.
    _assert_image(content, where, template)
    # And again for the media fields: media_title on a generic card is a 422, not a no-op.
    _assert_media(content, where, template)

    if template == "generic":
        _assert_generic(content, where)
    elif template == "countdown":
        _assert_countdown(content, where)
    elif template == "steps":
        _assert_steps(content, where)
    elif template == "alert":
        _assert_alert(content, where)
    elif template == "gauge":
        _assert_gauge(content, where)
    elif template == "timeline":
        _assert_timeline(content, where)
    elif template == "board":
        _assert_board(content, where)
    elif template == "log":
        _assert_log(content, where)


def _assert_image(content: dict, where: str, template: str) -> None:
    """The optional image trio: generic/steps only, https URL, enum shape, base64 hash.

    The server rejects these fields outright on a template with no image slot rather
    than dropping them, so presence alone is a contract violation elsewhere.
    """
    url = content.get("image_url")
    shape = content.get("image_shape")
    thumbhash = content.get("image_thumbhash")
    if all(value in (None, "") for value in (url, shape, thumbhash)):
        return
    if template not in IMAGE_TEMPLATES:
        _fail(where, f"image fields are only supported by {sorted(IMAGE_TEMPLATES)}, got {template!r}")

    if url not in (None, ""):
        _check_len(url, IMAGE_URL_MAX, "image_url", where)
        if _URL_FORBIDDEN_RE.search(str(url)):
            _fail(where, f"image_url must not contain whitespace or control characters, got {url!r}")
        try:
            parsed = urlparse(str(url))
        except ValueError:
            # urlparse raises on a bracketed non-IP host ("https://[camera]/a.jpg").
            # The contract has to fail on it, not error out of the assertion.
            _fail(where, f"image_url could not be parsed as a URL, got {url!r}")
        if parsed.scheme != "https" or not parsed.netloc:
            _fail(where, f"image_url must be an https URL with a host, got {url!r}")
        # The device fetches this and never re-validates it, so credentials in the
        # URL would ride along to every phone the activity is shared with. Checked on
        # the netloc: an empty userinfo ("https://@host/") leaves username == "".
        if "@" in parsed.netloc:
            _fail(where, f"image_url must not contain userinfo, got {url!r}")
        try:
            _ = parsed.port
        except ValueError:
            _fail(where, f"image_url must have a numeric port, got {url!r}")
        if not parsed.netloc.startswith("[") and not _URL_HOST_RE.match(parsed.hostname or ""):
            _fail(where, f"image_url host contains characters the server rejects, got {url!r}")

    if shape not in (None, "") and shape not in IMAGE_SHAPES:
        _fail(where, f"image_shape must be one of {list(IMAGE_SHAPES)}, got {shape!r}")

    if thumbhash in (None, ""):
        return
    _check_len(thumbhash, IMAGE_THUMBHASH_MAX, "image_thumbhash", where)
    if len(thumbhash) % 4 or not _THUMBHASH_B64_RE.fullmatch(str(thumbhash)):
        _fail(where, f"image_thumbhash must be padded standard-alphabet base64, got {thumbhash!r}")
    try:
        base64.b64decode(thumbhash, validate=True)
    except (binascii.Error, ValueError):
        _fail(where, f"image_thumbhash must be padded standard-alphabet base64, got {thumbhash!r}")


def _check_media_control(action: object, field: str, where: str) -> None:
    """One media control slot: a tap action that, on http(s), is a silent webhook.

    The server runs the shared tap-action rules and then rejects foreground on an
    http(s) control (it would open Safari on top of the player on every skip);
    custom schemes open that app and foreground is left alone there.
    """
    if action is None:
        return
    _check_tap_action(action, field, where)
    scheme = urlparse(str(action.get("url", ""))).scheme.lower()
    if scheme in _HTTP_SCHEMES and action.get("foreground") is True:
        _fail(where, f"{field}: media controls are always silent webhooks; foreground is not allowed")


def _assert_media(content: dict, where: str, template: str) -> None:
    """The media template's fields: media only, bounded, and controls as silent webhooks."""
    # "" counts as absent, as it does for the image trio (a cleared field is not a leak).
    present = [f for f in MEDIA_FIELDS if content.get(f) not in (None, "")]
    if template != "media":
        if present:
            _fail(where, f"{present} are only supported by the 'media' template, got {template!r}")
        return

    _check_len(content.get("media_title"), MEDIA_TITLE_MAX, "media_title", where)
    state = content.get("playback_state")
    if state not in (None, "") and state not in PLAYBACK_STATES:
        _fail(where, f"playback_state must be one of {list(PLAYBACK_STATES)}, got {state!r}")
    position = content.get("position_seconds")
    if position is not None and (not _is_finite_number(position) or not (0 <= float(position) <= MEDIA_DURATION_MAX)):
        _fail(where, f"position_seconds must be a finite number in [0, {MEDIA_DURATION_MAX}], got {position!r}")
    duration = content.get("duration_seconds")
    if duration is not None and (not _is_finite_number(duration) or not (0 < float(duration) <= MEDIA_DURATION_MAX)):
        _fail(where, f"duration_seconds must be a finite number in (0, {MEDIA_DURATION_MAX}], got {duration!r}")
    position_at = content.get("position_at")
    if position_at is not None:
        # A bare anchor says nothing: the server only ever stamps or clears it in
        # response to a position_seconds, so a frame carrying position_at alone
        # means the mapper broke its pairing rule.
        if content.get("position_seconds") is None:
            _fail(where, "position_at without position_seconds anchors nothing")
        if not _is_int(position_at) or position_at <= 0:
            _fail(where, f"position_at must be a positive unix timestamp, got {position_at!r}")
        if position_at > _now() + MAX_CLOCK_SKEW:
            _fail(where, f"position_at must not be in the future, got {position_at}")
        # A playhead sampled half a day ago says nothing about where the track is
        # now, and the server refuses to store one.
        if position_at < _now() - MEDIA_POSITION_MAX_AGE:
            _fail(where, f"position_at must be within the last {MEDIA_POSITION_MAX_AGE} seconds, got {position_at}")
    volume = content.get("volume")
    if volume is not None and (not _is_finite_number(volume) or not (0.0 <= float(volume) <= 1.0)):
        _fail(where, f"volume must be a finite number in [0.0, 1.0], got {volume!r}")
    favorite = content.get("favorite")
    if favorite is not None and not isinstance(favorite, bool):
        _fail(where, f"favorite must be a bool, got {favorite!r}")

    controls = content.get("controls")
    if controls is None:
        return
    if not isinstance(controls, dict):
        _fail(where, f"controls must be an object, got {type(controls).__name__}")
    unknown = sorted(set(controls) - set(MEDIA_CONTROL_SLOTS) - {"extra"})
    if unknown:
        _fail(where, f"controls has unknown slot(s) {unknown}")
    for slot in MEDIA_CONTROL_SLOTS:
        _check_media_control(controls.get(slot), f"controls.{slot}", where)
    extra = controls.get("extra")
    if extra is None:
        return
    if not isinstance(extra, list):
        _fail(where, f"controls.extra must be a list, got {type(extra).__name__}")
    if len(extra) > MEDIA_EXTRA_CONTROLS_MAX:
        _fail(where, f"controls.extra supports at most {MEDIA_EXTRA_CONTROLS_MAX} buttons, got {len(extra)}")
    for i, action in enumerate(extra):
        field = f"controls.extra[{i}]"
        if not isinstance(action, dict):
            _fail(where, f"{field} must be an object, got {type(action).__name__}")
        _check_media_control(action, field, where)
        if not action.get("icon"):
            _fail(where, f"{field}.icon is required")


def _assert_live_progress(content: dict, where: str, template: str) -> None:
    """live_progress is a generic/steps opt-in pairing with a future end_date.

    Generic fills the whole bar to 1.0 by end_date; steps fills the current step
    across start_date..end_date. Deliberately at least as strict as the server: it
    also requires end_date to be in the *future*, where the server accepts any
    positive timestamp within five years.
    """
    live_progress = content.get("live_progress")
    if live_progress is not None and not isinstance(live_progress, bool):
        _fail(where, f"live_progress must be a bool, got {live_progress!r}")
    end_date = content.get("end_date")
    if end_date is not None:
        if not _is_int(end_date) or end_date <= 0:
            _fail(where, f"{template} end_date must be a positive timestamp, got {end_date!r}")
        if end_date > _now() + MAX_FUTURE_OFFSET:
            _fail(where, f"{template} end_date must be within 5 years of now, got {end_date}")
    if live_progress and (not _is_int(end_date) or end_date <= _now()):
        _fail(where, f"live_progress requires a future end_date, got {end_date!r}")


def _assert_generic(content: dict, where: str) -> None:
    _assert_live_progress(content, where, "generic")


def _assert_countdown(content: dict, where: str) -> None:
    end_date = content.get("end_date")
    if not _is_int(end_date) or end_date <= 0:
        _fail(where, f"countdown requires a positive end_date, got {end_date!r}")
    if end_date > _now() + MAX_FUTURE_OFFSET:
        _fail(where, f"countdown end_date must be within 5 years of now, got {end_date}")
    start_date = content.get("start_date")
    if start_date is not None:
        if not _is_int(start_date) or start_date <= 0:
            _fail(where, f"countdown start_date must be a positive timestamp, got {start_date!r}")
        if start_date >= end_date:
            _fail(where, f"countdown start_date ({start_date}) must be before end_date ({end_date})")
    wt = content.get("warning_threshold")
    if wt is not None and (not _is_int(wt) or not (0 <= wt <= WARNING_THRESHOLD_MAX)):
        _fail(where, f"warning_threshold must be an int in [0, {WARNING_THRESHOLD_MAX}], got {wt!r}")
    snooze = content.get("snooze_seconds")
    if snooze is not None and (not _is_int(snooze) or not (SNOOZE_SECONDS_MIN <= snooze <= SNOOZE_SECONDS_MAX)):
        _fail(where, f"snooze_seconds must be an int in [{SNOOZE_SECONDS_MIN}, {SNOOZE_SECONDS_MAX}], got {snooze!r}")


def _assert_steps(content: dict, where: str) -> None:
    total = content.get("total_steps")
    current = content.get("current_step")
    if not _is_int(total) or total < 1:
        _fail(where, f"steps requires total_steps >= 1, got {total!r}")
    if total > TOTAL_STEPS_MAX:
        _fail(where, f"steps total_steps must be <= {TOTAL_STEPS_MAX}, got {total}")
    if not _is_int(current) or current < 0:
        _fail(where, f"steps requires current_step >= 0, got {current!r}")
    if current > total:
        _fail(where, f"current_step ({current}) cannot exceed total_steps ({total})")
    rows = content.get("step_rows")
    if rows:
        if len(rows) != total:
            _fail(where, f"step_rows length ({len(rows)}) must equal total_steps ({total})")
        for i, r in enumerate(rows):
            if not _is_int(r) or not (STEP_ROW_MIN <= r <= STEP_ROW_MAX):
                _fail(where, f"step_rows[{i}] must be an int in [{STEP_ROW_MIN}, {STEP_ROW_MAX}], got {r!r}")
    labels = content.get("step_labels")
    if labels:
        if len(labels) != total:
            _fail(where, f"step_labels length ({len(labels)}) must equal total_steps ({total})")
        for i, label in enumerate(labels):
            _check_len(label, STEP_LABEL_MAX, f"step_labels[{i}]", where)
    weights = content.get("step_weights")
    if weights:
        if len(weights) != total:
            _fail(where, f"step_weights length ({len(weights)}) must equal total_steps ({total})")
        for i, w in enumerate(weights):
            if isinstance(w, bool) or not isinstance(w, (int, float)):
                _fail(where, f"step_weights[{i}] must be a number, got {w!r}")
            if not math.isfinite(w) or w <= 0:
                _fail(where, f"step_weights[{i}] must be a positive finite number, got {w!r}")
    colors = content.get("step_colors")
    if colors:
        if len(colors) != total:
            _fail(where, f"step_colors length ({len(colors)}) must equal total_steps ({total})")
        for i, col in enumerate(colors):
            # "" is legal here: the server reads it as "fall back to accent_color".
            _check_color(col, f"step_colors[{i}]", where)
    _assert_live_progress(content, where, "steps")
    # The animated window must be a real forward range, else iOS renders nothing.
    start_date = content.get("start_date")
    if content.get("live_progress"):
        if not _is_int(start_date) or start_date <= 0:
            _fail(where, f"steps live_progress requires a positive start_date, got {start_date!r}")
        if start_date >= content["end_date"]:
            _fail(where, f"steps start_date ({start_date}) must precede end_date ({content['end_date']})")


def _assert_alert(content: dict, where: str) -> None:
    severity = content.get("severity")
    if severity not in SEVERITIES:
        _fail(where, f"alert severity must be one of {SEVERITIES}, got {severity!r}")
    _check_len(content.get("severity_label"), MAX_SEVERITY_LABEL_LEN, "severity_label", where)
    fired_at = content.get("fired_at")
    if fired_at is not None:
        if not _is_int(fired_at) or fired_at <= 0:
            _fail(where, f"fired_at must be a positive timestamp, got {fired_at!r}")
        if fired_at > _now() + MAX_CLOCK_SKEW:
            _fail(where, f"fired_at must not be in the future, got {fired_at}")


def _assert_gauge(content: dict, where: str) -> None:
    value = content.get("value")
    min_v = content.get("min_value")
    max_v = content.get("max_value")
    for name, v in (("value", value), ("min_value", min_v), ("max_value", max_v)):
        if v is None:
            _fail(where, f"gauge requires {name}")
        if not _is_finite_number(v):
            _fail(where, f"gauge {name} must be a finite number, got {v!r}")
    if float(min_v) >= float(max_v):
        _fail(where, f"gauge min_value ({min_v}) must be less than max_value ({max_v})")
    if not (float(min_v) <= float(value) <= float(max_v)):
        _fail(where, f"gauge value ({value}) must be within [{min_v}, {max_v}]")
    _check_len(content.get("unit"), ACTIVITY_UNIT_MAX, "unit", where)


def _assert_timeline(content: dict, where: str) -> None:
    value = content.get("value")
    if value is None:
        _fail(where, "timeline requires value as a non-empty labelled map")
    if _is_number(value):
        _fail(where, "timeline value must be a labelled map (e.g. {'CPU': 72.5}), not a number")
    if not isinstance(value, dict) or not value:
        _fail(where, f"timeline value must be a non-empty map, got {value!r}")
    if len(value) > MAX_TIMELINE_SERIES:
        _fail(where, f"timeline value supports at most {MAX_TIMELINE_SERIES} series, got {len(value)}")
    for key, v in value.items():
        if key == "":
            _fail(where, "timeline value key must not be empty")
        _check_len(key, TIMELINE_VALUE_KEY_MAX, f"value key {key!r}", where)
        if not _is_finite_number(v):
            _fail(where, f"timeline values[{key!r}] must be a finite number, got {v!r}")

    primary = content.get("primary_series")
    if primary is not None:
        if not isinstance(primary, str):
            _fail(where, f"timeline primary_series must be a string, got {type(primary).__name__}")
        _check_len(primary, TIMELINE_VALUE_KEY_MAX, "primary_series", where)

    scale = content.get("scale")
    if scale not in (None, "") and scale not in SCALES:
        _fail(where, f"timeline scale must be one of {SCALES}, got {scale!r}")
    decimals = content.get("decimals")
    if decimals is not None and (
        not _is_int(decimals) or not (TIMELINE_DECIMALS_MIN <= decimals <= TIMELINE_DECIMALS_MAX)
    ):
        # decimals is a display-precision count: the server types it as an integer
        # 0..10, so a float (or bool) is rejected at decode/validation time.
        _fail(where, f"timeline decimals must be an int in [0, 10], got {decimals!r}")
    _check_len(content.get("unit"), ACTIVITY_UNIT_MAX, "unit", where)

    thresholds = content.get("thresholds") or []
    if len(thresholds) > THRESHOLDS_MAX:
        _fail(where, f"timeline supports at most {THRESHOLDS_MAX} thresholds, got {len(thresholds)}")
    for i, t in enumerate(thresholds):
        if not isinstance(t, dict):
            _fail(where, f"thresholds[{i}] must be an object, got {type(t).__name__}")
        if not _is_finite_number(t.get("value")):
            _fail(where, f"thresholds[{i}].value must be a finite number")
        _check_color(t.get("color"), f"thresholds[{i}].color", where)
        _check_len(t.get("label"), THRESHOLD_LABEL_MAX, f"thresholds[{i}].label", where)

    units = content.get("units") or {}
    if len(units) > MAX_TIMELINE_SERIES:
        _fail(where, f"timeline units supports at most {MAX_TIMELINE_SERIES} entries, got {len(units)}")
    for key, u in units.items():
        # `value` is guaranteed a non-empty dict by the checks above.
        if key not in value:
            _fail(where, f"units key {key!r} must match a values key")
        _check_len(u, ACTIVITY_UNIT_MAX, f"units[{key!r}]", where)

    history = content.get("history") or {}
    max_future = _now() + MAX_CLOCK_SKEW
    for key, points in history.items():
        for i, p in enumerate(points):
            if not isinstance(p, dict):
                _fail(where, f"history[{key!r}][{i}] must be an object, got {type(p).__name__}")
            ts = p.get("timestamp")
            if not _is_int(ts) or ts <= 0:
                _fail(where, f"history[{key!r}][{i}].timestamp must be a positive timestamp, got {ts!r}")
            if ts > max_future:
                _fail(where, f"history[{key!r}][{i}].timestamp must not be in the future, got {ts}")
            if not _is_finite_number(p.get("value")):
                _fail(where, f"history[{key!r}][{i}].value must be a finite number")


def _assert_board(content: dict, where: str) -> None:
    tiles = content.get("tiles")
    if not isinstance(tiles, list) or not tiles:
        _fail(where, f"board requires a non-empty tiles list, got {tiles!r}")
    if len(tiles) > BOARD_MAX_TILES:
        _fail(where, f"board supports at most {BOARD_MAX_TILES} tiles, got {len(tiles)}")
    for i, tile in enumerate(tiles):
        if not isinstance(tile, dict):
            _fail(where, f"tiles[{i}] must be an object, got {type(tile).__name__}")
        label = tile.get("label")
        if not label or not str(label).strip():
            _fail(where, f"tiles[{i}].label is required")
        _check_len(label, BOARD_TILE_LABEL_MAX, f"tiles[{i}].label", where)
        # value is a STRING field on the server (BoardTile.Value) — a JSON number/bool
        # would fail to decode, so the contract requires a non-empty string.
        value = tile.get("value")
        if not isinstance(value, str):
            _fail(where, f"tiles[{i}].value must be a string, got {type(value).__name__}")
        if not value:
            _fail(where, f"tiles[{i}].value is required")
        _check_len(value, BOARD_TILE_VALUE_MAX, f"tiles[{i}].value", where)
        _check_len(tile.get("unit"), BOARD_TILE_UNIT_MAX, f"tiles[{i}].unit", where)
        _check_len(tile.get("icon"), BOARD_TILE_ICON_MAX, f"tiles[{i}].icon", where)
        _check_color(tile.get("color"), f"tiles[{i}].color", where)
        trend = tile.get("trend")
        if trend not in (None, "") and trend not in BOARD_TRENDS:
            _fail(where, f"tiles[{i}].trend must be one of {BOARD_TRENDS}, got {trend!r}")
        _check_tap_action(tile.get("url_action"), f"tiles[{i}].url_action", where)


def _assert_log(content: dict, where: str) -> None:
    lines = content.get("lines")
    if not isinstance(lines, list) or not lines:
        _fail(where, f"log requires a non-empty lines list, got {lines!r}")
    if len(lines) > LOG_MAX_LINES:
        _fail(where, f"log supports at most {LOG_MAX_LINES} lines, got {len(lines)}")
    for i, line in enumerate(lines):
        if not isinstance(line, dict):
            _fail(where, f"lines[{i}] must be an object, got {type(line).__name__}")
        text = line.get("text")
        if not text or not str(text).strip():
            _fail(where, f"lines[{i}].text is required")
        _check_len(text, LOG_LINE_TEXT_MAX, f"lines[{i}].text", where)
        at = line.get("at")
        if at is not None and (not _is_int(at) or at <= 0):
            # at is a *int64 unix timestamp on the server — a non-int JSON fails decode.
            _fail(where, f"lines[{i}].at must be a positive timestamp, got {at!r}")
        level = line.get("level")
        if level not in (None, "") and level not in LOG_LEVELS:
            _fail(where, f"lines[{i}].level must be one of {LOG_LEVELS}, got {level!r}")
    # log_backlog is server-owned — the integration must never send it.
    if "log_backlog" in content:
        _fail(where, "log must not send log_backlog (server-owned field)")


def _check_widget_date(value: object, field: str, where: str) -> datetime:
    """Assert an RFC 3339 widget date inside the server's floor/horizon window."""
    if not isinstance(value, str) or not value:
        _fail(where, f"{field} must be an RFC 3339 timestamp string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail(where, f"{field} is not a parseable RFC 3339 timestamp: {value!r}")
    if parsed.tzinfo is None:
        _fail(where, f"{field} must carry a UTC offset, got {value!r}")
    ts = parsed.timestamp()
    if ts < WIDGET_DATE_FLOOR_TS:
        _fail(where, f"{field} predates the widget date floor (2000-01-01), got {value!r}")
    horizon = (datetime.now(parsed.tzinfo) + timedelta(days=WIDGET_DATE_HORIZON_DAYS + 1)).timestamp()
    if ts > horizon:
        _fail(where, f"{field} is beyond the widget date horizon, got {value!r}")
    return parsed


def _check_timer(timer: object, field: str, where: str) -> None:
    """Assert an optional self-updating timer slot: {date, style?}."""
    if timer is None:
        return
    if not isinstance(timer, dict):
        _fail(where, f"{field} must be an object, got {type(timer).__name__}")
    _check_widget_date(timer.get("date"), f"{field}.date", where)
    style = timer.get("style") or ""
    if style not in TIMER_STYLES and style != "":
        _fail(where, f"{field}.style must be one of {TIMER_STYLES}, got {style!r}")


def _check_node(node: object, field: str, where: str, *, require_name: bool) -> None:
    """Assert the presentation fields a battery device and a flow node share."""
    name = node.get("name") if isinstance(node, dict) else None
    if require_name and not str(name or "").strip():
        _fail(where, f"{field}.name must not be empty")
    _check_len(name, WIDGET_NODE_NAME_MAX, f"{field}.name", where)
    _check_len(node.get("icon"), WIDGET_NODE_ICON_MAX, f"{field}.icon", where)
    _check_color(node.get("color"), f"{field}.color", where)


def _check_percent(value: object, field: str, where: str) -> None:
    if not _is_finite_number(value):
        _fail(where, f"{field} must be a finite number, got {value!r}")
    if not (0 <= float(value) <= 100):
        _fail(where, f"{field} must be between 0 and 100, got {value!r}")


def _assert_widget_collections(content: dict, where: str) -> None:
    """Bound every widget array/nested-object field, for EVERY template.

    Mirrors the server's own validateCollections, which deliberately runs
    template-agnostically: PATCH merges arrays wholesale, so a half-built element
    stored under a template that ignores the field still breaks the iOS decode of
    the whole widget list later.
    """
    points = content.get("points")
    if points is not None:
        if not isinstance(points, list):
            _fail(where, f"points must be a list, got {type(points).__name__}")
        if points and not (WIDGET_MIN_TREND_POINTS <= len(points) <= WIDGET_MAX_TREND_POINTS):
            _fail(
                where,
                f"trend requires between {WIDGET_MIN_TREND_POINTS} and {WIDGET_MAX_TREND_POINTS} points, "
                f"got {len(points)}",
            )
        for i, point in enumerate(points):
            if not _is_finite_number(point):
                _fail(where, f"points[{i}] must be a finite number, got {point!r}")

    devices = content.get("devices")
    if devices is not None:
        if not isinstance(devices, list):
            _fail(where, f"devices must be a list, got {type(devices).__name__}")
        if len(devices) > WIDGET_MAX_BATTERY_DEVICES:
            _fail(where, f"battery supports at most {WIDGET_MAX_BATTERY_DEVICES} devices, got {len(devices)}")
        for i, device in enumerate(devices):
            if not isinstance(device, dict):
                _fail(where, f"devices[{i}] must be an object, got {type(device).__name__}")
            _check_node(device, f"devices[{i}]", where, require_name=True)
            _check_percent(device.get("level"), f"devices[{i}].level", where)

    sort_keys = content.get("device_sort")
    if sort_keys is not None:
        if not isinstance(sort_keys, list):
            _fail(where, f"device_sort must be a list, got {type(sort_keys).__name__}")
        if len(sort_keys) > WIDGET_MAX_DEVICE_SORT_KEYS:
            _fail(where, f"device_sort supports at most {WIDGET_MAX_DEVICE_SORT_KEYS} keys, got {len(sort_keys)}")
        seen_sort_fields = set()
        for i, key in enumerate(sort_keys):
            if not isinstance(key, dict):
                _fail(where, f"device_sort[{i}] must be an object, got {type(key).__name__}")
            field = key.get("field")
            if field not in WIDGET_DEVICE_SORT_FIELDS:
                _fail(where, f"device_sort[{i}].field must be one of {WIDGET_DEVICE_SORT_FIELDS}, got {field!r}")
            # An absent direction is read as ascending, so "" is a legal value.
            direction = key.get("direction") or ""
            if direction not in WIDGET_DEVICE_SORT_DIRECTIONS:
                _fail(where, f"device_sort[{i}].direction must be one of asc/desc, got {direction!r}")
            if field in seen_sort_fields:
                _fail(where, f"device_sort[{i}].field duplicates an earlier key, got {field!r}")
            seen_sort_fields.add(field)

    periods = content.get("periods")
    if periods is not None:
        if not isinstance(periods, list):
            _fail(where, f"periods must be a list, got {type(periods).__name__}")
        if len(periods) > WIDGET_MAX_SCHEDULE_PERIODS:
            _fail(where, f"schedule supports at most {WIDGET_MAX_SCHEDULE_PERIODS} periods, got {len(periods)}")
        previous: datetime | None = None
        for i, period in enumerate(periods):
            if not isinstance(period, dict):
                _fail(where, f"periods[{i}] must be an object, got {type(period).__name__}")
            start = _check_widget_date(period.get("start"), f"periods[{i}].start", where)
            if previous is not None and start <= previous:
                _fail(where, f"periods[{i}].start must be after periods[{i - 1}].start")
            previous = start
            if not _is_finite_number(period.get("value")):
                _fail(where, f"periods[{i}].value must be a finite number, got {period.get('value')!r}")
            level = period.get("level") or ""
            if level and level not in SCHEDULE_LEVELS:
                _fail(where, f"periods[{i}].level must be one of {SCHEDULE_LEVELS}, got {level!r}")

    flow = content.get("flow")
    if flow is not None:
        if not isinstance(flow, dict):
            _fail(where, f"flow must be an object, got {type(flow).__name__}")
        inputs = flow.get("inputs") or []
        if len(inputs) > WIDGET_MAX_FLOW_INPUTS:
            _fail(where, f"flow supports at most {WIDGET_MAX_FLOW_INPUTS} inputs, got {len(inputs)}")
        slots = [(f"flow.inputs[{i}]", node) for i, node in enumerate(inputs)]
        slots += [(f"flow.{name}", flow[name]) for name in ("output", "storage", "exchange") if flow.get(name)]
        for field, node in slots:
            if not isinstance(node, dict):
                _fail(where, f"{field} must be an object, got {type(node).__name__}")
            if not _is_finite_number(node.get("rate")):
                _fail(where, f"{field}.rate is required and must be finite, got {node.get('rate')!r}")
            total = node.get("total")
            if total is not None:
                if not _is_finite_number(total):
                    _fail(where, f"{field}.total must be a finite number, got {total!r}")
                if float(total) < 0:
                    _fail(where, f"{field}.total must not be negative, got {total!r}")
            if node.get("level") is not None:
                _check_percent(node.get("level"), f"{field}.level", where)
            _check_node(node, field, where, require_name=False)

    _check_len(content.get("expired_text"), WIDGET_EXPIRED_TEXT_MAX, "expired_text", where)
    _check_timer(content.get("subtitle_timer"), "subtitle_timer", where)

    start_date, end_date = content.get("start_date"), content.get("end_date")
    parsed_start = _check_widget_date(start_date, "start_date", where) if start_date is not None else None
    parsed_end = _check_widget_date(end_date, "end_date", where) if end_date is not None else None
    if parsed_start is not None and parsed_end is not None and parsed_start >= parsed_end:
        _fail(where, f"start_date ({start_date}) must be before end_date ({end_date})")


def assert_valid_widget_content(content: dict, template: str | None = None, *, where: str = "widget") -> None:
    """Assert ``content`` satisfies the public widget content contract.

    ``template`` may be passed explicitly (the mappers return content *without*
    the template — the API client injects it on create), or read from
    ``content['template']`` when present.
    """
    if not isinstance(content, dict):
        _fail(where, f"content must be a dict, got {type(content).__name__}")
    template = template or content.get("template")
    if template not in WIDGET_TEMPLATES:
        _fail(where, f"widget template {template!r} is not one of {WIDGET_TEMPLATES}")

    _check_len(content.get("icon"), ICON_MAX, "icon", where)
    _check_len(content.get("label"), WIDGET_LABEL_MAX, "label", where)
    _check_len(content.get("subtitle"), WIDGET_SUBTITLE_MAX, "subtitle", where)
    _check_len(content.get("unit"), WIDGET_UNIT_MAX, "unit", where)

    # `or ""` coerces both an absent key and an explicit null to the empty-string
    # sentinel — the server treats a null severity/trend as "no annotation".
    severity = content.get("severity") or ""
    if severity not in WIDGET_SEVERITIES:
        _fail(where, f"widget severity must be one of {WIDGET_SEVERITIES}, got {severity!r}")
    trend = content.get("trend") or ""
    if trend not in _TRENDS:
        _fail(where, f"widget trend must be one of up/down/flat, got {trend!r}")
    for field in ("accent_color", "background_color", "text_color"):
        _check_color(content.get(field), field, where)
    for field in ("tap_action", "url_action", "secondary_url_action"):
        _check_tap_action(content.get(field), field, where)

    _assert_widget_collections(content, where)

    if template == "progress":
        value = content.get("value")
        has_window = content.get("start_date") is not None and content.get("end_date") is not None
        if value is None and not has_window:
            _fail(where, "progress widget requires a value unless start_date and end_date are both set")
        if value is not None:
            if not _is_finite_number(value):
                _fail(where, f"progress widget requires a finite value, got {value!r}")
            if not (0.0 <= float(value) <= 1.0):
                _fail(where, f"progress widget value must be in [0.0, 1.0], got {value!r}")
    elif template == "trend":
        value = content.get("value")
        if value is None or not _is_finite_number(value):
            _fail(where, f"trend widget requires a finite value, got {value!r}")
        if not content.get("points"):
            _fail(where, "trend widget requires points")
        min_v, max_v = content.get("min_value"), content.get("max_value")
        if min_v is not None and max_v is not None and float(min_v) >= float(max_v):
            _fail(where, f"trend widget min_value ({min_v}) must be less than max_value ({max_v})")
    elif template == "countdown":
        if content.get("end_date") is None:
            _fail(where, "countdown widget requires end_date")
    elif template == "battery":
        if not content.get("devices"):
            _fail(where, "battery widget requires at least one device")
    elif template == "schedule":
        if not content.get("periods"):
            _fail(where, "schedule widget requires at least one period")
    elif template == "flow":
        flow = content.get("flow")
        if not flow:
            _fail(where, "flow widget requires a flow object")
        if not any(flow.get(slot) for slot in ("inputs", "output", "storage", "exchange")):
            _fail(where, "flow widget requires at least one of inputs, output, storage, or exchange")
    elif template == "gauge":
        value, min_v, max_v = content.get("value"), content.get("min_value"), content.get("max_value")
        for name, v in (("value", value), ("min_value", min_v), ("max_value", max_v)):
            if v is None or not _is_finite_number(v):
                _fail(where, f"gauge widget {name} must be a finite number, got {v!r}")
        if float(min_v) >= float(max_v):
            _fail(where, f"gauge widget min_value ({min_v}) must be less than max_value ({max_v})")
        if not (float(min_v) <= float(value) <= float(max_v)):
            _fail(where, f"gauge widget value ({value}) must be within [{min_v}, {max_v}]")
    elif template == "value":
        value = content.get("value")
        if value is not None and not _is_finite_number(value):
            _fail(where, f"value widget value must be a finite number, got {value!r}")
    elif template == "stat_list":
        rows = content.get("stat_rows")
        if not rows:
            _fail(where, "stat_list widget requires at least one stat_rows entry")
        if len(rows) > WIDGET_MAX_STAT_ROWS:
            _fail(where, f"stat_list supports at most {WIDGET_MAX_STAT_ROWS} rows, got {len(rows)}")
        for i, row in enumerate(rows):
            if not str(row.get("label", "")).strip():
                _fail(where, f"stat_rows[{i}].label must not be empty")
            if not str(row.get("value", "")).strip():
                _fail(where, f"stat_rows[{i}].value must not be empty")
            _check_len(row.get("label"), WIDGET_STAT_LABEL_MAX, f"stat_rows[{i}].label", where)
            _check_len(row.get("value"), WIDGET_STAT_VALUE_MAX, f"stat_rows[{i}].value", where)
            _check_len(row.get("unit"), WIDGET_STAT_UNIT_MAX, f"stat_rows[{i}].unit", where)
            _check_timer(row.get("timer"), f"stat_rows[{i}].timer", where)


def assert_valid_sound(sound: object, *, where: str = "sound") -> None:
    """Assert ``sound`` is an accepted Live-Activity alert sound (or empty/None)."""
    if sound in (None, ""):
        return
    if sound not in SOUNDS:
        _fail(where, f"sound {sound!r} is not one of {SOUNDS}")


def assert_valid_priority(priority: object, *, where: str = "priority") -> None:
    """Assert a Live-Activity priority is within the accepted range."""
    if not _is_int(priority) or not (PRIORITY_MIN <= priority <= PRIORITY_MAX):
        _fail(where, f"priority must be an int in [{PRIORITY_MIN}, {PRIORITY_MAX}], got {priority!r}")


def _now() -> int:
    return int(time.time())
