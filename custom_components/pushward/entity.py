"""The device every entity of a PushWard account hangs off."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN


def account_device_info(entry: ConfigEntry) -> DeviceInfo:
    """One service device per config entry (PushWard account): the usage sensors and the notify entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="PushWard",
        entry_type=DeviceEntryType.SERVICE,
    )
