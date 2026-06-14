"""Usage/quota polling coordinator for PushWard.

Polls ``GET /auth/me`` for the account's own usage counters and subscription
tier so the sensor platform can surface them as Home Assistant entities. This is
the integration's only polling component — activities and widgets are
event-driven (see ``activity_manager`` / ``widget_manager``).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PushWardApiClient, PushWardApiError, PushWardAuthError
from .const import DOMAIN, USAGE_UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class PushWardUsageCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches account usage + quota from ``GET /auth/me``."""

    def __init__(self, hass: HomeAssistant, api: PushWardApiClient, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_usage",
            update_interval=timedelta(seconds=USAGE_UPDATE_INTERVAL),
        )
        self._api = api

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the latest usage snapshot.

        A bad/expired key raises ConfigEntryAuthFailed so HA starts reauth; any
        other failure raises UpdateFailed so the sensors go unavailable until the
        next successful poll without tearing down the config entry.
        """
        try:
            return await self._api.get_me()
        except PushWardAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PushWardApiError as err:
            raise UpdateFailed(str(err)) from err
