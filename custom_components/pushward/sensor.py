"""Usage/quota sensors for PushWard.

Surfaces the account's own consumption against its plan limits. Each metered
resource (notifications, Live Activity updates, widget updates, emails) gets one
"used" sensor whose state is the count consumed in the current period; the limit,
remaining, percent-used, period and reset time ride along as attributes. A
separate sensor reports the subscription tier (free / premium).

Free-tier accounts cap every resource, so each sensor shows used-vs-limit.
Premium accounts leave Live Activity / widget updates uncapped (limit reads
"unlimited"), switch the notifications counter to a daily cap, and add
month-to-date / daily-reset attributes.

All values come from the shared ``PushWardUsageCoordinator`` (``GET /auth/me``).
The fields stay absent (sensor → unavailable) on older servers that don't yet
return usage to integration keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, QUOTA_DAILY_RESET_KEY, QUOTA_RESET_KEY
from .coordinator import PushWardUsageCoordinator


@dataclass(frozen=True, kw_only=True)
class PushWardUsageSensorDescription(SensorEntityDescription):
    """Describes a PushWard usage sensor.

    ``key`` doubles as the ``/auth/me`` JSON key for the consumed count.
    ``limit_key`` is the key for its limit (absent ⇒ uncapped). ``is_notifications``
    adds the premium-only month-to-date + daily-reset attributes.
    """

    limit_key: str | None = None
    is_notifications: bool = False


USAGE_SENSORS: tuple[PushWardUsageSensorDescription, ...] = (
    PushWardUsageSensorDescription(
        key="notifications_used",
        translation_key="notifications_used",
        icon="mdi:bell",
        limit_key="notifications_limit",
        is_notifications=True,
    ),
    PushWardUsageSensorDescription(
        key="live_activity_updates_used",
        translation_key="live_activity_updates_used",
        icon="mdi:cellphone-message",
        limit_key="live_activity_updates_limit",
    ),
    PushWardUsageSensorDescription(
        key="widget_updates_used",
        translation_key="widget_updates_used",
        icon="mdi:widgets",
        limit_key="widget_updates_limit",
    ),
    PushWardUsageSensorDescription(
        key="emails_used",
        translation_key="emails_used",
        icon="mdi:email",
        limit_key="emails_limit",
    ),
)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """One service device per config entry, grouping all usage sensors."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="PushWard",
        manufacturer="PushWard",
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PushWard usage sensors from a config entry."""
    coordinator: PushWardUsageCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[SensorEntity] = [
        PushWardUsageSensor(coordinator, entry, description) for description in USAGE_SENSORS
    ]
    entities.append(PushWardTierSensor(coordinator, entry))
    async_add_entities(entities)


class _PushWardSensorBase(CoordinatorEntity[PushWardUsageCoordinator], SensorEntity):
    """Shared wiring for the account sensors.

    Subclasses set ``_data_key`` to the ``/auth/me`` field that must be present
    for the sensor to be available.
    """

    _attr_has_entity_name = True
    _data_key: str

    def __init__(self, coordinator: PushWardUsageCoordinator, entry: ConfigEntry, unique_suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        return super().available and self._data_key in (self.coordinator.data or {})


class PushWardUsageSensor(_PushWardSensorBase):
    """A single metered-resource "used" count with limit/remaining attributes."""

    entity_description: PushWardUsageSensorDescription
    # Period-resetting cumulative counter: TOTAL_INCREASING lets HA's long-term
    # statistics handle the monthly/daily reset back to zero.
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: PushWardUsageCoordinator,
        entry: ConfigEntry,
        description: PushWardUsageSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description
        self._data_key = description.key

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        description = self.entity_description
        used = data.get(description.key)
        limit = data.get(description.limit_key) if description.limit_key else None

        attrs: dict[str, Any] = {}
        if limit is None:
            # Uncapped (premium Live Activity / widget updates).
            attrs["limit"] = "unlimited"
            attrs["remaining"] = None
            attrs["percent_used"] = None
        else:
            attrs["limit"] = limit
            if isinstance(used, (int, float)) and limit > 0:
                attrs["remaining"] = max(limit - used, 0)
                attrs["percent_used"] = round(used / limit * 100, 1)
            else:
                attrs["remaining"] = None
                attrs["percent_used"] = None

        if (period := data.get("quota_period_month")) is not None:
            attrs["period"] = period
        if (resets_at := data.get(QUOTA_RESET_KEY)) is not None:
            attrs["resets_at"] = resets_at

        if description.is_notifications:
            # Premium-only: notifications_used is the daily count, so expose the
            # month-to-date total and the daily reset alongside it.
            if (used_month := data.get("notifications_used_month")) is not None:
                attrs["used_this_month"] = used_month
            if (daily_resets_at := data.get(QUOTA_DAILY_RESET_KEY)) is not None:
                attrs["daily_resets_at"] = daily_resets_at

        return attrs


class PushWardTierSensor(_PushWardSensorBase):
    """Reports the account's subscription tier (free / premium)."""

    _attr_translation_key = "subscription_tier"
    _attr_icon = "mdi:star-circle"
    _attr_device_class = SensorDeviceClass.ENUM
    _data_key = "subscribed"

    def __init__(self, coordinator: PushWardUsageCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "subscription_tier")
        self._attr_options = ["free", "premium"]

    @property
    def native_value(self) -> str | None:
        subscribed = (self.coordinator.data or {}).get("subscribed")
        if subscribed is None:
            return None
        return "premium" if subscribed else "free"
