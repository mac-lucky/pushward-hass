"""PushWard integration for Home Assistant."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .activity_manager import ActivityManager
from .api import PushWardApiClient
from .const import CONF_INTEGRATION_KEY, CONF_SERVER_URL, DOMAIN, SUBENTRY_TYPE_ENTITY

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PushWard from a config entry."""
    session = async_get_clientsession(hass)
    api = PushWardApiClient(session, entry.data[CONF_SERVER_URL], entry.data[CONF_INTEGRATION_KEY])

    try:
        await api.validate_connection()
    except Exception as err:
        _LOGGER.error("Failed to connect to PushWard: %s", err)
        raise ConfigEntryNotReady(f"Cannot connect to PushWard: {err}") from err

    entities = [dict(sub.data) for sub in entry.subentries.values() if sub.subentry_type == SUBENTRY_TYPE_ENTITY]
    manager = ActivityManager(hass, api, entities)
    await manager.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "manager": manager,
    }

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry or subentry updates — reload entity tracking."""
    manager = hass.data[DOMAIN][entry.entry_id]["manager"]
    entities = [dict(sub.data) for sub in entry.subentries.values() if sub.subentry_type == SUBENTRY_TYPE_ENTITY]
    await manager.async_reload(entities)


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
    return True
