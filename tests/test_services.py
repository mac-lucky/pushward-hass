"""Tests for PushWard HA service registration and handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.api import PushWardAuthError
from custom_components.pushward.const import (
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    DOMAIN,
)

MOCK_INTEGRATION_KEY = "test-key-123"


def _mock_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={
            CONF_SERVER_URL: DEFAULT_SERVER_URL,
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
        version=2,
        unique_id=DOMAIN,
    )


def _mock_api() -> AsyncMock:
    """Create a mock API client with all methods."""
    api = AsyncMock()
    api.validate_connection = AsyncMock(return_value=True)
    api.create_activity = AsyncMock()
    api.update_activity = AsyncMock()
    api.delete_activity = AsyncMock()
    return api


async def _setup_entry(hass: HomeAssistant, mock_api: AsyncMock) -> MockConfigEntry:
    """Set up a config entry with a mocked API client."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.pushward.PushWardApiClient",
        return_value=mock_api,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


async def test_services_registered_on_setup(hass: HomeAssistant) -> None:
    """Services are registered when the integration is set up."""
    api = _mock_api()
    entry = await _setup_entry(hass, api)

    assert hass.services.has_service(DOMAIN, "update_activity")
    assert hass.services.has_service(DOMAIN, "create_activity")
    assert hass.services.has_service(DOMAIN, "end_activity")
    assert hass.services.has_service(DOMAIN, "delete_activity")

    # Unload should remove services
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, "update_activity")
    assert not hass.services.has_service(DOMAIN, "create_activity")
    assert not hass.services.has_service(DOMAIN, "end_activity")
    assert not hass.services.has_service(DOMAIN, "delete_activity")


async def test_service_update_activity(hass: HomeAssistant) -> None:
    """update_activity service calls api.update_activity with correct args."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "ha-washer", "state": "ONGOING", "state_text": "Running", "progress": 0.5},
        blocking=True,
    )

    api.update_activity.assert_awaited_once()
    call_args = api.update_activity.call_args[0]
    assert call_args[0] == "ha-washer"
    assert call_args[1] == "ONGOING"
    content = call_args[2]
    assert content["state"] == "Running"  # state_text mapped to "state"
    assert content["progress"] == 0.5


async def test_service_create_activity(hass: HomeAssistant) -> None:
    """create_activity service calls api.create_activity."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "create_activity",
        {"slug": "ha-washer", "name": "Washer", "priority": 5},
        blocking=True,
    )

    api.create_activity.assert_awaited_once()
    call_kwargs = api.create_activity.call_args[1]
    assert call_kwargs["slug"] == "ha-washer"
    assert call_kwargs["name"] == "Washer"
    assert call_kwargs["priority"] == 5
    # TTLs omitted → None (server defaults)
    assert call_kwargs["ended_ttl"] is None
    assert call_kwargs["stale_ttl"] is None


async def test_service_create_activity_with_ttls(hass: HomeAssistant) -> None:
    """create_activity service passes explicit TTLs when provided."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "create_activity",
        {"slug": "ha-washer", "name": "Washer", "priority": 1, "ended_ttl": 60, "stale_ttl": 120},
        blocking=True,
    )

    call_kwargs = api.create_activity.call_args[1]
    assert call_kwargs["ended_ttl"] == 60
    assert call_kwargs["stale_ttl"] == 120


async def test_service_end_activity(hass: HomeAssistant) -> None:
    """end_activity service calls api.update_activity with ENDED state."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "end_activity",
        {"slug": "ha-washer", "completion_message": "Wash Done"},
        blocking=True,
    )

    api.update_activity.assert_awaited_once()
    call_args = api.update_activity.call_args[0]
    assert call_args[0] == "ha-washer"
    assert call_args[1] == "ENDED"
    assert call_args[2]["completion_message"] == "Wash Done"


async def test_service_delete_activity(hass: HomeAssistant) -> None:
    """delete_activity service calls api.delete_activity."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "delete_activity",
        {"slug": "ha-washer"},
        blocking=True,
    )

    api.delete_activity.assert_awaited_once_with("ha-washer")


# --- Setup health check tests ---


async def test_setup_auth_error_triggers_reauth(hass: HomeAssistant) -> None:
    """Auth error during setup puts entry in SETUP_ERROR and starts reauth."""
    api = _mock_api()
    api.validate_connection = AsyncMock(side_effect=PushWardAuthError("bad key", status_code=401))

    entry = _mock_entry()
    entry.add_to_hass(hass)

    with patch("custom_components.pushward.PushWardApiClient", return_value=api):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_connection_error_retries(hass: HomeAssistant) -> None:
    """Connection error during setup puts entry in SETUP_RETRY."""
    api = _mock_api()
    api.validate_connection = AsyncMock(side_effect=OSError("timeout"))

    entry = _mock_entry()
    entry.add_to_hass(hass)

    with patch("custom_components.pushward.PushWardApiClient", return_value=api):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
