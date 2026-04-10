"""PushWard integration for Home Assistant."""

from __future__ import annotations

import logging
from functools import partial

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .activity_manager import ActivityManager
from .api import PushWardApiClient, PushWardApiError, PushWardAuthError
from .const import (
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_PRIORITY,
    DOMAIN,
    SCALES,
    SEVERITIES,
    SUBENTRY_TYPE_ENTITY,
    validate_slug,
    validate_url,
)

_LOGGER = logging.getLogger(__name__)


_CONTENT_FIELDS = [
    "template",
    "progress",
    "state_text",
    "icon",
    "subtitle",
    "accent_color",
    "remaining_time",
    "url",
    "secondary_url",
    "end_date",
    "total_steps",
    "current_step",
    "severity",
    "completion_message",
    "value",
    "min_value",
    "max_value",
    "unit",
    "scale",
    "decimals",
    "smoothing",
    "thresholds",
]

SERVICE_UPDATE_ACTIVITY = "update_activity"
SERVICE_CREATE_ACTIVITY = "create_activity"
SERVICE_END_ACTIVITY = "end_activity"
SERVICE_DELETE_ACTIVITY = "delete_activity"

SCHEMA_UPDATE_ACTIVITY = vol.Schema(
    {
        vol.Required("slug"): validate_slug,
        vol.Required("state"): vol.In(["ONGOING", "ENDED"]),
        vol.Optional("template"): str,
        vol.Optional("progress"): vol.Coerce(float),
        vol.Optional("state_text"): str,
        vol.Optional("icon"): str,
        vol.Optional("subtitle"): str,
        vol.Optional("accent_color"): str,
        vol.Optional("remaining_time"): vol.Coerce(int),
        vol.Optional("url"): validate_url,
        vol.Optional("secondary_url"): validate_url,
        vol.Optional("end_date"): vol.Coerce(int),
        vol.Optional("total_steps"): vol.Coerce(int),
        vol.Optional("current_step"): vol.Coerce(int),
        vol.Optional("severity"): vol.In(SEVERITIES),
        vol.Optional("completion_message"): str,
        vol.Optional("value"): vol.Any(vol.Coerce(float), dict),
        vol.Optional("min_value"): vol.Coerce(float),
        vol.Optional("max_value"): vol.Coerce(float),
        vol.Optional("unit"): str,
        vol.Optional("scale"): vol.In(SCALES),
        vol.Optional("decimals"): vol.All(vol.Coerce(int), vol.Range(min=0, max=10)),
        vol.Optional("smoothing"): bool,
        vol.Optional("thresholds"): list,
    }
)

SCHEMA_CREATE_ACTIVITY = vol.Schema(
    {
        vol.Required("slug"): validate_slug,
        vol.Required("name"): str,
        vol.Optional("priority", default=DEFAULT_PRIORITY): vol.Coerce(int),
        vol.Optional("ended_ttl"): vol.Coerce(int),
        vol.Optional("stale_ttl"): vol.Coerce(int),
    }
)

SCHEMA_END_ACTIVITY = vol.Schema(
    {
        vol.Required("slug"): validate_slug,
        vol.Optional("completion_message"): str,
    }
)

SCHEMA_DELETE_ACTIVITY = vol.Schema(
    {
        vol.Required("slug"): validate_slug,
    }
)


def _get_api(hass: HomeAssistant) -> PushWardApiClient:
    """Get the API client from the first available config entry."""
    entries = hass.data.get(DOMAIN)
    if not entries:
        raise HomeAssistantError("PushWard is not configured. Add the integration via Settings → Devices & Services.")
    return next(iter(entries.values()))["api"]


async def _async_handle_update_activity(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the update_activity service call."""
    api = _get_api(hass)
    slug = call.data["slug"]
    state = call.data["state"]
    content = {}
    for field in _CONTENT_FIELDS:
        if field in call.data:
            # Map state_text -> state for the API
            key = "state" if field == "state_text" else field
            content[key] = call.data[field]
    await api.update_activity(slug, state, content)


async def _async_handle_create_activity(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the create_activity service call."""
    api = _get_api(hass)
    await api.create_activity(
        slug=call.data["slug"],
        name=call.data["name"],
        priority=call.data["priority"],
        ended_ttl=call.data.get("ended_ttl"),
        stale_ttl=call.data.get("stale_ttl"),
    )


async def _async_handle_end_activity(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the end_activity service call."""
    api = _get_api(hass)
    slug = call.data["slug"]
    content = {}
    if "completion_message" in call.data:
        content["completion_message"] = call.data["completion_message"]
    await api.update_activity(slug, "ENDED", content)


async def _async_handle_delete_activity(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the delete_activity service call."""
    api = _get_api(hass)
    await api.delete_activity(call.data["slug"])


def _register_services(hass: HomeAssistant) -> None:
    """Register PushWard services (only once)."""
    if hass.services.has_service(DOMAIN, SERVICE_UPDATE_ACTIVITY):
        return

    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_ACTIVITY, partial(_async_handle_update_activity, hass), SCHEMA_UPDATE_ACTIVITY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CREATE_ACTIVITY, partial(_async_handle_create_activity, hass), SCHEMA_CREATE_ACTIVITY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_END_ACTIVITY, partial(_async_handle_end_activity, hass), SCHEMA_END_ACTIVITY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_ACTIVITY, partial(_async_handle_delete_activity, hass), SCHEMA_DELETE_ACTIVITY
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PushWard from a config entry."""
    session = async_get_clientsession(hass)
    api = PushWardApiClient(session, entry.data[CONF_SERVER_URL], entry.data[CONF_INTEGRATION_KEY])

    try:
        await api.validate_connection()
    except PushWardAuthError as err:
        raise ConfigEntryAuthFailed(f"Invalid integration key: {err}") from err
    except (PushWardApiError, aiohttp.ClientError, TimeoutError, OSError) as err:
        raise ConfigEntryNotReady(f"Cannot connect to PushWard: {err}") from err

    entities = [dict(sub.data) for sub in entry.subentries.values() if sub.subentry_type == SUBENTRY_TYPE_ENTITY]
    manager = ActivityManager(hass, api, entities, entry)
    await manager.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "manager": manager,
    }

    _register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry or subentry updates — reload entity tracking."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data is None:
        return
    entities = [dict(sub.data) for sub in entry.subentries.values() if sub.subentry_type == SUBENTRY_TYPE_ENTITY]
    await data["manager"].async_reload(entities)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entry to current version."""
    _LOGGER.debug("Migrating PushWard from version %s", config_entry.version)

    if config_entry.version == 1:
        # V1 stored entities in options; V2 uses subentries.
        # Clear legacy options — users re-add entities as subentries.
        hass.config_entries.async_update_entry(config_entry, version=2, options={})

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload PushWard config entry."""
    data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if data:
        await data["manager"].async_stop()

    # Unregister services when no entries remain
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_UPDATE_ACTIVITY)
        hass.services.async_remove(DOMAIN, SERVICE_CREATE_ACTIVITY)
        hass.services.async_remove(DOMAIN, SERVICE_END_ACTIVITY)
        hass.services.async_remove(DOMAIN, SERVICE_DELETE_ACTIVITY)

    return True
