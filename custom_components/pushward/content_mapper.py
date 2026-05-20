"""Map Home Assistant state/attributes to PushWard content."""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util.color import (
    color_hs_to_RGB,
    color_temperature_to_rgb,
    color_xy_to_RGB,
)

from .const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ALARM,
    CONF_SNOOZE_SECONDS,
    CONF_BACKGROUND_COLOR,
    CONF_BACKGROUND_COLOR_ATTRIBUTE,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_DECIMALS,
    CONF_FIRED_AT_ATTRIBUTE,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SCALE,
    CONF_SECONDARY_URL,
    CONF_SECONDARY_URL_FOREGROUND,
    CONF_SECONDARY_URL_TITLE,
    CONF_SERIES,
    CONF_SEVERITY,
    CONF_SMOOTHING,
    CONF_STATE_LABELS,
    CONF_STEP_LABELS,
    CONF_STEP_ROWS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_TAP_ACTION_FOREGROUND,
    CONF_TAP_ACTION_URL,
    CONF_TEMPLATE,
    CONF_TEXT_COLOR,
    CONF_TEXT_COLOR_ATTRIBUTE,
    CONF_THRESHOLDS,
    CONF_TOTAL_STEPS,
    CONF_UNIT,
    CONF_UNITS,
    CONF_URL,
    CONF_URL_FOREGROUND,
    CONF_URL_TITLE,
    CONF_VALUE_ATTRIBUTE,
    CONF_WARNING_THRESHOLD,
    DEFAULT_DECIMALS,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    DEFAULT_SEVERITY,
    DEFAULT_TAP_ACTION_FOREGROUND,
    DEFAULT_TOTAL_STEPS,
    DEVICE_CLASS_ICONS,
    DOMAIN_DEFAULTS,
    normalize_slug,
)

_LOGGER = logging.getLogger(__name__)

# Timeline display fields carried forward to completion content.
# Keep in sync with the fields emitted in map_content's timeline branch.
_TIMELINE_CARRY_FIELDS = ("unit", "scale", "decimals", "smoothing", "thresholds", "units")

# Common (all-template) fields carried from last_content into completion.
_COMMON_CARRY_FIELDS = ("background_color", "text_color")

# Mirrors server-side ValidateColor: 6- or 8-digit hex with optional '#', or
# one of the server's allowlisted named colors. Must stay in sync with
# pushward-server/internal/model/activity.go:validNamedColors.
_COLOR_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")
_COLOR_NAMED = frozenset(
    {
        "red",
        "orange",
        "yellow",
        "green",
        "blue",
        "purple",
        "pink",
        "indigo",
        "teal",
        "cyan",
        "mint",
        "brown",
    }
)


def sanitize_slug(entity_id: str) -> str:
    """Convert an HA entity_id to a PushWard slug.

    sensor.washing_machine_status -> ha-sensor-washing-machine-status
    """
    return f"ha-{normalize_slug(entity_id)}"


def color_to_str(value: object) -> str:
    """Convert an HA color attribute to a string the API accepts.

    Handles rgb_color (3-tuple), rgbw/rgbww (4/5-tuple, takes RGB),
    xy_color (2-tuple 0-1), hs_color (2-tuple hue 0-360, sat 0-100),
    color_temp_kelvin (int), and plain named-color / hex strings.

    Returns "" for anything unrecognized so callers fall back to the default
    accent instead of emitting a garbage color string the server will reject.
    """
    if isinstance(value, (list, tuple)):
        try:
            if len(value) >= 3:
                r, g, b = int(value[0]), int(value[1]), int(value[2])
                return f"#{r:02x}{g:02x}{b:02x}"
            if len(value) == 2:
                a, b_val = float(value[0]), float(value[1])
                if a <= 1.0 and b_val <= 1.0:
                    r, g, b = color_xy_to_RGB(a, b_val)
                else:
                    r, g, b = color_hs_to_RGB(a, b_val)
                return f"#{r:02x}{g:02x}{b:02x}"
        except (TypeError, ValueError):
            return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            rf, gf, bf = color_temperature_to_rgb(float(value))
            return f"#{int(rf):02x}{int(gf):02x}{int(bf):02x}"
        except (TypeError, ValueError):
            return ""
    if isinstance(value, str):
        stripped = value.strip()
        if _COLOR_HEX_RE.match(stripped) or stripped.lower() in _COLOR_NAMED:
            return stripped
    return ""


def resolve_color(state: State, config: dict, static_key: str, attr_key: str) -> str:
    """Resolve a color value: dynamic attribute > static config > empty string."""
    attr_name = config.get(attr_key)
    if attr_name:
        raw = state.attributes.get(attr_name)
        if raw:
            resolved = color_to_str(raw)
            if resolved:
                return resolved
    return config.get(static_key, "")


def build_tap_action(url: str, foreground: bool, title: str = "") -> dict | None:
    """Build a server-side TapAction dict from URL + foreground + optional button title.

    Returns None when url is empty. For http(s) URLs with foreground=False,
    auto-injects ``method="POST"`` so iOS treats it as a silent webhook
    (without an HTTP shape, iOS drops the tap per pushward-server fa4a98f).
    Custom-scheme URLs (e.g. ``homeassistant://``) ignore foreground.
    """
    url = (url or "").strip()
    if not url:
        return None
    action: dict = {"url": url, "foreground": bool(foreground)}
    if not foreground:
        scheme = urlparse(url).scheme.lower()
        if scheme in ("http", "https"):
            action["method"] = "POST"
    title = (title or "").strip()
    if title:
        action["title"] = title
    return action


def add_tap_action(content: dict, config: dict, *, key: str = "tap_action") -> None:
    """Attach a structured tap-action to content[key] from CONF_TAP_ACTION_URL/FOREGROUND.

    Shared between activity and widget mappers — the widget-wide tap target has
    the same wire shape on both surfaces.
    """
    action = build_tap_action(
        config.get(CONF_TAP_ACTION_URL, ""),
        config.get(CONF_TAP_ACTION_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND),
    )
    if action is not None:
        content[key] = action


# (url_action_slot_key, url_config_key, foreground_config_key, title_config_key)
_BUTTON_SLOTS: tuple[tuple[str, str, str, str], ...] = (
    ("url_action", CONF_URL, CONF_URL_FOREGROUND, CONF_URL_TITLE),
    ("secondary_url_action", CONF_SECONDARY_URL, CONF_SECONDARY_URL_FOREGROUND, CONF_SECONDARY_URL_TITLE),
)


def _add_tap_actions(content: dict, entity_config: dict) -> None:
    """Add structured tap_action / url_action / secondary_url_action to content.

    tap_action is universal (every template); url_action and secondary_url_action
    are emitted only for steps/alert templates that render button affordances.
    """
    add_tap_action(content, entity_config)

    if entity_config.get(CONF_TEMPLATE, "generic") not in ("steps", "alert"):
        return

    for slot_key, url_key, foreground_key, title_key in _BUTTON_SLOTS:
        action = build_tap_action(
            entity_config.get(url_key, ""),
            entity_config.get(foreground_key, DEFAULT_TAP_ACTION_FOREGROUND),
            entity_config.get(title_key, ""),
        )
        if action is not None:
            content[slot_key] = action


def _resolve_device_class_icon(domain: str, device_class: str) -> str:
    """Resolve MDI icon from entity domain + device_class.

    Modern HA integrations use frontend-only icon translations, so
    state.attributes["icon"] and entity_registry.original_icon are empty.
    This mirrors the HA frontend's device-class icon tables.
    The ``number`` domain shares sensor device-class icons.
    """
    if not domain or not device_class:
        return ""
    icon = DEVICE_CLASS_ICONS.get(f"{domain}.{device_class}", "")
    if not icon and domain == "number":
        icon = DEVICE_CLASS_ICONS.get(f"sensor.{device_class}", "")
    return icon


def lookup_registry_icon(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """Return the entity registry's icon for entity_id, or None."""
    if not entity_id:
        return None
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None:
        return None
    return entry.icon or entry.original_icon or None


def resolve_icon(state: State, config: dict, registry_icon: str | None = None) -> str:
    """Resolve an entity's display icon via a 6-level fallback chain.

    Order: icon_attribute (dynamic from HA attribute) → static CONF_ICON
    (user-configured) → state.attributes["icon"] (legacy / _attr_icon) →
    entity registry icon → device_class icon (mirrors HA frontend tables)
    → domain default. Returns "" only when every step yields nothing,
    which is rare given the domain-default fallback.
    """
    icon = ""
    icon_attr = config.get(CONF_ICON_ATTRIBUTE)
    if icon_attr:
        dynamic_icon = state.attributes.get(icon_attr)
        if dynamic_icon:
            icon = str(dynamic_icon)
    if not icon:
        icon = config.get(CONF_ICON, "") or ""
    if not icon:
        entity_icon = state.attributes.get("icon")
        if entity_icon:
            icon = str(entity_icon)
    if not icon and registry_icon:
        icon = registry_icon
    if not icon:
        device_class = state.attributes.get("device_class", "")
        icon = _resolve_device_class_icon(state.domain, device_class)
    if not icon:
        icon = get_domain_defaults(state.domain)["icon"]
    return icon


def map_content(state: State, entity_config: dict, *, registry_icon: str | None = None) -> dict:
    """Map HA state + attributes to a PushWard content dict."""
    # State label: use custom label if configured, else default formatting
    state_labels = entity_config.get(CONF_STATE_LABELS) or {}
    if state.state in state_labels:
        state_text = state_labels[state.state]
    else:
        state_text = state.state.replace("_", " ").capitalize()

    icon = resolve_icon(state, entity_config, registry_icon=registry_icon)

    # Subtitle: subtitle_attribute > friendly_name
    subtitle_attr = entity_config.get(CONF_SUBTITLE_ATTRIBUTE)
    if subtitle_attr:
        raw = state.attributes.get(subtitle_attr)
        subtitle = str(raw) if raw is not None else state.attributes.get("friendly_name", "")
    else:
        subtitle = state.attributes.get("friendly_name", "")

    # Accent color resolution: accent_color_attribute > static accent_color > "blue"
    accent = resolve_color(state, entity_config, CONF_ACCENT_COLOR, CONF_ACCENT_COLOR_ATTRIBUTE) or "blue"

    background_color = resolve_color(state, entity_config, CONF_BACKGROUND_COLOR, CONF_BACKGROUND_COLOR_ATTRIBUTE)
    text_color = resolve_color(state, entity_config, CONF_TEXT_COLOR, CONF_TEXT_COLOR_ATTRIBUTE)

    content: dict = {
        "template": entity_config.get(CONF_TEMPLATE, "generic"),
        "progress": _get_progress(state, entity_config),
        "state": state_text,
        "icon": icon,
        "subtitle": subtitle,
        "accent_color": accent,
    }

    if background_color:
        content["background_color"] = background_color
    if text_color:
        content["text_color"] = text_color

    remaining = _get_remaining_time(state, entity_config)
    if remaining is not None:
        content["remaining_time"] = remaining

    _add_tap_actions(content, entity_config)

    # Template-specific required fields
    template = content["template"]
    if template == "countdown":
        now = int(time.time())
        content["end_date"] = now + (remaining if remaining is not None else 0)
        completion_msg = entity_config.get(CONF_COMPLETION_MESSAGE)
        if completion_msg:
            content["completion_message"] = completion_msg
        if remaining is not None:
            content["start_date"] = now
        warning_threshold = entity_config.get(CONF_WARNING_THRESHOLD)
        if warning_threshold is not None:
            content["warning_threshold"] = int(warning_threshold)
        if entity_config.get(CONF_ALARM):
            content["alarm"] = True
            snooze_seconds = entity_config.get(CONF_SNOOZE_SECONDS)
            if snooze_seconds is not None:
                content["snooze_seconds"] = int(snooze_seconds)
    elif template == "steps":
        total = entity_config.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)
        current = _get_current_step(state, entity_config)
        content["total_steps"] = total
        content["current_step"] = current
        # Auto-derive progress when no explicit progress_attribute is configured
        if not entity_config.get(CONF_PROGRESS_ATTRIBUTE) and total > 0:
            content["progress"] = max(0.0, min(1.0, current / total))
        step_labels = entity_config.get(CONF_STEP_LABELS) or {}
        if step_labels:
            labels_list = [step_labels.get(str(i), "") for i in range(1, total + 1)]
            if any(labels_list):
                content["step_labels"] = labels_list
        step_rows = entity_config.get(CONF_STEP_ROWS) or []
        if len(step_rows) == total:
            content["step_rows"] = [max(1, min(10, int(r))) for r in step_rows]
    elif template == "alert":
        content["severity"] = entity_config.get(CONF_SEVERITY, DEFAULT_SEVERITY)
        fired_at_attr = entity_config.get(CONF_FIRED_AT_ATTRIBUTE)
        if fired_at_attr:
            raw_fired_at = state.attributes.get(fired_at_attr)
            if raw_fired_at is not None:
                try:
                    content["fired_at"] = int(float(raw_fired_at))
                except (ValueError, TypeError):
                    _LOGGER.debug("Could not parse fired_at attribute %s for %s", fired_at_attr, state.entity_id)
    elif template == "gauge":
        min_val, max_val = _gauge_base_fields(content, entity_config)
        value = _get_gauge_value(state, entity_config)
        value = max(min_val, min(max_val, value))
        content["value"] = value
        if max_val > min_val:
            content["progress"] = (value - min_val) / (max_val - min_val)
        else:
            content["progress"] = 1.0
    elif template == "timeline":
        values = _get_timeline_values(state, entity_config)
        if values:
            content["value"] = values
        unit = entity_config.get(CONF_UNIT, "")
        if unit:
            content["unit"] = unit
        scale = entity_config.get(CONF_SCALE, "")
        if scale and scale != "linear":
            content["scale"] = scale
        decimals = entity_config.get(CONF_DECIMALS)
        if decimals is not None and decimals != DEFAULT_DECIMALS:
            content["decimals"] = decimals
        smoothing = entity_config.get(CONF_SMOOTHING)
        if smoothing:
            content["smoothing"] = smoothing
        thresholds = entity_config.get(CONF_THRESHOLDS, [])
        if thresholds:
            content["thresholds"] = thresholds
        units = entity_config.get(CONF_UNITS) or {}
        if units:
            content["units"] = units
        content["progress"] = 0.0

    return content


def map_completion_content(entity_config: dict, last_content: dict | None = None) -> dict:
    """Build content for the "Complete" phase of two-phase end.

    Preserves progress and subtitle from the last live update so the end
    screen reflects the actual value (e.g. lamp brightness) rather than
    jumping to 100%.
    """
    completion_msg = entity_config.get(CONF_COMPLETION_MESSAGE) or "Complete"

    content: dict = {
        "template": entity_config.get(CONF_TEMPLATE, "generic"),
        "progress": last_content.get("progress", 1.0) if last_content else 1.0,
        "state": completion_msg,
        "icon": "checkmark.circle.fill",
        "subtitle": last_content.get("subtitle", "") if last_content else "",
        "accent_color": "green",
    }

    _add_tap_actions(content, entity_config)

    # Template-specific required fields for server validation
    template = content["template"]
    if template == "countdown":
        content["end_date"] = int(time.time())
    elif template == "steps":
        total = entity_config.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)
        content["total_steps"] = total
        content["current_step"] = total
        content["progress"] = 1.0
    elif template == "alert":
        content["severity"] = entity_config.get(CONF_SEVERITY, DEFAULT_SEVERITY)
    elif template == "gauge":
        _, max_val = _gauge_base_fields(content, entity_config)
        if last_content and "value" in last_content:
            content["value"] = last_content["value"]
        else:
            content["value"] = max_val
            content["progress"] = 1.0
    elif template == "timeline":
        if last_content and "value" in last_content:
            content["value"] = last_content["value"]
        for key in _TIMELINE_CARRY_FIELDS:
            if last_content and key in last_content:
                content[key] = last_content[key]

    if last_content:
        for key in _COMMON_CARRY_FIELDS:
            if key in last_content:
                content[key] = last_content[key]

    return content


def _gauge_base_fields(content: dict, entity_config: dict) -> tuple[float, float]:
    """Set shared gauge fields (min_value, max_value, unit) on content and return the range."""
    min_val = entity_config.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE)
    max_val = entity_config.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE)
    content["min_value"] = min_val
    content["max_value"] = max_val
    unit = entity_config.get(CONF_UNIT, "")
    if unit:
        content["unit"] = unit
    return min_val, max_val


def get_domain_defaults(domain: str) -> dict:
    """Return default icon, start_states, and end_states for an HA domain."""
    return DOMAIN_DEFAULTS.get(
        domain,
        {"icon": "mdi:eye", "start_states": [], "end_states": []},
    )


_ATTRS_0_255 = frozenset({"brightness"})


def _rescale_attr(value: float, attr_name: str) -> float:
    """Rescale 0-255 attributes (e.g. brightness) to 0-100."""
    if attr_name in _ATTRS_0_255:
        return round(value / 255.0 * 100.0)
    return value


def _get_progress(state: State, entity_config: dict) -> float:
    """Extract progress from entity attributes, clamped to 0.0-1.0.

    Attributes in the 0-255 range (e.g. brightness) are divided by 255;
    all others are treated as 0-100 percentages.
    """
    attr_name = entity_config.get(CONF_PROGRESS_ATTRIBUTE)
    if not attr_name:
        return 0.0
    try:
        value = float(state.attributes.get(attr_name, 0))
        scale = 255.0 if attr_name in _ATTRS_0_255 else 100.0
        return round(max(0.0, min(1.0, value / scale)), 2)
    except (ValueError, TypeError):
        _LOGGER.debug("Could not parse progress attribute %s for %s", attr_name, state.entity_id)
        return 0.0


def _get_current_step(state: State, entity_config: dict) -> int:
    """Extract current step from entity attributes, clamped to 0..total_steps."""
    attr_name = entity_config.get(CONF_CURRENT_STEP_ATTR)
    total = entity_config.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)
    if not attr_name:
        return 0
    try:
        value = int(state.attributes.get(attr_name, 0))
        return max(0, min(total, value))
    except (ValueError, TypeError):
        _LOGGER.debug("Could not parse current_step attribute %s for %s", attr_name, state.entity_id)
        return 0


def _get_remaining_time(state: State, entity_config: dict) -> int | None:
    """Extract remaining time in seconds from entity attributes."""
    attr_name = entity_config.get(CONF_REMAINING_TIME_ATTR)
    if not attr_name:
        return None
    try:
        return int(state.attributes.get(attr_name, 0))
    except (ValueError, TypeError):
        _LOGGER.debug("Could not parse remaining_time attribute %s for %s", attr_name, state.entity_id)
        return None


def _get_timeline_values(state: State, entity_config: dict) -> dict[str, float]:
    """Extract labeled value map for timeline template.

    Multi-series: reads from CONF_SERIES attribute->label mapping.
    Single-series fallback: CONF_VALUE_ATTRIBUTE or entity state with friendly_name label.
    """
    series_map = entity_config.get(CONF_SERIES) or {}
    if series_map:
        values: dict[str, float] = {}
        for attr_name, label in series_map.items():
            raw = state.attributes.get(attr_name)
            if raw is not None:
                try:
                    values[label] = _rescale_attr(float(raw), attr_name)
                except (ValueError, TypeError):
                    _LOGGER.debug(
                        "Could not parse timeline series attribute %s for %s",
                        attr_name,
                        state.entity_id,
                    )
        return values

    # Single series: value_attribute or entity state
    label = state.attributes.get("friendly_name", state.entity_id)
    attr_name = entity_config.get(CONF_VALUE_ATTRIBUTE)
    if attr_name:
        raw = state.attributes.get(attr_name)
        if raw is None:
            return {}
        try:
            return {label: _rescale_attr(float(raw), attr_name)}
        except (ValueError, TypeError):
            _LOGGER.debug(
                "Could not parse timeline value attribute %s for %s",
                attr_name,
                state.entity_id,
            )
            return {}
    try:
        return {label: float(state.state)}
    except (ValueError, TypeError):
        _LOGGER.debug("Could not parse entity state as timeline value for %s", state.entity_id)
        return {}


def _get_gauge_value(state: State, entity_config: dict) -> float:
    """Extract gauge value from entity attribute or state.

    When value_attribute is configured, reads from that attribute.
    Otherwise falls back to the entity's primary state.
    Attributes in the 0-255 range (e.g. brightness) are rescaled to 0-100.
    """
    attr_name = entity_config.get(CONF_VALUE_ATTRIBUTE)
    if attr_name:
        try:
            return _rescale_attr(float(state.attributes.get(attr_name, 0)), attr_name)
        except (ValueError, TypeError):
            _LOGGER.debug("Could not parse gauge value attribute %s for %s", attr_name, state.entity_id)
            return 0.0
    try:
        return float(state.state)
    except (ValueError, TypeError):
        _LOGGER.debug("Could not parse entity state as gauge value for %s", state.entity_id)
        return 0.0
