"""Map Home Assistant state/attributes to PushWard content."""

import re
import time

from homeassistant.core import State

from .const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SECONDARY_URL,
    CONF_SEVERITY,
    CONF_STATE_LABELS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_TEMPLATE,
    CONF_TOTAL_STEPS,
    CONF_URL,
    DEFAULT_SEVERITY,
    DEFAULT_TOTAL_STEPS,
    DOMAIN_DEFAULTS,
)


def sanitize_slug(entity_id: str) -> str:
    """Convert an HA entity_id to a PushWard slug.

    sensor.washing_machine_status -> ha-washing-machine-status
    """
    # Remove domain prefix (e.g. "sensor.")
    slug = entity_id.replace(".", "-", 1) if "." in entity_id else entity_id
    # Replace underscores with hyphens
    slug = slug.replace("_", "-")
    # Remove any non-alphanumeric characters except hyphens
    slug = re.sub(r"[^a-z0-9-]", "", slug.lower())
    # Collapse multiple hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"ha-{slug}"


def map_content(state: State, entity_config: dict) -> dict:
    """Map HA state + attributes to a PushWard content dict."""
    # State label: use custom label if configured, else default formatting
    state_labels = entity_config.get(CONF_STATE_LABELS) or {}
    if state.state in state_labels:
        state_text = state_labels[state.state]
    else:
        state_text = state.state.replace("_", " ").capitalize()

    # Icon resolution: icon_attribute > static icon > domain default
    icon = entity_config.get(CONF_ICON, "questionmark.circle")
    icon_attr = entity_config.get(CONF_ICON_ATTRIBUTE)
    if icon_attr:
        dynamic_icon = state.attributes.get(icon_attr)
        if dynamic_icon:
            icon = str(dynamic_icon)

    # Subtitle: subtitle_attribute > friendly_name
    subtitle_attr = entity_config.get(CONF_SUBTITLE_ATTRIBUTE)
    if subtitle_attr:
        subtitle = state.attributes.get(subtitle_attr) or state.attributes.get("friendly_name", "")
    else:
        subtitle = state.attributes.get("friendly_name", "")

    # Accent color resolution: accent_color_attribute > static accent_color > "blue"
    accent = entity_config.get(CONF_ACCENT_COLOR, "")
    color_attr = entity_config.get(CONF_ACCENT_COLOR_ATTRIBUTE)
    if color_attr:
        dynamic_color = state.attributes.get(color_attr)
        if dynamic_color:
            accent = str(dynamic_color)
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

    # URL deep links
    url = entity_config.get(CONF_URL, "")
    if url:
        content["url"] = url
    secondary_url = entity_config.get(CONF_SECONDARY_URL, "")
    if secondary_url:
        content["secondary_url"] = secondary_url

    # Template-specific required fields
    template = content["template"]
    if template == "countdown":
        content["end_date"] = int(time.time()) + (remaining if remaining is not None else 0)
        completion_msg = entity_config.get(CONF_COMPLETION_MESSAGE)
        if completion_msg:
            content["completion_message"] = completion_msg
    elif template == "pipeline":
        total = entity_config.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)
        current = _get_current_step(state, entity_config)
        content["total_steps"] = total
        content["current_step"] = current
        # Auto-derive progress when no explicit progress_attribute is configured
        if not entity_config.get(CONF_PROGRESS_ATTRIBUTE) and total > 0:
            content["progress"] = max(0.0, min(1.0, current / total))
    elif template == "alert":
        content["severity"] = entity_config.get(CONF_SEVERITY, DEFAULT_SEVERITY)

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

    # Persist URL deep links through end
    url = entity_config.get(CONF_URL, "")
    if url:
        content["url"] = url
    secondary_url = entity_config.get(CONF_SECONDARY_URL, "")
    if secondary_url:
        content["secondary_url"] = secondary_url

    # Template-specific required fields for server validation
    template = content["template"]
    if template == "countdown":
        content["end_date"] = int(time.time())
    elif template == "pipeline":
        total = entity_config.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)
        content["total_steps"] = total
        content["current_step"] = total
        content["progress"] = 1.0
    elif template == "alert":
        content["severity"] = entity_config.get(CONF_SEVERITY, DEFAULT_SEVERITY)

    return content


def get_domain_defaults(domain: str) -> dict:
    """Return default icon, start_states, and end_states for an HA domain."""
    return DOMAIN_DEFAULTS.get(
        domain,
        {"icon": "questionmark.circle", "start_states": [], "end_states": []},
    )


def _get_progress(state: State, entity_config: dict) -> float:
    """Extract progress from entity attributes, clamped to 0.0-1.0."""
    attr_name = entity_config.get(CONF_PROGRESS_ATTRIBUTE)
    if not attr_name:
        return 0.0
    try:
        value = float(state.attributes.get(attr_name, 0))
        return max(0.0, min(1.0, value / 100.0))
    except (ValueError, TypeError):
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
        return 0


def _get_remaining_time(state: State, entity_config: dict) -> int | None:
    """Extract remaining time in seconds from entity attributes."""
    attr_name = entity_config.get(CONF_REMAINING_TIME_ATTR)
    if not attr_name:
        return None
    try:
        return int(state.attributes.get(attr_name, 0))
    except (ValueError, TypeError):
        return None
