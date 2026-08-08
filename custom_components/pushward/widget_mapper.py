"""Map Home Assistant state/attributes to PushWard widget content."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_BACKGROUND_COLOR,
    CONF_BATTERY_DEVICES,
    CONF_CHARGING_ENTITY,
    CONF_END_DATE_ATTRIBUTE,
    CONF_ENTITY_ID,
    CONF_EXPIRED_TEXT,
    CONF_FLOW_NODES,
    CONF_FLOW_SLOT,
    CONF_ICON,
    CONF_LABEL,
    CONF_LABEL_ATTRIBUTE,
    CONF_LEVEL_ENTITY,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_NODE_NAME,
    CONF_SCHEDULE_ATTRIBUTES,
    CONF_SCHEDULE_HIGH_MIN,
    CONF_SCHEDULE_LOW_MAX,
    CONF_SCHEDULE_START_KEY,
    CONF_SCHEDULE_VALUE_KEY,
    CONF_SEVERITY,
    CONF_START_DATE_ATTRIBUTE,
    CONF_STAT_ROWS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_SUBTITLE_TIMER_ATTRIBUTE,
    CONF_SUBTITLE_TIMER_ENTITY,
    CONF_SUBTITLE_TIMER_STYLE,
    CONF_TEXT_COLOR,
    CONF_TOTAL_ENTITY,
    CONF_UNIT,
    CONF_VALUE_ATTRIBUTE,
    CONF_VALUE_SCALE,
    CONF_WIDGET_NAME,
    CONF_WIDGET_TEMPLATE,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    DEFAULT_SCHEDULE_START_KEY,
    DEFAULT_SCHEDULE_VALUE_KEY,
    DEFAULT_SUBTITLE_TIMER_STYLE,
    DEFAULT_VALUE_SCALE,
    FLOW_SLOT_INPUT,
    FRACTION_OVERSHOOT_TOLERANCE,
    TIMER_STYLES,
    VALUE_SCALE_FRACTION,
    VALUE_SCALE_PERCENT,
    WIDGET_DATE_FLOOR_TS,
    WIDGET_DATE_HORIZON_DAYS,
    WIDGET_EXPIRED_TEXT_MAX,
    WIDGET_GROUP_TEMPLATES,
    WIDGET_LABEL_MAX,
    WIDGET_MAX_BATTERY_DEVICES,
    WIDGET_MAX_FLOW_INPUTS,
    WIDGET_MAX_SCHEDULE_PERIODS,
    WIDGET_MAX_STAT_ROWS,
    WIDGET_MAX_TREND_POINTS,
    WIDGET_MIN_TREND_POINTS,
    WIDGET_NAME_MAX,
    WIDGET_NODE_ICON_MAX,
    WIDGET_NODE_NAME_MAX,
    WIDGET_STAT_LABEL_MAX,
    WIDGET_STAT_UNIT_MAX,
    WIDGET_STAT_VALUE_MAX,
    WIDGET_SUBTITLE_MAX,
    WIDGET_TEMPLATE_BATTERY,
    WIDGET_TEMPLATE_COUNTDOWN,
    WIDGET_TEMPLATE_FLOW,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_PROGRESS,
    WIDGET_TEMPLATE_SCHEDULE,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_STATUS,
    WIDGET_TEMPLATE_TREND,
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TREND_DOWN,
    WIDGET_TREND_FLAT,
    WIDGET_TREND_UP,
    WIDGET_UNIT_MAX,
)
from .content_mapper import add_tap_action, color_to_str, is_valid_color, resolve_color, resolve_icon

_LOGGER = logging.getLogger(__name__)

# Attribute holding the end of the window for domains that publish one under a
# well-known name. Only consulted when the user configured no end-date attribute.
_END_DATE_DOMAIN_DEFAULTS = {
    "timer": "finishes_at",
    "calendar": "end_time",
}


def _truncate(value: str, limit: int) -> str:
    if not value:
        return ""
    return value if len(value) <= limit else value[:limit]


def _coerce_float(value: object) -> float | None:
    """Lenient float coercion. Returns None on failure."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _finite(value: float | None) -> float | None:
    """Pass a float through only when the server would accept it."""
    if value is None or not math.isfinite(value):
        return None
    return value


def _read_value(state: State, config: dict) -> object:
    """Return raw value from attribute (if configured) or entity state."""
    attr = config.get(CONF_VALUE_ATTRIBUTE)
    if attr:
        return state.attributes.get(attr)
    return state.state


def read_numeric_value(state: State, config: dict) -> float | None:
    """Public read of the bound numeric value, exactly as the templates see it.

    The widget manager samples the trend buffer through this so the points it
    stores can never drift from the value the mapper renders.
    """
    return _coerce_float(_read_value(state, config))


def _read_string(state: State, config: dict, static_key: str, attr_key: str) -> str:
    """Resolve a string field: attribute override → static config → ''."""
    attr_name = config.get(attr_key)
    if attr_name:
        raw = state.attributes.get(attr_name)
        if raw not in (None, ""):
            return str(raw)
    return str(config.get(static_key, "") or "")


def _label_or_subtitle(state: State, config: dict) -> tuple[str, str]:
    """Resolve (label, subtitle). Both are optional and attribute-overridable.

    Unlike the activity mapper, no friendly_name fallback — widgets are compact
    and prefer empty fields over auto-filled noise.
    """
    label = _read_string(state, config, CONF_LABEL, CONF_LABEL_ATTRIBUTE)

    subtitle_attr = config.get(CONF_SUBTITLE_ATTRIBUTE)
    if subtitle_attr:
        raw = state.attributes.get(subtitle_attr)
        subtitle = str(raw) if raw is not None else ""
    else:
        subtitle = ""

    return _truncate(label, WIDGET_LABEL_MAX), _truncate(subtitle, WIDGET_SUBTITLE_MAX)


def _trend(value: float | None, prev_value: float | None) -> str:
    """Auto-derive trend from value delta. Empty when no prior value."""
    if value is None or prev_value is None:
        return ""
    if value > prev_value:
        return WIDGET_TREND_UP
    if value < prev_value:
        return WIDGET_TREND_DOWN
    return WIDGET_TREND_FLAT


def _is_unavailable(state: State | None) -> bool:
    return state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)


# ----- dates and timers -----


def _parse_widget_datetime(raw: object) -> datetime | None:
    """Parse an HA attribute into an aware datetime, or None.

    Accepts what integrations actually publish: an RFC 3339 / ISO string, a real
    datetime, or a unix epoch number. A naive datetime is read as HA local time,
    matching how HA renders one.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        return dt_util.as_utc(raw) if raw.tzinfo else dt_util.as_utc(dt_util.as_local(raw))
    if isinstance(raw, (int, float)):
        if not math.isfinite(raw):
            return None
        try:
            return datetime.fromtimestamp(raw, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    parsed = dt_util.parse_datetime(raw.strip())
    if parsed is None:
        return None
    return dt_util.as_utc(parsed) if parsed.tzinfo else dt_util.as_utc(dt_util.as_local(parsed))


def _widget_date_ok(value: datetime) -> bool:
    """True when the server's date bounds would accept ``value``.

    Floor is the server's absolute 2000-01-01; the ceiling is a day inside the
    server's 366-day horizon because the server resolves "now" when the request
    lands. An out-of-bounds date is OMITTED rather than clamped: one 422 rejects
    the whole PATCH, taking every other field with it.
    """
    ts = value.timestamp()
    horizon = dt_util.utcnow() + timedelta(days=WIDGET_DATE_HORIZON_DAYS)
    return WIDGET_DATE_FLOOR_TS <= ts <= horizon.timestamp()


def _iso(value: datetime) -> str:
    """Serialize an aware datetime as the RFC 3339 string the server decodes."""
    return dt_util.as_utc(value).isoformat()


def _read_datetime(
    hass: HomeAssistant,
    state: State | None,
    config: dict,
    entity_key: str | None,
    attr_key: str | None,
) -> datetime | None:
    """Resolve a date from an optional companion entity and optional attribute.

    Falls back to ``state`` when no companion entity is configured, and to the
    source's own state when no attribute is named. Returns None for anything the
    server would reject, so the caller simply omits the field.
    """
    source = state
    entity_id = config.get(entity_key) if entity_key else None
    if entity_id:
        source = hass.states.get(entity_id)
    if _is_unavailable(source):
        return None
    assert source is not None
    attr = config.get(attr_key) if attr_key else None
    raw = source.attributes.get(attr) if attr else source.state
    parsed = _parse_widget_datetime(raw)
    if parsed is None or not _widget_date_ok(parsed):
        return None
    return parsed


def _timer_style(config: dict, key: str) -> str:
    style = str(config.get(key, "") or "").strip().lower()
    return style if style in TIMER_STYLES else DEFAULT_SUBTITLE_TIMER_STYLE


def _subtitle_timer(hass: HomeAssistant, state: State, config: dict) -> dict | None:
    """Build the subtitle_timer slot for a single-entity template."""
    if not config.get(CONF_SUBTITLE_TIMER_ENTITY) and not config.get(CONF_SUBTITLE_TIMER_ATTRIBUTE):
        return None
    when = _read_datetime(hass, state, config, CONF_SUBTITLE_TIMER_ENTITY, CONF_SUBTITLE_TIMER_ATTRIBUTE)
    if when is None:
        return None
    return {"date": _iso(when), "style": _timer_style(config, CONF_SUBTITLE_TIMER_STYLE)}


# ----- shared chrome -----


_GROUP_COLORS = (
    (CONF_ACCENT_COLOR, "accent_color"),
    (CONF_BACKGROUND_COLOR, "background_color"),
    (CONF_TEXT_COLOR, "text_color"),
)


def _apply_static_color(content: dict, config: dict, conf_key: str, out_key: str) -> None:
    raw = config.get(conf_key) or ""
    if not raw:
        return
    sanitized = color_to_str(raw)
    if sanitized:
        content[out_key] = sanitized


def _group_chrome(config: dict) -> dict:
    """Static icon/label/colors/tap-action for a multi-entity template.

    No single state anchors an attribute override, so only the static config
    applies - the same rule stat_list has always followed.
    """
    content: dict = {}
    icon = config.get(CONF_ICON) or ""
    if icon:
        content["icon"] = str(icon)
    label = str(config.get(CONF_LABEL, "") or "")
    if label:
        content["label"] = _truncate(label, WIDGET_LABEL_MAX)
    for conf_key, out_key in _GROUP_COLORS:
        _apply_static_color(content, config, conf_key, out_key)
    add_tap_action(content, config)
    return content


def map_widget_content(
    hass: HomeAssistant,
    config: dict,
    *,
    prev_value: float | None = None,
    registry_icon: str | None = None,
    points: list[float] | None = None,
) -> dict | None:
    """Render a WidgetContent dict from HA state + widget config.

    Returns None when the configuration cannot produce a valid payload yet
    (e.g. progress/gauge with no usable numeric state). Caller decides
    whether to skip the request entirely or send a placeholder.
    """
    template = config.get(CONF_WIDGET_TEMPLATE)
    if not template:
        return None

    if template in WIDGET_GROUP_TEMPLATES:
        return _GROUP_MAPPERS[template](hass, config)

    entity_id = config.get(CONF_ENTITY_ID)
    state = hass.states.get(entity_id) if entity_id else None

    # For single-entity templates, unavailable entity means skip the update.
    if _is_unavailable(state):
        if template == WIDGET_TEMPLATE_STATUS:
            # status template can render without a numeric value — emit minimal content.
            return _map_status_static(config)
        return None

    assert state is not None  # narrowing for type-checkers

    label, subtitle = _label_or_subtitle(state, config)
    icon = resolve_icon(state, config, registry_icon=registry_icon)
    accent = resolve_color(state, config, CONF_ACCENT_COLOR, CONF_ACCENT_COLOR_ATTRIBUTE)
    # Background/text colors are static-only — no attribute selector in the widget config flow.
    background = color_to_str(config.get(CONF_BACKGROUND_COLOR, "") or "")
    text_color = color_to_str(config.get(CONF_TEXT_COLOR, "") or "")

    content: dict = {}
    if icon:
        content["icon"] = icon
    if label:
        content["label"] = label
    if subtitle:
        content["subtitle"] = subtitle
    if accent:
        content["accent_color"] = accent
    if background:
        content["background_color"] = background
    if text_color:
        content["text_color"] = text_color

    unit = str(config.get(CONF_UNIT, "") or "")
    if unit:
        content["unit"] = _truncate(unit, WIDGET_UNIT_MAX)

    add_tap_action(content, config)

    timer = _subtitle_timer(hass, state, config)
    if timer:
        content["subtitle_timer"] = timer

    if template == WIDGET_TEMPLATE_VALUE:
        return _map_value(state, config, content, prev_value)

    if template == WIDGET_TEMPLATE_PROGRESS:
        return _map_progress(hass, state, config, content)

    if template == WIDGET_TEMPLATE_GAUGE:
        return _map_gauge(state, config, content, prev_value)

    if template == WIDGET_TEMPLATE_TREND:
        return _map_trend(state, config, content, points)

    if template == WIDGET_TEMPLATE_COUNTDOWN:
        return _map_countdown(hass, state, config, content)

    if template == WIDGET_TEMPLATE_SCHEDULE:
        return _map_schedule(state, config, content)

    if template == WIDGET_TEMPLATE_STATUS:
        severity = str(config.get(CONF_SEVERITY, "") or "")
        if severity:
            content["severity"] = severity
        return content

    _LOGGER.debug("Unknown widget template %r", template)
    return None


def _map_value(state: State, config: dict, content: dict, prev_value: float | None) -> dict:
    """value template: optional numeric value with auto-derived trend."""
    value = _finite(read_numeric_value(state, config))
    if value is not None:
        content["value"] = value
        trend = _trend(value, prev_value)
        if trend:
            content["trend"] = trend
    return content


def _is_percent_scale(state: State, config: dict, value: float) -> bool:
    """Decide whether a raw progress value is a 0-100 percent rather than a fraction.

    A fraction only ever exceeds 1.0 by rounding noise, so a clearly larger value
    is an unambiguous percent. Below that the value alone says nothing, so fall
    back to the entity's own unit -- but only when reading the entity's state,
    since the unit describes that, not some arbitrary attribute.

    Activity progress (content_mapper._get_progress) deliberately does NOT
    auto-detect: there a raw 1 is 1%, never a full bar. Keep the conventions
    separate -- see the note on that function.
    """
    scale = config.get(CONF_VALUE_SCALE, DEFAULT_VALUE_SCALE)
    if scale == VALUE_SCALE_PERCENT:
        return True
    if scale == VALUE_SCALE_FRACTION:
        return False
    if not config.get(CONF_VALUE_ATTRIBUTE) and state.attributes.get("unit_of_measurement") == "%":
        return True
    return value > 1.0 + FRACTION_OVERSHOOT_TOLERANCE


def _date_window(hass: HomeAssistant, state: State, config: dict) -> tuple[datetime, datetime] | None:
    """Resolve the start/end pair that makes a bar or countdown self-advance."""
    start = _read_datetime(hass, state, config, None, CONF_START_DATE_ATTRIBUTE)
    end = _read_datetime(hass, state, config, None, CONF_END_DATE_ATTRIBUTE)
    if start is None or end is None or start >= end:
        return None
    return start, end


def _map_progress(hass: HomeAssistant, state: State, config: dict, content: dict) -> dict | None:
    """progress template: 0.0-1.0 value, a date window, or both.

    A date pair advances the bar on device with no further pushes, so it stands
    in for the value; sending both is best, since older clients only read value.
    """
    window = _date_window(hass, state, config)
    if window is not None:
        content["start_date"] = _iso(window[0])
        content["end_date"] = _iso(window[1])

    value = _finite(read_numeric_value(state, config))
    if value is None:
        if window is None:
            _LOGGER.debug("Could not coerce progress value for %s; skipping update", state.entity_id)
            return None
        # Explicit null, not an omitted key: PATCH is an RFC 7396 merge, so
        # leaving the key out preserves whatever fraction was stored last. App
        # builds before 1.6 read only `value` and would render that stale bar
        # forever. Null deletes it, and every released build decodes the field
        # as optional (`value ?? 0`), so they fall back to an empty bar.
        content["value"] = None
        return content
    if _is_percent_scale(state, config, value):
        value = value / 100.0
    # Clamp to [0,1] so server validation never rejects the payload.
    content["value"] = max(0.0, min(1.0, value))
    return content


def _map_gauge(state: State, config: dict, content: dict, prev_value: float | None) -> dict | None:
    """gauge template: value + min/max required, value clamped to [min, max]."""
    value = _finite(read_numeric_value(state, config))
    if value is None:
        _LOGGER.debug("Could not coerce gauge value for %s; skipping update", state.entity_id)
        return None
    min_val = float(config.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE))
    max_val = float(config.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE))
    if min_val >= max_val:
        _LOGGER.warning(
            "Widget gauge for %s has min_value >= max_value (%s >= %s); skipping",
            state.entity_id,
            min_val,
            max_val,
        )
        return None
    clamped = max(min_val, min(max_val, value))
    content["value"] = clamped
    content["min_value"] = min_val
    content["max_value"] = max_val
    trend = _trend(clamped, prev_value)
    if trend:
        content["trend"] = trend
    return content


def _map_trend(state: State, config: dict, content: dict, points: list[float] | None) -> dict | None:
    """trend template: the current value plus a 2-48 point sparkline history.

    Chart bounds are optional here (the client auto-scales without them), unlike
    the gauge's required scale.
    """
    value = _finite(read_numeric_value(state, config))
    if value is None:
        _LOGGER.debug("Could not coerce trend value for %s; skipping update", state.entity_id)
        return None

    series = [p for p in (points or []) if _finite(_coerce_float(p)) is not None]
    if len(series) < WIDGET_MIN_TREND_POINTS:
        _LOGGER.debug(
            "Trend widget for %s has %d point(s); needs %d",
            state.entity_id,
            len(series),
            WIDGET_MIN_TREND_POINTS,
        )
        return None

    content["value"] = value
    content["points"] = [float(p) for p in series[-WIDGET_MAX_TREND_POINTS:]]

    min_val = _finite(_coerce_float(config.get(CONF_MIN_VALUE)))
    max_val = _finite(_coerce_float(config.get(CONF_MAX_VALUE)))
    if min_val is not None and max_val is not None and min_val >= max_val:
        _LOGGER.warning(
            "Trend widget for %s has min_value >= max_value (%s >= %s); ignoring both bounds",
            state.entity_id,
            min_val,
            max_val,
        )
        min_val = max_val = None
    if min_val is not None:
        content["min_value"] = min_val
    if max_val is not None:
        content["max_value"] = max_val
    return content


def _map_countdown(hass: HomeAssistant, state: State, config: dict, content: dict) -> dict | None:
    """countdown template: an end date, optionally paired with a start date.

    The end date comes from the configured attribute, then the domain's own
    convention (a timer's finishes_at, a calendar's end_time), then the entity
    state itself, which is what a `device_class: timestamp` sensor publishes.
    """
    attr_config = dict(config)
    if not attr_config.get(CONF_END_DATE_ATTRIBUTE):
        domain = state.entity_id.split(".")[0]
        default_attr = _END_DATE_DOMAIN_DEFAULTS.get(domain)
        if default_attr and default_attr in state.attributes:
            attr_config[CONF_END_DATE_ATTRIBUTE] = default_attr

    end = _read_datetime(hass, state, attr_config, None, CONF_END_DATE_ATTRIBUTE)
    if end is None:
        # Without a usable end date there is nothing to count to; returning None
        # leaves the last pushed content in place rather than blanking it.
        _LOGGER.debug("Countdown widget for %s has no usable end date", state.entity_id)
        return None
    content["end_date"] = _iso(end)

    start = _read_datetime(hass, state, config, None, CONF_START_DATE_ATTRIBUTE)
    if start is not None and start < end:
        content["start_date"] = _iso(start)

    expired = str(config.get(CONF_EXPIRED_TEXT, "") or "").strip()
    if expired:
        content["expired_text"] = _truncate(expired, WIDGET_EXPIRED_TEXT_MAX)
    return content


def _node_chrome(row: dict, out: dict) -> None:
    """Copy the icon/color pair a battery device and a flow node share."""
    icon = str(row.get(CONF_ICON, "") or "").strip()
    if icon:
        out["icon"] = _truncate(icon, WIDGET_NODE_ICON_MAX)
    color = color_to_str(row.get("color") or "")
    if color and is_valid_color(color):
        out["color"] = color


def _row_state(hass: HomeAssistant, row: dict) -> State | None:
    entity_id = row.get(CONF_ENTITY_ID)
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    return None if _is_unavailable(state) else state


def _companion_number(hass: HomeAssistant, row: dict, key: str) -> float | None:
    """Read a plain numeric state off a per-row companion entity."""
    entity_id = row.get(key)
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if _is_unavailable(state):
        return None
    assert state is not None
    return _finite(_coerce_float(state.state))


def _map_battery(hass: HomeAssistant, config: dict) -> dict | None:
    """battery template: up to 8 rings, each bound to a separate entity.

    Rows whose entity is unavailable or non-numeric are skipped, so one flat
    battery sensor never takes the whole widget down.
    """
    devices: list[dict] = []
    for row in config.get(CONF_BATTERY_DEVICES) or []:
        if not isinstance(row, dict):
            continue
        state = _row_state(hass, row)
        if state is None:
            continue
        level = _finite(read_numeric_value(state, row))
        if level is None:
            continue
        name = str(row.get(CONF_NODE_NAME, "") or "").strip()
        if not name:
            name = str(state.attributes.get("friendly_name") or state.entity_id)
        device: dict = {
            "name": _truncate(name, WIDGET_NODE_NAME_MAX),
            "level": max(0.0, min(100.0, level)),
        }
        charging = hass.states.get(row.get(CONF_CHARGING_ENTITY) or "")
        if charging is not None and charging.state == "on":
            device["charging"] = True
        _node_chrome(row, device)
        devices.append(device)
        if len(devices) >= WIDGET_MAX_BATTERY_DEVICES:
            break

    if not devices:
        return None
    content = _group_chrome(config)
    content["devices"] = devices
    return content


def _schedule_level(value: float, low_max: float | None, high_min: float | None) -> str:
    """Band a period value. Empty when the user configured no thresholds."""
    if low_max is None and high_min is None:
        return ""
    if low_max is not None and value <= low_max:
        return "low"
    if high_min is not None and value >= high_min:
        return "high"
    return "medium"


def _map_schedule(state: State, config: dict, content: dict) -> dict | None:
    """schedule template: hourly tariffs, delivery windows, shifts.

    Reads one or more attributes each holding a list of period dicts (the shape
    Nordpool and friends publish as raw_today / raw_tomorrow), concatenates them
    and normalizes to the server's strictly-increasing period list.
    """
    start_key = str(config.get(CONF_SCHEDULE_START_KEY) or DEFAULT_SCHEDULE_START_KEY)
    value_key = str(config.get(CONF_SCHEDULE_VALUE_KEY) or DEFAULT_SCHEDULE_VALUE_KEY)
    low_max = _finite(_coerce_float(config.get(CONF_SCHEDULE_LOW_MAX)))
    high_min = _finite(_coerce_float(config.get(CONF_SCHEDULE_HIGH_MIN)))

    raw_periods: list[dict] = []
    for attr in config.get(CONF_SCHEDULE_ATTRIBUTES) or []:
        source = state.attributes.get(attr)
        if isinstance(source, list):
            raw_periods.extend(item for item in source if isinstance(item, dict))

    by_start: dict[float, dict] = {}
    for item in raw_periods:
        when = _parse_widget_datetime(item.get(start_key))
        if when is None or not _widget_date_ok(when):
            continue
        value = _finite(_coerce_float(item.get(value_key)))
        if value is None:
            continue
        period: dict = {"start": _iso(when), "value": value}
        level = _schedule_level(value, low_max, high_min)
        if level:
            period["level"] = level
        # A duplicate start would break the strictly-increasing rule; last wins,
        # which is what a tomorrow-array overlapping today's tail intends.
        by_start[when.timestamp()] = period

    if not by_start:
        _LOGGER.debug("Schedule widget for %s produced no usable periods", state.entity_id)
        return None

    starts = sorted(by_start)
    ordered = [by_start[ts] for ts in starts]
    if len(ordered) > WIDGET_MAX_SCHEDULE_PERIODS:
        # Drop history rather than the future: keep the period covering now, so
        # the client still highlights the current band, plus everything after it.
        now = dt_util.utcnow().timestamp()
        first = 0
        for i, ts in enumerate(starts):
            if ts <= now:
                first = i
        ordered = ordered[first : first + WIDGET_MAX_SCHEDULE_PERIODS]

    content["periods"] = ordered
    return content


def _flow_node(hass: HomeAssistant, row: dict) -> dict | None:
    """Build one flow node, or None when its entity has nothing usable."""
    state = _row_state(hass, row)
    if state is None:
        return None
    rate = _finite(read_numeric_value(state, row))
    if rate is None:
        return None

    node: dict = {"rate": rate}
    name = str(row.get(CONF_NODE_NAME, "") or "").strip()
    if name:
        node["name"] = _truncate(name, WIDGET_NODE_NAME_MAX)

    total = _companion_number(hass, row, CONF_TOTAL_ENTITY)
    # The server rejects a negative total outright; a meter reading below zero is
    # a broken sensor, not a value worth failing the whole push over.
    if total is not None and total >= 0:
        node["total"] = total

    level = _companion_number(hass, row, CONF_LEVEL_ENTITY)
    if level is not None:
        node["level"] = max(0.0, min(100.0, level))

    _node_chrome(row, node)
    return node


def _map_flow(hass: HomeAssistant, config: dict) -> dict | None:
    """flow template: production in, storage, exchange, consumption out.

    Domain-agnostic by design: energy is the motivating case, but water, data
    and money use the same four slots. Sign conventions are the caller's: an
    exchange rate is positive inbound, a storage rate positive while filling.
    """
    flow: dict = {}
    inputs: list[dict] = []
    for row in config.get(CONF_FLOW_NODES) or []:
        if not isinstance(row, dict):
            continue
        slot = str(row.get(CONF_FLOW_SLOT, "") or "").strip().lower()
        node = _flow_node(hass, row)
        if node is None:
            continue
        if slot == FLOW_SLOT_INPUT:
            if len(inputs) < WIDGET_MAX_FLOW_INPUTS:
                inputs.append(node)
        elif slot and slot not in flow:
            flow[slot] = node

    if inputs:
        flow["inputs"] = inputs
    if not flow:
        return None

    content = _group_chrome(config)
    unit = str(config.get(CONF_UNIT, "") or "")
    if unit:
        content["unit"] = _truncate(unit, WIDGET_UNIT_MAX)
    content["flow"] = flow
    return content


def _map_status_static(config: dict) -> dict:
    """status template fallback used when the bound entity is unavailable.

    Emits severity + the user-configured static label/icon/accent so the iOS
    widget still shows something useful while HA is reporting unknown state.
    """
    content: dict = {}

    severity = str(config.get(CONF_SEVERITY, "") or "")
    if severity:
        content["severity"] = severity

    label_config = str(config.get(CONF_LABEL, "") or "")
    if label_config:
        content["label"] = _truncate(label_config, WIDGET_LABEL_MAX)
    icon = config.get(CONF_ICON) or ""
    if icon:
        content["icon"] = str(icon)
    _apply_static_color(content, config, CONF_ACCENT_COLOR, "accent_color")
    add_tap_action(content, config)
    return content


def _map_stat_list(hass: HomeAssistant, config: dict) -> dict | None:
    """stat_list: 1-WIDGET_MAX_STAT_ROWS rows, each binding to a separate HA entity.

    Each row dict: {label, entity_id, value_attribute?, unit?, timer_style?}. Rows
    with unavailable entities or empty values are skipped silently. A row whose
    value parses as a date and carries a timer_style also ships a timer slot; the
    string value stays as the fallback older clients render.
    """
    rows_out: list[dict] = []
    for row in config.get(CONF_STAT_ROWS) or []:
        if not isinstance(row, dict):
            continue
        entity_id = row.get(CONF_ENTITY_ID)
        label = _truncate(str(row.get(CONF_LABEL, "") or ""), WIDGET_STAT_LABEL_MAX)
        if not entity_id or not label:
            continue
        state = hass.states.get(entity_id)
        if _is_unavailable(state):
            continue
        attr = row.get(CONF_VALUE_ATTRIBUTE)
        raw = state.attributes.get(attr) if attr else state.state
        if raw in (None, ""):
            continue
        out: dict = {"label": label, "value": _truncate(str(raw), WIDGET_STAT_VALUE_MAX)}
        unit = row.get(CONF_UNIT)
        if unit:
            out["unit"] = _truncate(str(unit), WIDGET_STAT_UNIT_MAX)
        style = str(row.get("timer_style", "") or "").strip().lower()
        if style in TIMER_STYLES:
            when = _parse_widget_datetime(raw)
            if when is not None and _widget_date_ok(when):
                out["timer"] = {"date": _iso(when), "style": style}
        rows_out.append(out)
        if len(rows_out) >= WIDGET_MAX_STAT_ROWS:
            break

    if not rows_out:
        return None

    content = _group_chrome(config)
    content["stat_rows"] = rows_out
    return content


def widget_name_from_config(config: dict, hass: HomeAssistant | None = None) -> str:
    """Resolve the widget name for POST /widgets create."""
    name = str(config.get(CONF_WIDGET_NAME, "") or "").strip()
    if name:
        return _truncate(name, WIDGET_NAME_MAX)
    entity_id = config.get(CONF_ENTITY_ID)
    if hass and entity_id:
        state = hass.states.get(entity_id)
        if state is not None:
            friendly = state.attributes.get("friendly_name")
            if friendly:
                return _truncate(str(friendly), WIDGET_NAME_MAX)
    return _truncate(str(entity_id or "PushWard widget"), WIDGET_NAME_MAX)


# Dispatch for the templates with no anchoring entity. Keyed by
# WIDGET_GROUP_TEMPLATES; a drift guard in the tests keeps the two in step.
_GROUP_MAPPERS = {
    WIDGET_TEMPLATE_STAT_LIST: _map_stat_list,
    WIDGET_TEMPLATE_BATTERY: _map_battery,
    WIDGET_TEMPLATE_FLOW: _map_flow,
}
