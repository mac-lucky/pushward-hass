"""Tests for PushWard HA service registration and handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.api import (
    PushWardApiError,
    PushWardAuthError,
    PushWardEmailPermissionError,
)
from custom_components.pushward.const import (
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    DOMAIN,
)

from .conftest import make_usage_payload

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
    # async_setup_entry validates + seeds the usage coordinator via get_me.
    api.get_me = AsyncMock(return_value=make_usage_payload())
    api.create_activity = AsyncMock()
    api.update_activity = AsyncMock()
    api.delete_activity = AsyncMock()
    api.create_notification = AsyncMock()
    api.send_email = AsyncMock()
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
    assert hass.services.has_service(DOMAIN, "send_email")

    # Unload should remove services
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, "update_activity")
    assert not hass.services.has_service(DOMAIN, "create_activity")
    assert not hass.services.has_service(DOMAIN, "end_activity")
    assert not hass.services.has_service(DOMAIN, "delete_activity")
    assert not hass.services.has_service(DOMAIN, "send_notification")
    assert not hass.services.has_service(DOMAIN, "send_email")


async def test_service_update_activity(hass: HomeAssistant) -> None:
    """update_activity service calls api.update_activity with correct args."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "ha-washer", "state": "ongoing", "state_text": "Running", "progress": 0.5},
        blocking=True,
    )

    api.update_activity.assert_awaited_once()
    call_args = api.update_activity.call_args[0]
    assert call_args[0] == "ha-washer"
    assert call_args[1] == "ongoing"
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
    assert call_args[1] == "ended"
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
    """Auth error during setup puts entry in SETUP_ERROR and starts reauth.

    Setup validates the key via the usage coordinator's first refresh
    (GET /auth/me), so the failure is injected through get_me.
    """
    api = _mock_api()
    api.get_me = AsyncMock(side_effect=PushWardAuthError("bad key", status_code=401))

    entry = _mock_entry()
    entry.add_to_hass(hass)

    with patch("custom_components.pushward.PushWardApiClient", return_value=api):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )


async def test_setup_connection_error_retries(hass: HomeAssistant) -> None:
    """Connection error during setup puts entry in SETUP_RETRY."""
    api = _mock_api()
    api.get_me = AsyncMock(side_effect=PushWardApiError("timeout"))

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


# --- send_email service tests ---


async def test_service_send_email(hass: HomeAssistant) -> None:
    """send_email maps `body` -> text_body and forwards to/subject."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "send_email",
        {"to": "alerts@example.com", "subject": "Deploy done", "body": "Deployment succeeded."},
        blocking=True,
    )

    api.send_email.assert_awaited_once()
    call_kwargs = api.send_email.call_args[1]
    assert call_kwargs["to"] == "alerts@example.com"
    assert call_kwargs["subject"] == "Deploy done"
    assert call_kwargs["text_body"] == "Deployment succeeded."
    assert call_kwargs["html_body"] is None


async def test_service_send_email_html_only(hass: HomeAssistant) -> None:
    """send_email accepts an html_body without a plain-text body."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "send_email",
        {"to": "alerts@example.com", "subject": "Report", "html_body": "<p>Hi</p>"},
        blocking=True,
    )

    call_kwargs = api.send_email.call_args[1]
    assert call_kwargs["html_body"] == "<p>Hi</p>"
    assert call_kwargs["text_body"] is None


async def test_service_send_email_both_bodies(hass: HomeAssistant) -> None:
    """send_email forwards both bodies when given, mapping `body` -> text_body."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "send_email",
        {"to": "alerts@example.com", "subject": "Report", "body": "plain", "html_body": "<p>Hi</p>"},
        blocking=True,
    )

    call_kwargs = api.send_email.call_args[1]
    assert call_kwargs["text_body"] == "plain"
    assert call_kwargs["html_body"] == "<p>Hi</p>"


async def test_service_send_email_requires_a_body(hass: HomeAssistant) -> None:
    """Omitting both body and html_body fails schema validation."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "send_email",
            {"to": "alerts@example.com", "subject": "Empty"},
            blocking=True,
        )
    api.send_email.assert_not_awaited()


async def test_service_send_email_rejects_empty_bodies(hass: HomeAssistant) -> None:
    """An empty-string body is treated as absent — sending only `body: ""` fails."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "send_email",
            {"to": "alerts@example.com", "subject": "Empty", "body": ""},
            blocking=True,
        )
    api.send_email.assert_not_awaited()


async def test_service_send_email_drops_empty_text_body(hass: HomeAssistant) -> None:
    """An empty `body` alongside a valid html_body is coerced to None, not "" ."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "send_email",
        {"to": "alerts@example.com", "subject": "Report", "body": "", "html_body": "<p>Hi</p>"},
        blocking=True,
    )

    call_kwargs = api.send_email.call_args[1]
    assert call_kwargs["text_body"] is None
    assert call_kwargs["html_body"] == "<p>Hi</p>"


async def test_service_send_email_rejects_invalid_to(hass: HomeAssistant) -> None:
    """A malformed recipient address is rejected client-side."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "send_email",
            {"to": "not-an-email", "subject": "Hi", "body": "x"},
            blocking=True,
        )
    api.send_email.assert_not_awaited()


async def test_service_send_email_rejects_subject_with_line_breaks(hass: HomeAssistant) -> None:
    """CR/LF in the subject is rejected (email header-injection guard)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "send_email",
            {"to": "alerts@example.com", "subject": "Hi\r\nBcc: evil@example.com", "body": "x"},
            blocking=True,
        )
    api.send_email.assert_not_awaited()


async def test_service_send_email_permission_error_becomes_validation_error(hass: HomeAssistant) -> None:
    """A 403 from the API surfaces as a clean ServiceValidationError, not a raw exception."""
    api = _mock_api()
    api.send_email = AsyncMock(
        side_effect=PushWardEmailPermissionError(
            "recipient is not a verified address for this account", status_code=403
        )
    )
    await _setup_entry(hass, api)

    with pytest.raises(ServiceValidationError, match="verified address"):
        await hass.services.async_call(
            DOMAIN,
            "send_email",
            {"to": "alerts@example.com", "subject": "Hi", "body": "x"},
            blocking=True,
        )


# --- update_activity new field tests ---


async def test_update_activity_service_passes_sound_top_level(hass: HomeAssistant) -> None:
    """sound is passed as a kwarg to api.update_activity, not in content."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ongoing", "template": "generic", "sound": "chime"},
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
        {"slug": "x", "state": "ongoing", "priority": 7},
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
            {"slug": "x", "state": "ongoing", "sound": "badvalue"},
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
            {"slug": "x", "state": "ongoing", "priority": 11},
            blocking=True,
        )


async def test_update_activity_service_accepts_background_and_text_color(hass: HomeAssistant) -> None:
    """background_color and text_color appear in content dict passed to api."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ongoing", "background_color": "#123456", "text_color": "red"},
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
        {"slug": "x", "state": "ongoing", "warning_threshold": 60},
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
        {"slug": "x", "state": "ongoing", "step_labels": ["Init", "Build"]},
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
        {"slug": "x", "state": "ongoing", "alarm": True},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["alarm"] is True


async def test_update_activity_service_accepts_snooze_seconds(hass: HomeAssistant) -> None:
    """snooze_seconds int appears in content dict."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ongoing", "alarm": True, "snooze_seconds": 600},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["snooze_seconds"] == 600


async def test_update_activity_service_rejects_out_of_range_snooze_seconds(
    hass: HomeAssistant,
) -> None:
    """snooze_seconds outside 60-3600 is rejected by the service schema."""
    api = _mock_api()
    await _setup_entry(hass, api)

    for bad in (59, 3601):
        with pytest.raises(vol.MultipleInvalid):
            await hass.services.async_call(
                DOMAIN,
                "update_activity",
                {"slug": "x", "state": "ongoing", "alarm": True, "snooze_seconds": bad},
                blocking=True,
            )


async def test_update_activity_service_accepts_fired_at(hass: HomeAssistant) -> None:
    """fired_at int appears in content dict."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ongoing", "fired_at": 1700000000},
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
        {"slug": "x", "state": "ongoing", "units": {"Temp": "°C"}},
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
