"""Map Home Assistant state/attributes to PushWard widget content."""

from __future__ import annotations

import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State

from .const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_BACKGROUND_COLOR,
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_LABEL,
    CONF_LABEL_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_SEVERITY,
    CONF_STAT_ROWS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_TEXT_COLOR,
    CONF_UNIT,
    CONF_VALUE_ATTRIBUTE,
    CONF_VALUE_SCALE,
    CONF_WIDGET_NAME,
    CONF_WIDGET_TEMPLATE,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    DEFAULT_VALUE_SCALE,
    FRACTION_OVERSHOOT_TOLERANCE,
    VALUE_SCALE_FRACTION,
    VALUE_SCALE_PERCENT,
    WIDGET_LABEL_MAX,
    WIDGET_MAX_STAT_ROWS,
    WIDGET_NAME_MAX,
    WIDGET_STAT_LABEL_MAX,
    WIDGET_STAT_UNIT_MAX,
    WIDGET_STAT_VALUE_MAX,
    WIDGET_SUBTITLE_MAX,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_PROGRESS,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_STATUS,
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TREND_DOWN,
    WIDGET_TREND_FLAT,
    WIDGET_TREND_UP,
    WIDGET_UNIT_MAX,
)
from .content_mapper import add_tap_action, color_to_str, resolve_color, resolve_icon

_LOGGER = logging.getLogger(__name__)


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


def _read_value(state: State, config: dict) -> object:
    """Return raw value from attribute (if configured) or entity state."""
    attr = config.get(CONF_VALUE_ATTRIBUTE)
    if attr:
        return state.attributes.get(attr)
    return state.state


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


def map_widget_content(
    hass: HomeAssistant,
    config: dict,
    *,
    prev_value: float | None = None,
    registry_icon: str | None = None,
) -> dict | None:
    """Render a WidgetContent dict from HA state + widget config.

    Returns None when the configuration cannot produce a valid payload yet
    (e.g. progress/gauge with no usable numeric state). Caller decides
    whether to skip the request entirely or send a placeholder.
    """
    template = config.get(CONF_WIDGET_TEMPLATE)
    if not template:
        return None

    if template == WIDGET_TEMPLATE_STAT_LIST:
        return _map_stat_list(hass, config)

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

    if template == WIDGET_TEMPLATE_VALUE:
        return _map_value(state, config, content, prev_value)

    if template == WIDGET_TEMPLATE_PROGRESS:
        return _map_progress(state, config, content)

    if template == WIDGET_TEMPLATE_GAUGE:
        return _map_gauge(state, config, content, prev_value)

    if template == WIDGET_TEMPLATE_STATUS:
        severity = str(config.get(CONF_SEVERITY, "") or "")
        if severity:
            content["severity"] = severity
        return content

    _LOGGER.debug("Unknown widget template %r", template)
    return None


def _map_value(state: State, config: dict, content: dict, prev_value: float | None) -> dict:
    """value template: optional numeric value with auto-derived trend."""
    raw = _read_value(state, config)
    value = _coerce_float(raw)
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


def _map_progress(state: State, config: dict, content: dict) -> dict | None:
    """progress template: value 0.0-1.0 required, 0-100 sensors rescaled."""
    value = _coerce_float(_read_value(state, config))
    if value is None:
        _LOGGER.debug("Could not coerce progress value for %s; skipping update", state.entity_id)
        return None
    if _is_percent_scale(state, config, value):
        value = value / 100.0
    # Clamp to [0,1] so server validation never rejects the payload.
    value = max(0.0, min(1.0, value))
    content["value"] = value
    return content


def _map_gauge(state: State, config: dict, content: dict, prev_value: float | None) -> dict | None:
    """gauge template: value + min/max required, value clamped to [min, max]."""
    value = _coerce_float(_read_value(state, config))
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


def _apply_static_color(content: dict, config: dict, conf_key: str, out_key: str) -> None:
    raw = config.get(conf_key) or ""
    if not raw:
        return
    sanitized = color_to_str(raw)
    if sanitized:
        content[out_key] = sanitized


_STAT_LIST_COLORS = (
    (CONF_ACCENT_COLOR, "accent_color"),
    (CONF_BACKGROUND_COLOR, "background_color"),
    (CONF_TEXT_COLOR, "text_color"),
)


def _map_stat_list(hass: HomeAssistant, config: dict) -> dict | None:
    """stat_list: 1-WIDGET_MAX_STAT_ROWS rows, each binding to a separate HA entity.

    Each row dict: {label, entity_id, value_attribute?, unit?}. Rows with
    unavailable entities or empty values are skipped silently.
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
        rows_out.append(out)
        if len(rows_out) >= WIDGET_MAX_STAT_ROWS:
            break

    if not rows_out:
        return None

    content: dict = {"stat_rows": rows_out}
    # Cosmetic fields apply to the row group: no single state anchors attribute overrides.
    icon = config.get(CONF_ICON) or ""
    if icon:
        content["icon"] = icon
    label = str(config.get(CONF_LABEL, "") or "")
    if label:
        content["label"] = _truncate(label, WIDGET_LABEL_MAX)
    for conf_key, out_key in _STAT_LIST_COLORS:
        _apply_static_color(content, config, conf_key, out_key)
    add_tap_action(content, config)
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
