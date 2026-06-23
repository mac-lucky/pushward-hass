"""Diagnostics support for the PushWard integration.

Home Assistant renders a per-config-entry "Download diagnostics" button from
``async_get_config_entry_diagnostics``. The dump lets a user attach a redacted
snapshot to a bug report so a broken board/log (or any template) payload is
visible without manual back-and-forth.

The integration key (``hlk_``) is always redacted; only rendered content and
config shapes are included.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .activity_manager import ActivityManager
from .const import (
    CONF_ENTITY_ID,
    CONF_INTEGRATION_KEY,
    CONF_SECONDARY_URL,
    CONF_SLUG,
    CONF_TAP_ACTION_URL,
    CONF_URL,
    DOMAIN,
    SUBENTRY_TYPE_ENTITY,
    SUBENTRY_TYPE_WIDGET,
)
from .widget_manager import WidgetManager

# Never leak the integration key, nor user-supplied tap-action targets — webhook
# URLs (config + rendered last_content) and any silent-webhook headers/body can
# embed secrets/tokens. async_redact_data matches these keys recursively, so the
# rendered tap_action/url_action dicts inside last_content are covered too.
TO_REDACT = {
    CONF_INTEGRATION_KEY,
    CONF_TAP_ACTION_URL,
    CONF_URL,
    CONF_SECONDARY_URL,
    "headers",
    "body",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return a redacted diagnostics dump for a PushWard config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    manager: ActivityManager | None = data.get("manager")
    widget_manager: WidgetManager | None = data.get("widget_manager")
    coordinator = data.get("coordinator")

    subentries: list[dict[str, Any]] = []
    for sub in entry.subentries.values():
        item: dict[str, Any] = {
            "subentry_type": sub.subentry_type,
            "title": sub.title,
            "config": async_redact_data(dict(sub.data), TO_REDACT),
        }
        if sub.subentry_type == SUBENTRY_TYPE_ENTITY and manager is not None:
            tracked = manager._tracked.get(sub.data.get(CONF_ENTITY_ID))
            if tracked is not None:
                item["is_active"] = tracked.is_active
                item["last_content"] = (
                    async_redact_data(tracked.last_content, TO_REDACT) if tracked.last_content else tracked.last_content
                )
        elif sub.subentry_type == SUBENTRY_TYPE_WIDGET and widget_manager is not None:
            tracked = widget_manager._tracked.get(sub.data.get(CONF_SLUG))
            if tracked is not None:
                item["last_content"] = (
                    async_redact_data(tracked.last_content, TO_REDACT) if tracked.last_content else tracked.last_content
                )
        subentries.append(item)

    return {
        "entry": {
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "subentries": subentries,
        "usage": coordinator.data if coordinator is not None else None,
    }
