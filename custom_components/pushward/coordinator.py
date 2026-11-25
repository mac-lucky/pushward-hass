"""Polling coordinators for PushWard.

``PushWardUsageCoordinator`` polls ``GET /auth/me`` for the account's own usage
counters and subscription tier so the sensor platform can surface them as Home
Assistant entities. ``PushWardMuteCoordinator`` reads ``GET /app/mutes`` at the
same slow pace for the pause switch. Activities and widgets are event-driven
(see ``activity_manager`` / ``widget_manager``).
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

from .api import PushWardAccountPermissionError, PushWardApiClient, PushWardApiError, PushWardAuthError
from .const import (
    DOMAIN,
    MAX_RETRIES,
    PRICING_URL,
    QUOTA_RESET_KEY,
    USAGE_LIMIT_RESOURCES,
    USAGE_UPDATE_INTERVAL,
    usage_limit_issue_id,
)
from .quota import QuotaGate

_LOGGER = logging.getLogger(__name__)


def _format_reset(value: Any) -> str:
    """Friendly reset hint for the repair description.

    ``/auth/me`` returns ISO-8601 timestamps (e.g. ``2026-07-01T00:00:00Z``); the
    date portion is enough for the user and avoids leaking a clock-precise time.
    """
    if isinstance(value, str) and value:
        return value.split("T", 1)[0]
    return "the next reset"


def monthly_resets_at(data: dict[str, Any]) -> Any:
    """When the monthly counters reset: the first of the month after quota_period_month, 00:00 UTC.

    ``quota_resets_at`` is the notifications counter's reset, the next midnight on a
    paid account, so it stands in only when the ``YYYYMM`` month is missing.
    """
    period = data.get("quota_period_month")
    if isinstance(period, int) and not isinstance(period, bool) and 1 <= period % 100 <= 12:
        year, month = divmod(period, 100)
        return f"{year + month // 12:04d}-{month % 12 + 1:02d}-01T00:00:00Z"
    return data.get(QUOTA_RESET_KEY)


def _is_over_limit(used: Any, limit: Any) -> bool:
    """True when a metered resource has reached or exceeded a positive cap.

    Uncapped resources omit the limit key (``None``); a non-numeric or non-positive
    limit reads as "no cap" so a missing or garbled field never raises a false alarm.
    """
    return isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0 and used >= limit


class PushWardUsageCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches account usage + quota from ``GET /auth/me``."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: PushWardApiClient,
        entry: ConfigEntry,
        quota_gate: QuotaGate | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_usage ({entry.title})",
            update_interval=timedelta(seconds=USAGE_UPDATE_INTERVAL),
        )
        self._api = api
        # The gate wakes this coordinator when a paused quota should have reset;
        # the poll below releases the kind once the counters confirm it.
        self._quota_gate = quota_gate
        if quota_gate is not None:
            quota_gate.wakeup = self.async_request_refresh

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the latest usage snapshot.

        A bad/expired key raises ConfigEntryAuthFailed so HA starts reauth; any
        other failure raises UpdateFailed so the sensors go unavailable until the
        next successful poll without tearing down the config entry.
        """
        try:
            # The first refresh runs inside setup, which HA retries with its own backoff.
            data = await self._api.get_me(attempts=1) if self.data is None else await self._api.get_me()
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

        The same under-limit reading releases the quota gate for that resource: the
        server has confirmed the period rolled over (or the plan changed), so paused
        updates can go out again.
        """
        entry_id = self.config_entry.entry_id
        for resource in USAGE_LIMIT_RESOURCES:
            issue_id = usage_limit_issue_id(entry_id, resource.used_key)
            used = data.get(resource.used_key)
            limit = data.get(resource.limit_key)
            if not _is_over_limit(used, limit):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                if self._quota_gate is not None:
                    self._quota_gate.release(resource.kind)
                continue
            reset = (
                data.get(resource.reset_key) or data.get(QUOTA_RESET_KEY)
                if resource.reset_key
                else monthly_resets_at(data)
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=resource.translation_key,
                translation_placeholders={
                    "account": self.config_entry.title,
                    "used": str(used),
                    "limit": str(limit),
                    "resets_at": _format_reset(reset),
                },
                learn_more_url=PRICING_URL,
            )


class PushWardMuteCoordinator(DataUpdateCoordinator[list[dict[str, Any]] | None]):
    """The account's mutes from ``GET /app/mutes``, for the pause switch.

    The switch refreshes it once it is added and then it polls every
    USAGE_UPDATE_INTERVAL, so a disabled switch sends nothing; the switch and the mute
    actions re-read after each change they make. ``None`` means the key lacks the
    Account permission: the switch is unavailable and nothing else is affected.
    """

    def __init__(
        self, hass: HomeAssistant, api: PushWardApiClient, entry: ConfigEntry, usage: PushWardUsageCoordinator
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_mutes ({entry.title})",
            update_interval=timedelta(seconds=USAGE_UPDATE_INTERVAL),
        )
        self._api = api
        self._usage = usage
        self._warned = False

    def _account_level(self) -> Any:
        """The key's Account permission as the last ``/auth/me`` reported it, None when it did not."""
        key = (self._usage.data or {}).get("integration_key")
        permissions = key.get("permissions") if isinstance(key, dict) else None
        return permissions.get("account") if isinstance(permissions, dict) else None

    async def _async_update_data(self) -> list[dict[str, Any]] | None:
        # /auth/me, polled anyway, tells when the read would only be refused: skip it then.
        if self._account_level() != "none":
            try:
                # One try for the first read, which the switch's setup waits for.
                mutes = await self._api.list_mutes(attempts=1 if self.data is None else MAX_RETRIES, lane="background")
            except PushWardAccountPermissionError:
                pass
            except PushWardApiError as err:
                raise UpdateFailed(str(err)) from err
            else:
                self._warned = False
                return mutes
        if not self._warned:
            self._warned = True
            _LOGGER.warning(
                "%s: the integration key cannot read mutes, so the pause switch is unavailable. In the PushWard "
                "app, open Settings > Integration Keys, pick this key and set Account to Full management",
                self.config_entry.title,
            )
        return None
