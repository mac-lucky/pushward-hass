"""Map Home Assistant state/attributes to PushWard content."""

from __future__ import annotations

import logging
import time

from homeassistant.core import State
from homeassistant.util.color import (
    color_hs_to_RGB,
    color_temperature_to_rgb,
    color_xy_to_RGB,
)

from .const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SECONDARY_URL,
    CONF_SEVERITY,
    CONF_STATE_LABELS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_TEMPLATE,
    CONF_TOTAL_STEPS,
    CONF_UNIT,
    CONF_URL,
    CONF_VALUE_ATTRIBUTE,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    DEFAULT_SEVERITY,
    DEFAULT_TOTAL_STEPS,
    DEVICE_CLASS_ICONS,
    DOMAIN_DEFAULTS,
    normalize_slug,
)

_LOGGER = logging.getLogger(__name__)


def sanitize_slug(entity_id: str) -> str:
    """Convert an HA entity_id to a PushWard slug.

    sensor.washing_machine_status -> ha-sensor-washing-machine-status
    """
    return f"ha-{normalize_slug(entity_id)}"


def _color_to_str(value: object) -> str:
    """Convert an HA color attribute to a string the API accepts.

    Handles rgb_color (3-tuple), rgbw/rgbww (4/5-tuple, takes RGB),
    xy_color (2-tuple 0-1), hs_color (2-tuple hue 0-360, sat 0-100),
    color_temp_kelvin (int), and plain strings/named colors.
    """
    if isinstance(value, (list, tuple)):
        if len(value) >= 3:
            # rgb_color, rgbw_color, rgbww_color — take first 3 as RGB
            r, g, b = int(value[0]), int(value[1]), int(value[2])
            return f"#{r:02x}{g:02x}{b:02x}"
        if len(value) == 2:
            a, b_val = float(value[0]), float(value[1])
            if a <= 1.0 and b_val <= 1.0:
                # Heuristic: values in 0.0-1.0 range → CIE XY color.
                # HS would need hue ≤ 1° and sat ≤ 1% (near-white), which is
                # unrealistic in practice. Users map to typed HA attributes
                # (xy_color / hs_color) anyway.
                r, g, b = color_xy_to_RGB(a, b_val)
            else:
                # hs_color (hue 0-360, saturation 0-100)
                r, g, b = color_hs_to_RGB(a, b_val)
            return f"#{r:02x}{g:02x}{b:02x}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # color_temp_kelvin
        rf, gf, bf = color_temperature_to_rgb(float(value))
        return f"#{int(rf):02x}{int(gf):02x}{int(bf):02x}"
    return str(value)


def _add_url_deeplinks(content: dict, entity_config: dict) -> None:
    """Add URL deep-link fields to content when configured."""
    url = entity_config.get(CONF_URL, "")
    if url:
        content["url"] = url
    secondary_url = entity_config.get(CONF_SECONDARY_URL, "")
    if secondary_url:
        content["secondary_url"] = secondary_url


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


def map_content(state: State, entity_config: dict, *, registry_icon: str | None = None) -> dict:
    """Map HA state + attributes to a PushWard content dict."""
    # State label: use custom label if configured, else default formatting
    state_labels = entity_config.get(CONF_STATE_LABELS) or {}
    if state.state in state_labels:
        state_text = state_labels[state.state]
    else:
        state_text = state.state.replace("_", " ").capitalize()

    # Icon resolution order:
    # 1. icon_attribute (dynamic from HA attribute)
    # 2. static CONF_ICON (user-configured in PushWard)
    # 3. state.attributes["icon"] (legacy integrations / _attr_icon)
    # 4. entity registry icon (user-customized or platform-provided)
    # 5. device_class icon (mirrors HA frontend tables)
    # 6. domain default
    icon = ""
    icon_attr = entity_config.get(CONF_ICON_ATTRIBUTE)
    if icon_attr:
        dynamic_icon = state.attributes.get(icon_attr)
        if dynamic_icon:
            icon = str(dynamic_icon)
    if not icon:
        icon = entity_config.get(CONF_ICON, "")
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

    # Subtitle: subtitle_attribute > friendly_name
    subtitle_attr = entity_config.get(CONF_SUBTITLE_ATTRIBUTE)
    if subtitle_attr:
        raw = state.attributes.get(subtitle_attr)
        subtitle = str(raw) if raw is not None else state.attributes.get("friendly_name", "")
    else:
        subtitle = state.attributes.get("friendly_name", "")

    # Accent color resolution: accent_color_attribute > static accent_color > "blue"
    accent = entity_config.get(CONF_ACCENT_COLOR, "")
    color_attr = entity_config.get(CONF_ACCENT_COLOR_ATTRIBUTE)
    if color_attr:
        dynamic_color = state.attributes.get(color_attr)
        if dynamic_color:
            accent = _color_to_str(dynamic_color)
    if not accent:
        accent = "blue"

    content: dict = {
        "template": entity_config.get(CONF_TEMPLATE, "generic"),
        "progress": _get_progress(state, entity_config),
        "state": state_text,
        "icon": icon,
        "subtitle": subtitle,
        "accent_color": accent,
    }

    remaining = _get_remaining_time(state, entity_config)
    if remaining is not None:
        content["remaining_time"] = remaining

    _add_url_deeplinks(content, entity_config)

    # Template-specific required fields
    template = content["template"]
    if template == "countdown":
        content["end_date"] = int(time.time()) + (remaining if remaining is not None else 0)
        completion_msg = entity_config.get(CONF_COMPLETION_MESSAGE)
        if completion_msg:
            content["completion_message"] = completion_msg
    elif template == "steps":
        total = entity_config.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)
        current = _get_current_step(state, entity_config)
        content["total_steps"] = total
        content["current_step"] = current
        # Auto-derive progress when no explicit progress_attribute is configured
        if not entity_config.get(CONF_PROGRESS_ATTRIBUTE) and total > 0:
            content["progress"] = max(0.0, min(1.0, current / total))
    elif template == "alert":
        content["severity"] = entity_config.get(CONF_SEVERITY, DEFAULT_SEVERITY)
    elif template == "gauge":
        min_val, max_val = _gauge_base_fields(content, entity_config)
        value = _get_gauge_value(state, entity_config)
        value = max(min_val, min(max_val, value))
        content["value"] = value
        if max_val > min_val:
            content["progress"] = (value - min_val) / (max_val - min_val)
        else:
            content["progress"] = 1.0

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

    _add_url_deeplinks(content, entity_config)

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
        content["value"] = max_val
        content["progress"] = 1.0

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
        return max(0.0, min(1.0, value / scale))
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


def _get_gauge_value(state: State, entity_config: dict) -> float:
    """Extract gauge value from entity attribute or state.

    When value_attribute is configured, reads from that attribute.
    Otherwise falls back to the entity's primary state.
    """
    attr_name = entity_config.get(CONF_VALUE_ATTRIBUTE)
    if attr_name:
        try:
            return float(state.attributes.get(attr_name, 0))
        except (ValueError, TypeError):
            _LOGGER.debug("Could not parse gauge value attribute %s for %s", attr_name, state.entity_id)
            return 0.0
    try:
        return float(state.state)
    except (ValueError, TypeError):
        _LOGGER.debug("Could not parse entity state as gauge value for %s", state.entity_id)
        return 0.0
