"""Tests for PushWard HA service registration and handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
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
    api.create_notification = AsyncMock()
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
    assert hass.services.has_service(DOMAIN, "send_notification")

    # Unload should remove services
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, "update_activity")
    assert not hass.services.has_service(DOMAIN, "create_activity")
    assert not hass.services.has_service(DOMAIN, "end_activity")
    assert not hass.services.has_service(DOMAIN, "delete_activity")
    assert not hass.services.has_service(DOMAIN, "send_notification")


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


# --- send_notification service tests ---


async def test_service_send_notification(hass: HomeAssistant) -> None:
    """send_notification service calls api.create_notification with required fields."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {"title": "Door Opened", "body": "The front door was opened."},
        blocking=True,
    )

    api.create_notification.assert_awaited_once()
    call_kwargs = api.create_notification.call_args[1]
    assert call_kwargs["title"] == "Door Opened"
    assert call_kwargs["body"] == "The front door was opened."
    assert call_kwargs["push"] is True


async def test_service_send_notification_all_fields(hass: HomeAssistant) -> None:
    """send_notification service passes all optional fields.

    The `volume` field is asserted here for backward compatibility — the
    Python service schema accepts it and forwards it to the server so existing
    automation YAMLs keep working. The UI selector may or may not surface
    `volume`; the schema is the authoritative contract.
    """
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {
            "title": "Alert",
            "body": "Motion detected",
            "subtitle": "Front Yard",
            "level": "time-sensitive",
            "volume": 0.8,
            "thread_id": "security",
            "collapse_id": "motion-front",
            "category": "SECURITY",
            "source": "home-assistant",
            "source_display_name": "Home Assistant",
            "activity_slug": "ha-motion",
            "push": False,
        },
        blocking=True,
    )

    api.create_notification.assert_awaited_once()
    call_kwargs = api.create_notification.call_args[1]
    assert call_kwargs["title"] == "Alert"
    assert call_kwargs["body"] == "Motion detected"
    assert call_kwargs["subtitle"] == "Front Yard"
    assert call_kwargs["level"] == "time-sensitive"
    assert call_kwargs["volume"] == 0.8
    assert call_kwargs["thread_id"] == "security"
    assert call_kwargs["collapse_id"] == "motion-front"
    assert call_kwargs["category"] == "SECURITY"
    assert call_kwargs["source"] == "home-assistant"
    assert call_kwargs["source_display_name"] == "Home Assistant"
    assert call_kwargs["activity_slug"] == "ha-motion"
    assert call_kwargs["push"] is False


async def test_service_send_notification_push_defaults_true(hass: HomeAssistant) -> None:
    """send_notification defaults push to true when not specified."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {"title": "Test", "body": "Hello"},
        blocking=True,
    )

    call_kwargs = api.create_notification.call_args[1]
    assert call_kwargs["push"] is True


async def test_service_send_notification_level_critical_backward_compat(
    hass: HomeAssistant,
) -> None:
    """Automation YAMLs with level: critical must continue to validate and
    forward. `critical` is retained in NOTIFICATION_LEVELS for backward
    compatibility even though the UI selector no longer offers it; the server
    handles downgrade when required.
    """
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {"title": "Alert", "body": "Motion", "level": "critical"},
        blocking=True,
    )

    api.create_notification.assert_awaited_once()
    call_kwargs = api.create_notification.call_args[1]
    assert call_kwargs["level"] == "critical"


# --- update_activity new field tests ---


async def test_update_activity_service_passes_sound_top_level(hass: HomeAssistant) -> None:
    """sound is passed as a kwarg to api.update_activity, not in content."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ONGOING", "template": "generic", "sound": "chime"},
        blocking=True,
    )

    api.update_activity.assert_awaited_once()
    call_kwargs = api.update_activity.call_args.kwargs
    assert call_kwargs.get("sound") == "chime"
    content = api.update_activity.call_args[0][2]
    assert "sound" not in content


async def test_update_activity_service_passes_priority_top_level(hass: HomeAssistant) -> None:
    """priority is passed as a kwarg, not embedded in content."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ONGOING", "priority": 7},
        blocking=True,
    )

    api.update_activity.assert_awaited_once()
    call_kwargs = api.update_activity.call_args.kwargs
    assert call_kwargs.get("priority") == 7
    content = api.update_activity.call_args[0][2]
    assert "priority" not in content


async def test_update_activity_service_rejects_invalid_sound(hass: HomeAssistant) -> None:
    """Invalid sound value is rejected by the service schema."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises((vol.Invalid, Exception)):
        await hass.services.async_call(
            DOMAIN,
            "update_activity",
            {"slug": "x", "state": "ONGOING", "sound": "badvalue"},
            blocking=True,
        )


async def test_update_activity_service_rejects_priority_out_of_range(hass: HomeAssistant) -> None:
    """Priority > 10 is rejected by the service schema."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises((vol.Invalid, Exception)):
        await hass.services.async_call(
            DOMAIN,
            "update_activity",
            {"slug": "x", "state": "ONGOING", "priority": 11},
            blocking=True,
        )


async def test_update_activity_service_accepts_background_and_text_color(hass: HomeAssistant) -> None:
    """background_color and text_color appear in content dict passed to api."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ONGOING", "background_color": "#123456", "text_color": "red"},
        blocking=True,
    )

    api.update_activity.assert_awaited_once()
    content = api.update_activity.call_args[0][2]
    assert content["background_color"] == "#123456"
    assert content["text_color"] == "red"


async def test_update_activity_service_accepts_warning_threshold(hass: HomeAssistant) -> None:
    """warning_threshold appears in content dict."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ONGOING", "warning_threshold": 60},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["warning_threshold"] == 60


async def test_update_activity_service_accepts_step_labels_list(hass: HomeAssistant) -> None:
    """step_labels list appears in content dict."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ONGOING", "step_labels": ["Init", "Build"]},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["step_labels"] == ["Init", "Build"]


async def test_update_activity_service_accepts_alarm_bool(hass: HomeAssistant) -> None:
    """alarm bool appears in content dict."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ONGOING", "alarm": True},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["alarm"] is True


async def test_update_activity_service_accepts_fired_at(hass: HomeAssistant) -> None:
    """fired_at int appears in content dict."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ONGOING", "fired_at": 1700000000},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["fired_at"] == 1700000000


async def test_update_activity_service_accepts_units_dict(hass: HomeAssistant) -> None:
    """units dict appears in content dict."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ONGOING", "units": {"Temp": "°C"}},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["units"] == {"Temp": "°C"}


async def test_send_notification_service_accepts_url_media_icon_metadata(hass: HomeAssistant) -> None:
    """send_notification forwards url, media, icon_url, and metadata."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {
            "title": "Test",
            "body": "Hello",
            "url": "https://example.com",
            "media": {"url": "https://example.com/image.png", "type": "image"},
            "icon_url": "https://example.com/icon.png",
            "metadata": {"key": "value"},
        },
        blocking=True,
    )

    api.create_notification.assert_awaited_once()
    call_kwargs = api.create_notification.call_args[1]
    assert call_kwargs["url"] == "https://example.com"
    assert call_kwargs["media"] == {"url": "https://example.com/image.png", "type": "image"}
    assert call_kwargs["icon_url"] == "https://example.com/icon.png"
    assert call_kwargs["metadata"] == {"key": "value"}


async def test_send_notification_service_accepts_actions(hass: HomeAssistant) -> None:
    """send_notification forwards an `actions` list to the API client."""
    api = _mock_api()
    await _setup_entry(hass, api)

    actions = [
        {
            "id": "open",
            "title": "Open",
            "url": "https://example.com",
            "foreground": True,
        },
        {
            "id": "dismiss",
            "title": "Dismiss",
            "destructive": True,
        },
    ]

    await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {"title": "Test", "body": "Hello", "actions": actions},
        blocking=True,
    )

    api.create_notification.assert_awaited_once()
    call_kwargs = api.create_notification.call_args[1]
    assert call_kwargs["actions"] == actions


async def test_send_notification_service_rejects_invalid_media_type(hass: HomeAssistant) -> None:
    """media.type must be one of image/video/audio."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises((vol.Invalid, Exception)):
        await hass.services.async_call(
            DOMAIN,
            "send_notification",
            {
                "title": "Test",
                "body": "Hello",
                "media": {"url": "https://example.com/x.png", "type": "gif"},
            },
            blocking=True,
        )
