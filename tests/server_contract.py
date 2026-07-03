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

import math
import time
from urllib.parse import urlparse

from custom_components.pushward.const import (
    BOARD_MAX_TILES,
    BOARD_TILE_ICON_MAX,
    BOARD_TILE_LABEL_MAX,
    BOARD_TILE_UNIT_MAX,
    BOARD_TILE_VALUE_MAX,
    BOARD_TRENDS,
    LOG_LEVELS,
    LOG_LINE_TEXT_MAX,
    LOG_MAX_LINES,
    MAX_LONG_TEXT_LEN,
    MAX_SEVERITY_LABEL_LEN,
    MAX_TAP_ACTION_ICON_LEN,
    MAX_TAP_ACTION_TITLE_LEN,
    MAX_TEXT_LEN,
    MAX_URL_LEN,
    PRIORITY_MAX,
    PRIORITY_MIN,
    SCALES,
    SEVERITIES,
    SNOOZE_SECONDS_MAX,
    SNOOZE_SECONDS_MIN,
    SOUNDS,
    TEMPLATES,
    TIMELINE_MAX_SERIES,
    TIMELINE_SERIES_LABEL_MAX,
    TOTAL_STEPS_MAX,
    WARNING_THRESHOLD_MAX,
    WIDGET_LABEL_MAX,
    WIDGET_MAX_STAT_ROWS,
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
ACTIVITY_UNIT_MAX = 32  # config_flow caps gauge/timeline unit at 32
STEP_LABEL_MAX = 32
STEP_ROW_MIN = 1
STEP_ROW_MAX = 10
THRESHOLD_LABEL_MAX = 12
THRESHOLDS_MAX = 5
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

_HTTP_SCHEMES = ("http", "https")
_TRENDS = ("", WIDGET_TREND_UP, WIDGET_TREND_DOWN, WIDGET_TREND_FLAT)


class PushWardContractError(AssertionError):
    """Raised when a content payload violates the public PushWard API contract."""


def _fail(where: str, msg: str) -> None:
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
    if template not in TEMPLATES:
        _fail(where, f"template {template!r} is not one of {TEMPLATES}")

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

    if template == "countdown":
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

    if template == "progress":
        value = content.get("value")
        if value is None or not _is_finite_number(value):
            _fail(where, f"progress widget requires a finite value, got {value!r}")
        if not (0.0 <= float(value) <= 1.0):
            _fail(where, f"progress widget value must be in [0.0, 1.0], got {value!r}")
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
