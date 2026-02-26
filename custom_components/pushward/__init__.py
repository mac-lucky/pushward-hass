"""PushWard integration for Home Assistant."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .activity_manager import ActivityManager
from .api import PushWardApiClient
from .const import CONF_ENTITIES, CONF_INTEGRATION_KEY, CONF_SERVER_URL, DOMAIN

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

    entities = entry.options.get(CONF_ENTITIES, [])
    manager = ActivityManager(hass, api, entities)
    await manager.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "manager": manager,
    }

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload entity tracking."""
    manager = hass.data[DOMAIN][entry.entry_id]["manager"]
    new_entities = entry.options.get(CONF_ENTITIES, [])
    await manager.async_reload(new_entities)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload PushWard config entry."""
    data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if data:
        await data["manager"].async_stop()
    return True
