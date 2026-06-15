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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PushWardApiClient, PushWardApiError, PushWardAuthError
from .const import (
    APP_STORE_URL,
    DOMAIN,
    QUOTA_RESET_KEY,
    USAGE_LIMIT_RESOURCES,
    USAGE_UPDATE_INTERVAL,
    usage_limit_issue_id,
)

_LOGGER = logging.getLogger(__name__)


def _format_reset(value: Any) -> str:
    """Friendly reset hint for the repair description.

    ``/auth/me`` returns ISO-8601 timestamps (e.g. ``2026-07-01T00:00:00Z``); the
    date portion is enough for the user and avoids leaking a clock-precise time.
    """
    if isinstance(value, str) and value:
        return value.split("T", 1)[0]
    return "the next reset"


def _is_over_limit(used: Any, limit: Any) -> bool:
    """True when a metered resource has reached or exceeded a positive cap.

    Uncapped resources omit the limit key (``None``); a non-numeric or non-positive
    limit reads as "no cap" so a missing or garbled field never raises a false alarm.
    """
    return isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0 and used >= limit


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
            data = await self._api.get_me()
        except PushWardAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PushWardApiError as err:
            raise UpdateFailed(str(err)) from err
        self._evaluate_usage_limits(data)
        return data

    def _evaluate_usage_limits(self, data: dict[str, Any]) -> None:
        """Raise/clear a Repair issue per metered resource that is at/over limit.

        Uncapped resources (premium Live Activity / widget updates omit the limit key)
        never trip. ``async_delete_issue`` is a no-op when the issue is absent, so the
        under-limit branch keeps the registry in sync without a presence check.
        """
        entry_id = self.config_entry.entry_id
        for resource in USAGE_LIMIT_RESOURCES:
            issue_id = usage_limit_issue_id(entry_id, resource.used_key)
            used = data.get(resource.used_key)
            limit = data.get(resource.limit_key)
            if not _is_over_limit(used, limit):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue
            reset = data.get(resource.reset_key) or data.get(QUOTA_RESET_KEY)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=resource.translation_key,
                translation_placeholders={
                    "used": str(used),
                    "limit": str(limit),
                    "resets_at": _format_reset(reset),
                },
                learn_more_url=APP_STORE_URL,
            )
