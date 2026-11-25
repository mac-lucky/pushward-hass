"""Pause switch for PushWard, one per config entry (PushWard account).

On mutes everything except critical alerts (target "*", notifications and
activities, silent) on every device of the account until it is turned off; off
removes the "*" mutes again, also ones set in the app or by pushward.mute. Daily
"*" schedule mutes (quiet hours) neither turn it on nor go away with it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import _surface_api_errors
from .api import PushWardApiClient, unscheduled_mutes
from .const import DOMAIN, MUTE_TARGET_ALL
from .coordinator import PushWardMuteCoordinator
from .entity import account_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the account's pause switch."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: PushWardMuteCoordinator = data["mute_coordinator"]
    # Not the first-refresh variant: a failure (a key without the Account permission)
    # leaves only this switch unavailable instead of failing the entry.
    await coordinator.async_refresh()
    async_add_entities([PushWardPauseSwitch(coordinator, entry, data["api"])])


def _ends(mute: dict[str, Any]) -> datetime | None:
    """When a mute ends, None when it lasts until removed (or the server sent no readable time)."""
    until = dt_util.parse_datetime(str(mute.get("until") or ""))
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=dt_util.UTC)
    return until


class PushWardPauseSwitch(CoordinatorEntity[PushWardMuteCoordinator], SwitchEntity):
    """On while the account has a "*" mute that is not a daily schedule."""

    _attr_has_entity_name = True
    _attr_translation_key = "pause_notifications"
    _attr_icon = "mdi:bell-sleep"

    def __init__(self, coordinator: PushWardMuteCoordinator, entry: ConfigEntry, api: PushWardApiClient) -> None:
        super().__init__(coordinator)
        self._api = api
        self._attr_unique_id = f"{entry.entry_id}_pause_notifications"
        self._attr_device_info = account_device_info(entry)
        self._unsub_expiry: CALLBACK_TYPE | None = None

    def _pause_ends(self) -> list[datetime | None]:
        return [_ends(mute) for mute in unscheduled_mutes(self.coordinator.data or [], MUTE_TARGET_ALL)]

    @property
    def is_on(self) -> bool:
        now = dt_util.utcnow()
        return any(end is None or end > now for end in self._pause_ends())

    async def async_turn_on(self, **kwargs: Any) -> None:
        with _surface_api_errors():
            await self._api.create_mute(MUTE_TARGET_ALL, applies_to="both", mode="silent")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        with _surface_api_errors():
            await self._api.unmute(MUTE_TARGET_ALL)
        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_expiry)
        self._schedule_expiry()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_expiry()
        super()._handle_coordinator_update()

    @callback
    def _schedule_expiry(self) -> None:
        """Turn off when the last timed "*" mute ends, without waiting for the next poll."""
        self._cancel_expiry()
        ends = self._pause_ends()
        if ends and None not in ends and (last := max(ends)) > dt_util.utcnow():
            self._unsub_expiry = async_track_point_in_utc_time(self.hass, self._expired, last)

    @callback
    def _expired(self, _now: datetime) -> None:
        self._unsub_expiry = None
        self.async_write_ha_state()

    @callback
    def _cancel_expiry(self) -> None:
        if self._unsub_expiry is not None:
            self._unsub_expiry()
            self._unsub_expiry = None
