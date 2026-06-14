"""Tests for the PushWard usage sensors and coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.api import PushWardApiError, PushWardAuthError
from custom_components.pushward.const import (
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    DOMAIN,
)
from custom_components.pushward.coordinator import PushWardUsageCoordinator
from custom_components.pushward.sensor import (
    USAGE_SENSORS,
    PushWardTierSensor,
    PushWardUsageSensor,
)

from .conftest import make_premium_usage_payload, make_usage_payload


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: "test-key"},
        version=2,
        unique_id=DOMAIN,
    )


def _coordinator(
    hass: HomeAssistant,
    payload: dict | None,
    *,
    success: bool = True,
    get_me_error: Exception | None = None,
):
    """Build a coordinator with its data pre-seeded (no network).

    Pass ``get_me_error`` to make the injected ``api.get_me`` raise, for the
    error-mapping tests that drive ``_async_update_data`` directly.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.get_me = AsyncMock(side_effect=get_me_error) if get_me_error else AsyncMock(return_value=payload)
    coordinator = PushWardUsageCoordinator(hass, api, entry)
    coordinator.data = payload
    coordinator.last_update_success = success
    return coordinator, entry


def _usage_sensor(coordinator, entry, key: str) -> PushWardUsageSensor:
    description = next(d for d in USAGE_SENSORS if d.key == key)
    return PushWardUsageSensor(coordinator, entry, description)


# --- usage sensor: free tier ---


async def test_free_tier_notifications_sensor(hass: HomeAssistant) -> None:
    coordinator, entry = _coordinator(hass, make_usage_payload())
    sensor = _usage_sensor(coordinator, entry, "notifications_used")

    assert sensor.native_value == 137
    assert sensor.available is True
    attrs = sensor.extra_state_attributes
    assert attrs["limit"] == 500
    assert attrs["remaining"] == 363
    assert attrs["percent_used"] == 27.4
    assert attrs["period"] == 202606
    assert attrs["resets_at"] == "2026-07-01T00:00:00Z"
    # Free tier: no daily / month-to-date breakdown.
    assert "used_this_month" not in attrs
    assert "daily_resets_at" not in attrs


async def test_free_tier_all_limits_present(hass: HomeAssistant) -> None:
    coordinator, entry = _coordinator(hass, make_usage_payload())
    for key, want_limit in (
        ("live_activity_updates_used", 250),
        ("widget_updates_used", 50),
        ("emails_used", 500),
    ):
        sensor = _usage_sensor(coordinator, entry, key)
        assert sensor.extra_state_attributes["limit"] == want_limit


async def test_over_quota_clamps_remaining_and_exceeds_100_percent(hass: HomeAssistant) -> None:
    # Usage can equal/exceed the cap around enforcement/refund races.
    coordinator, entry = _coordinator(hass, make_usage_payload(notifications_used=600, notifications_limit=500))
    attrs = _usage_sensor(coordinator, entry, "notifications_used").extra_state_attributes
    assert attrs["remaining"] == 0
    assert attrs["percent_used"] == 120.0


# --- usage sensor: premium tier (uncapped LA/widget, daily notifications) ---


async def test_premium_uncapped_and_daily_notifications(hass: HomeAssistant) -> None:
    coordinator, entry = _coordinator(hass, make_premium_usage_payload())

    la = _usage_sensor(coordinator, entry, "live_activity_updates_used")
    la_attrs = la.extra_state_attributes
    assert la_attrs["limit"] == "unlimited"
    assert la_attrs["remaining"] is None
    assert la_attrs["percent_used"] is None

    notif = _usage_sensor(coordinator, entry, "notifications_used")
    n_attrs = notif.extra_state_attributes
    assert notif.native_value == 12
    assert n_attrs["limit"] == 5000
    assert n_attrs["remaining"] == 4988
    assert n_attrs["percent_used"] == 0.2
    assert n_attrs["used_this_month"] == 420
    assert n_attrs["daily_resets_at"] == "2026-06-15T00:00:00Z"


# --- tier sensor ---


@pytest.mark.parametrize(("subscribed", "expected"), [(False, "free"), (True, "premium")])
async def test_tier_sensor(hass: HomeAssistant, subscribed: bool, expected: str) -> None:
    coordinator, entry = _coordinator(hass, make_usage_payload(subscribed=subscribed))
    sensor = PushWardTierSensor(coordinator, entry)
    assert sensor.native_value == expected
    assert sensor.available is True


# --- resilience: missing usage keys / failed update ---


async def test_sensor_unavailable_when_fields_absent(hass: HomeAssistant) -> None:
    # Old server that doesn't return usage to integration keys: compact body.
    coordinator, entry = _coordinator(hass, {"id": "u", "activity_count": 0})
    sensor = _usage_sensor(coordinator, entry, "notifications_used")
    assert sensor.available is False
    assert sensor.native_value is None

    tier = PushWardTierSensor(coordinator, entry)
    assert tier.available is False
    assert tier.native_value is None


async def test_sensor_unavailable_on_failed_update(hass: HomeAssistant) -> None:
    coordinator, entry = _coordinator(hass, make_usage_payload(), success=False)
    sensor = _usage_sensor(coordinator, entry, "emails_used")
    assert sensor.available is False


# --- coordinator error mapping ---


async def test_coordinator_auth_error_maps_to_reauth(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, None, get_me_error=PushWardAuthError("bad key"))
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_coordinator_api_error_maps_to_update_failed(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, None, get_me_error=PushWardApiError("boom"))
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# --- end-to-end: entities created and populated on setup ---


async def test_sensors_created_on_setup(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.get_me = AsyncMock(return_value=make_usage_payload())

    with patch("custom_components.pushward.PushWardApiClient", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    notif_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_notifications_used")
    assert notif_id is not None
    state = hass.states.get(notif_id)
    assert state is not None
    assert state.state == "137"
    assert state.attributes["limit"] == 500

    tier_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_subscription_tier")
    assert tier_id is not None
    assert hass.states.get(tier_id).state == "free"
