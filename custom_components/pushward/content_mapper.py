"""Map Home Assistant state/attributes to PushWard content."""

import re

from homeassistant.core import State

from .const import (
    CONF_ACCENT_COLOR,
    CONF_ICON,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_TEMPLATE,
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
    content: dict = {
        "template": entity_config.get(CONF_TEMPLATE, "generic"),
        "progress": _get_progress(state, entity_config),
        "state": state.state.replace("_", " ").capitalize(),
        "icon": entity_config.get(CONF_ICON, "questionmark.circle"),
        "subtitle": state.attributes.get("friendly_name", ""),
    }

    remaining = _get_remaining_time(state, entity_config)
    if remaining is not None:
        content["remaining_time"] = remaining

    accent = entity_config.get(CONF_ACCENT_COLOR, "")
    content["accent_color"] = accent if accent else "blue"

    return content


def map_completion_content(entity_config: dict) -> dict:
    """Build content for the "Complete" phase of two-phase end."""
    return {
        "template": entity_config.get(CONF_TEMPLATE, "generic"),
        "progress": 1.0,
        "state": "Complete",
        "icon": "checkmark.circle.fill",
        "subtitle": "",
        "accent_color": "green",
    }


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


def _get_remaining_time(state: State, entity_config: dict) -> int | None:
    """Extract remaining time in seconds from entity attributes."""
    attr_name = entity_config.get(CONF_REMAINING_TIME_ATTR)
    if not attr_name:
        return None
    try:
        return int(state.attributes.get(attr_name, 0))
    except (ValueError, TypeError):
        return None
