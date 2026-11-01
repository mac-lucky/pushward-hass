"""Notify entity for PushWard, one per config entry (PushWard account).

It makes the account a target for notify.send_message and for notify groups.
The message goes through the same client call as the send_notification action,
so the entry's end-to-end key seals it and the quota gate refuses it the same
way. The server needs a title and notify.send_message may not carry one; the
fallback is the Home Assistant location name (Settings > System > General),
which tells the phone which home sent it. The entry title would only repeat the
account name on the account's own phone.
"""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _surface_api_errors
from .api import PushWardApiClient
from .const import DOMAIN
from .entity import account_device_info

DEFAULT_TITLE = "Home Assistant"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the account's notify entity."""
    api: PushWardApiClient = hass.data[DOMAIN][entry.entry_id]["api"]
    async_add_entities([PushWardNotifyEntity(entry, api)])


class PushWardNotifyEntity(NotifyEntity):
    """Sends notify.send_message to the PushWard account as a push notification."""

    _attr_has_entity_name = True
    _attr_translation_key = "push_notification"
    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(self, entry: ConfigEntry, api: PushWardApiClient) -> None:
        self._api = api
        self._attr_unique_id = f"{entry.entry_id}_notify"
        self._attr_device_info = account_device_info(entry)

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        if not title or not title.strip():
            title = self.hass.config.location_name or DEFAULT_TITLE
        with _surface_api_errors():
            await self._api.create_notification(title=title, body=message)
