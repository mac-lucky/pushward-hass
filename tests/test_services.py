"""Tests for PushWard HA service registration and handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward import async_remove_entry
from custom_components.pushward.api import (
    PushWardApiError,
    PushWardAuthError,
    PushWardEmailPermissionError,
    PushWardForbiddenError,
)
from custom_components.pushward.const import (
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    DOMAIN,
    MAX_URL_LEN,
    SUBENTRY_TYPE_WIDGET,
    TEMPLATES,
    validate_tap_action_url,
)
from custom_components.pushward.widget_manager import WidgetManager

from .conftest import make_usage_payload, make_widget_config
from .server_contract import assert_valid_activity_content

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
    api.delete_widget = AsyncMock()
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


_BASE_SERVICES = (
    "update_activity",
    "create_activity",
    "end_activity",
    "delete_activity",
    "send_notification",
    "send_email",
)


async def test_services_registered_on_setup(hass: HomeAssistant) -> None:
    """Services register at component setup and persist after an entry unloads.

    They are registered in async_setup (component lifetime), not per entry, so unloading
    the only entry must not remove them — automations keep validating against them.
    """
    api = _mock_api()
    entry = await _setup_entry(hass, api)

    for name in _BASE_SERVICES:
        assert hass.services.has_service(DOMAIN, name)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    for name in _BASE_SERVICES:
        assert hass.services.has_service(DOMAIN, name)


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
    assert call_kwargs["dismissal_ttl"] is None


async def test_service_create_activity_with_ttls(hass: HomeAssistant) -> None:
    """create_activity service passes explicit TTLs when provided."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "create_activity",
        {
            "slug": "ha-washer",
            "name": "Washer",
            "priority": 1,
            "ended_ttl": 60,
            "stale_ttl": 120,
            "dismissal_ttl": 0,
        },
        blocking=True,
    )

    call_kwargs = api.create_activity.call_args[1]
    assert call_kwargs["ended_ttl"] == 60
    assert call_kwargs["stale_ttl"] == 120
    # 0 = immediate Lock Screen removal; must pass through, not be treated as unset.
    assert call_kwargs["dismissal_ttl"] == 0


async def test_service_create_activity_accepts_ttl_bounds(hass: HomeAssistant) -> None:
    """The exact bounds are accepted, so an off-by-one in vol.Range can't hide.

    The rejection test below only proves out-of-range fails; without this an
    accidental max=14399 would pass the whole suite.
    """
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "create_activity",
        {
            "slug": "ha-bounds",
            "name": "Bounds",
            "dismissal_ttl": 14400,
            "ended_ttl": 1,
            "stale_ttl": 2592000,
        },
        blocking=True,
    )

    call_kwargs = api.create_activity.call_args[1]
    assert call_kwargs["dismissal_ttl"] == 14400
    assert call_kwargs["ended_ttl"] == 1
    assert call_kwargs["stale_ttl"] == 2592000


async def test_service_create_activity_rejects_out_of_range_dismissal_ttl(
    hass: HomeAssistant,
) -> None:
    """dismissal_ttl outside the server's 0-14400 bound is rejected by the schema.

    services.yaml's number selector only constrains the UI; automations, scripts and
    REST callers hit the schema directly.
    """
    api = _mock_api()
    await _setup_entry(hass, api)

    for bad in (-1, 14401):
        with pytest.raises(vol.MultipleInvalid):
            await hass.services.async_call(
                DOMAIN,
                "create_activity",
                {"slug": "ha-washer", "name": "Washer", "dismissal_ttl": bad},
                blocking=True,
            )
        api.create_activity.assert_not_awaited()


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

    with pytest.raises(vol.MultipleInvalid):
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

    with pytest.raises(vol.MultipleInvalid):
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


# --- per-template update_activity_<template> services ---


async def test_per_template_services_registered_and_persist(hass: HomeAssistant) -> None:
    """All six update_activity_<template> services register on setup and persist after unload."""
    api = _mock_api()
    entry = await _setup_entry(hass, api)

    for template in TEMPLATES:
        assert hass.services.has_service(DOMAIN, f"update_activity_{template}")

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    for template in TEMPLATES:
        assert hass.services.has_service(DOMAIN, f"update_activity_{template}")


@pytest.mark.parametrize("template", TEMPLATES)
async def test_update_activity_template_injects_template(hass: HomeAssistant, template: str) -> None:
    """The service name implies the template; the handler injects it and remaps state_text -> state."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        f"update_activity_{template}",
        {"slug": "x", "state": "ongoing", "state_text": "Running"},
        blocking=True,
    )

    api.update_activity.assert_awaited_once()
    slug, state, content = api.update_activity.call_args[0]
    assert (slug, state) == ("x", "ongoing")
    assert content["template"] == template
    assert content["state"] == "Running"
    assert "state_text" not in content


@pytest.mark.parametrize("template", TEMPLATES)
async def test_per_template_keeps_sound_priority_top_level(hass: HomeAssistant, template: str) -> None:
    """Every template's schema accepts sound/priority and keeps them out of content."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        f"update_activity_{template}",
        {"slug": "x", "state": "ongoing", "sound": "chime", "priority": 7},
        blocking=True,
    )

    call = api.update_activity.call_args
    content = call[0][2]
    assert "sound" not in content and "priority" not in content
    assert call.kwargs["sound"] == "chime"
    assert call.kwargs["priority"] == 7


async def test_update_activity_countdown_forwards_fields(hass: HomeAssistant) -> None:
    """Countdown-specific fields go into content; sound/priority stay top-level kwargs."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_countdown",
        {
            "slug": "c",
            "state": "ongoing",
            "end_date": 1700000000,
            "warning_threshold": 30,
            "alarm": True,
            "snooze_seconds": 600,
            "completion_message": "Done",
            "sound": "chime",
            "priority": 7,
        },
        blocking=True,
    )

    call = api.update_activity.call_args
    content = call[0][2]
    assert content["template"] == "countdown"
    assert content["end_date"] == 1700000000
    assert content["warning_threshold"] == 30
    assert content["alarm"] is True
    assert content["snooze_seconds"] == 600
    assert content["completion_message"] == "Done"
    assert "sound" not in content and "priority" not in content
    assert call.kwargs["sound"] == "chime"
    assert call.kwargs["priority"] == 7


async def test_update_activity_alert_forwards_fields(hass: HomeAssistant) -> None:
    """Alert-specific fields (severity, fired_at, url, secondary_url) are forwarded into content."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_alert",
        {
            "slug": "a",
            "state": "ongoing",
            "severity": "critical",
            "fired_at": 1700000000,
            "url": "https://example.com",
            "secondary_url": "https://example.com/details",
        },
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["template"] == "alert"
    assert content["severity"] == "critical"
    assert content["fired_at"] == 1700000000
    assert content["url"] == "https://example.com"
    assert content["secondary_url"] == "https://example.com/details"


async def test_update_activity_gauge_forwards_fields(hass: HomeAssistant) -> None:
    """Gauge-specific fields (value, min/max, unit) are forwarded into content."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_gauge",
        {"slug": "g", "state": "ongoing", "value": 22.5, "min_value": 0, "max_value": 100, "unit": "°C"},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["template"] == "gauge"
    assert content["value"] == 22.5
    assert content["min_value"] == 0
    assert content["max_value"] == 100
    assert content["unit"] == "°C"


async def test_update_activity_gauge_rejects_dict_value(hass: HomeAssistant) -> None:
    """A gauge value must be a single number, not the timeline dict form."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "update_activity_gauge",
            {"slug": "g", "state": "ongoing", "value": {"Temp": 22.5}},
            blocking=True,
        )
    api.update_activity.assert_not_awaited()


async def test_update_activity_timeline_forwards_fields(hass: HomeAssistant) -> None:
    """Timeline-specific fields (units, primary_series, scale, decimals, smoothing, thresholds) are forwarded."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_timeline",
        {
            "slug": "t",
            "state": "ongoing",
            "value": {"Temp": 22.5, "Humidity": 40},
            "units": {"Temp": "°C"},
            "primary_series": "Humidity",
            "scale": "linear",
            "decimals": 2,
            "smoothing": True,
            "thresholds": [10, 20],
        },
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["template"] == "timeline"
    assert content["units"] == {"Temp": "°C"}
    assert content["primary_series"] == "Humidity"
    assert content["scale"] == "linear"
    assert content["decimals"] == 2
    assert content["smoothing"] is True
    assert content["thresholds"] == [10, 20]


async def test_update_activity_steps_accepts_list_step_labels(hass: HomeAssistant) -> None:
    """step_labels is an ordered list and is forwarded verbatim (matches the server contract)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_steps",
        {"slug": "s", "state": "ongoing", "total_steps": 3, "step_labels": ["A", "B", "C"]},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["template"] == "steps"
    assert content["step_labels"] == ["A", "B", "C"]


async def test_update_activity_steps_accepts_weights_and_colors(hass: HomeAssistant) -> None:
    """step_weights/step_colors are ordered lists forwarded verbatim, empties included."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_steps",
        {
            "slug": "s",
            "state": "ongoing",
            "total_steps": 3,
            "step_weights": [1, 2.5, 1],
            "step_colors": ["green", "", "red"],
        },
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["step_weights"] == [1, 2.5, 1]
    assert content["step_colors"] == ["green", "", "red"]


async def test_update_activity_steps_rejects_dict_step_labels(hass: HomeAssistant) -> None:
    """A dict for step_labels is rejected — the schema requires a list (the old docs were wrong)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "update_activity_steps",
            {"slug": "s", "state": "ongoing", "step_labels": {"1": "A", "2": "B"}},
            blocking=True,
        )
    api.update_activity.assert_not_awaited()


async def test_update_activity_gauge_has_no_countdown_fields(hass: HomeAssistant) -> None:
    """Per-template schemas are scoped: a countdown field is rejected by the gauge service."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "update_activity_gauge",
            {"slug": "g", "state": "ongoing", "end_date": 1700000000},
            blocking=True,
        )
    api.update_activity.assert_not_awaited()


async def test_deprecated_update_activity_raises_repair_issue(hass: HomeAssistant) -> None:
    """The legacy update_activity action still forwards, and surfaces a deprecation Repair issue."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {"slug": "x", "state": "ongoing", "template": "generic"},
        blocking=True,
    )

    api.update_activity.assert_awaited_once()
    slug, state, content = api.update_activity.call_args[0]
    assert (slug, state) == ("x", "ongoing")
    assert content["template"] == "generic"

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "deprecated_update_activity")
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING


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

    with pytest.raises(vol.MultipleInvalid):
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


# --- countdown duration / start_date ---


async def test_update_activity_countdown_forwards_duration_string(hass: HomeAssistant) -> None:
    """A duration string (e.g. "30m") is forwarded verbatim — the server expands it."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_countdown",
        {"slug": "c", "state": "ongoing", "duration": "1h30m"},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["duration"] == "1h30m"


async def test_update_activity_countdown_forwards_duration_int_and_start_date(hass: HomeAssistant) -> None:
    """An integer duration and an explicit start_date are forwarded into content."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_countdown",
        {"slug": "c", "state": "ongoing", "duration": 1800, "start_date": 1700000000},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["duration"] == 1800
    assert content["start_date"] == 1700000000


async def test_update_activity_countdown_rejects_zero_duration(hass: HomeAssistant) -> None:
    """An integer duration must be >= 1 second."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "update_activity_countdown",
            {"slug": "c", "state": "ongoing", "duration": 0},
            blocking=True,
        )
    api.update_activity.assert_not_awaited()


# --- universal action fields (all templates that render button slots) ---

# board/log use a lean schema: only the whole-activity tap_action, no url /
# secondary_url / url_action / secondary_url_action slots (board uses per-tile
# url_action; log has no buttons).
_UNIVERSAL_ACTION_TEMPLATES = [t for t in TEMPLATES if t not in ("board", "log")]


@pytest.mark.parametrize("template", _UNIVERSAL_ACTION_TEMPLATES)
async def test_action_fields_forwarded_on_every_template(hass: HomeAssistant, template: str) -> None:
    """url / secondary_url / tap_action / url_action / secondary_url_action reach content on all templates."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        f"update_activity_{template}",
        {
            "slug": "x",
            "state": "ongoing",
            "url": "https://example.com",
            "secondary_url": "https://example.com/more",
            "tap_action": {"url": "homeassistant://navigate/lovelace/0"},
            "url_action": {"url": "https://example.com", "title": "Open", "method": "POST", "body": "go"},
            "secondary_url_action": {"url": "https://example.com/more", "title": "More"},
        },
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["url"] == "https://example.com"
    assert content["secondary_url"] == "https://example.com/more"
    assert content["tap_action"] == {"url": "homeassistant://navigate/lovelace/0"}
    assert content["url_action"]["method"] == "POST"
    assert content["url_action"]["title"] == "Open"
    assert content["secondary_url_action"]["title"] == "More"


@pytest.mark.parametrize("template", ["board", "log"])
async def test_board_log_accept_only_tap_action(hass: HomeAssistant, template: str) -> None:
    """board/log forward the whole-activity tap_action but reject the button-slot fields."""
    api = _mock_api()
    await _setup_entry(hass, api)

    # tap_action (whole-activity) is accepted and forwarded.
    await hass.services.async_call(
        DOMAIN,
        f"update_activity_{template}",
        {
            "slug": "x",
            "state": "ongoing",
            "tap_action": {"url": "homeassistant://navigate/lovelace/0"},
        },
        blocking=True,
    )
    content = api.update_activity.call_args[0][2]
    assert content["tap_action"] == {"url": "homeassistant://navigate/lovelace/0"}

    # The button-slot fields are not part of the lean schema → rejected.
    for field in ("url", "secondary_url", "url_action", "secondary_url_action", "progress", "remaining_time"):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                f"update_activity_{template}",
                {"slug": "x", "state": "ongoing", field: "https://example.com"},
                blocking=True,
            )


async def test_log_line_at_rejects_non_positive(hass: HomeAssistant) -> None:
    """update_activity_log rejects a non-positive `at` locally (server requires a positive timestamp)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    for bad_at in (0, -5):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                "update_activity_log",
                {"slug": "x", "state": "ongoing", "lines": [{"text": "boom", "at": bad_at}]},
                blocking=True,
            )

    # A positive `at` passes and is forwarded unchanged.
    await hass.services.async_call(
        DOMAIN,
        "update_activity_log",
        {"slug": "x", "state": "ongoing", "lines": [{"text": "ok", "at": 1735689600}]},
        blocking=True,
    )
    content = api.update_activity.call_args[0][2]
    assert content["lines"][0]["at"] == 1735689600


async def test_tap_action_accepts_custom_scheme_url(hass: HomeAssistant) -> None:
    """A custom-scheme tap_action url (homeassistant://) is accepted — unlike the http-only url field of old."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_generic",
        {"slug": "x", "state": "ongoing", "tap_action": {"url": "homeassistant://navigate/lovelace/0"}},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["tap_action"]["url"] == "homeassistant://navigate/lovelace/0"


async def test_tap_action_rejects_http_fields_on_custom_scheme(hass: HomeAssistant) -> None:
    """method/headers/body require an http(s) url — they're rejected on a custom scheme."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "update_activity_generic",
            {"slug": "x", "state": "ongoing", "tap_action": {"url": "homeassistant://x", "method": "POST"}},
            blocking=True,
        )
    api.update_activity.assert_not_awaited()


async def test_tap_action_rejects_dangerous_scheme(hass: HomeAssistant) -> None:
    """A javascript: url is rejected outright (mirrors the server)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "update_activity_generic",
            {"slug": "x", "state": "ongoing", "tap_action": {"url": "javascript:alert(1)"}},
            blocking=True,
        )
    api.update_activity.assert_not_awaited()


async def test_url_action_rejects_bad_method(hass: HomeAssistant) -> None:
    """An unknown HTTP method is rejected by the schema enum."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "update_activity_generic",
            {"slug": "x", "state": "ongoing", "url_action": {"url": "https://example.com", "method": "FETCH"}},
            blocking=True,
        )
    api.update_activity.assert_not_awaited()


async def test_url_action_lowercase_method_is_upper_cased(hass: HomeAssistant) -> None:
    """A lowercase method is normalized to upper-case (vol.Upper) before forwarding."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_generic",
        {"slug": "x", "state": "ongoing", "url_action": {"url": "https://example.com", "method": "post"}},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["url_action"]["method"] == "POST"


# --- timeline history seed ---


async def test_update_activity_timeline_forwards_history(hass: HomeAssistant) -> None:
    """The optional timeline history seed is forwarded into content."""
    api = _mock_api()
    await _setup_entry(hass, api)

    history = {"Temp": [{"timestamp": 1700000000, "value": 21.0}]}
    await hass.services.async_call(
        DOMAIN,
        "update_activity_timeline",
        {"slug": "t", "state": "ongoing", "value": {"Temp": 22.5}, "history": history},
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["history"] == history


# --- notification action HTTP webhook fields ---


async def test_send_notification_action_http_fields_forwarded(hass: HomeAssistant) -> None:
    """A notification action button can carry method/headers/body for a silent webhook."""
    api = _mock_api()
    await _setup_entry(hass, api)

    actions = [
        {
            "id": "ack",
            "title": "Acknowledge",
            "url": "https://example.com/ack",
            "method": "POST",
            "headers": {"X-Token": "abc"},
            "body": "ok",
        }
    ]
    await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {"title": "t", "body": "b", "actions": actions},
        blocking=True,
    )

    call_kwargs = api.create_notification.call_args[1]
    assert call_kwargs["actions"][0]["method"] == "POST"
    assert call_kwargs["actions"][0]["headers"] == {"X-Token": "abc"}
    assert call_kwargs["actions"][0]["body"] == "ok"


async def test_send_notification_action_rejects_method_without_http_url(hass: HomeAssistant) -> None:
    """method on an action with no http(s) url is rejected (the server requires an http shape)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "send_notification",
            {"title": "t", "body": "b", "actions": [{"id": "a", "title": "A", "method": "POST"}]},
            blocking=True,
        )
    api.create_notification.assert_not_awaited()


async def test_send_notification_action_reply_with_text(hass: HomeAssistant) -> None:
    """text_input on a silent http(s) action passes through to the API."""
    api = _mock_api()
    await _setup_entry(hass, api)

    actions = [
        {
            "id": "reply",
            "title": "Reply",
            "url": "https://example.com/reply",
            "method": "POST",
            "text_input": True,
            "text_input_placeholder": "Type a reply",
            "text_input_button_title": "Send",
        }
    ]
    await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {"title": "t", "body": "b", "actions": actions},
        blocking=True,
    )

    action = api.create_notification.call_args[1]["actions"][0]
    assert action["text_input"] is True
    assert action["text_input_placeholder"] == "Type a reply"
    assert action["text_input_button_title"] == "Send"


async def test_send_notification_text_input_labels_reject_oversize(hass: HomeAssistant) -> None:
    """text_input_placeholder / text_input_button_title over 64 chars are rejected by the caps."""
    api = _mock_api()
    await _setup_entry(hass, api)

    for bad in (
        {"id": "r", "title": "R", "url": "https://x", "text_input": True, "text_input_placeholder": "p" * 65},
        {"id": "r", "title": "R", "url": "https://x", "text_input": True, "text_input_button_title": "b" * 65},
    ):
        with pytest.raises(vol.MultipleInvalid):
            await hass.services.async_call(
                DOMAIN,
                "send_notification",
                {"title": "t", "body": "b", "actions": [bad]},
                blocking=True,
            )
    api.create_notification.assert_not_awaited()


async def test_send_notification_action_text_input_requires_silent_http(hass: HomeAssistant) -> None:
    """text_input on a foreground or non-http action is rejected (mirrors the server)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    for bad in (
        # foreground action cannot carry a reply field
        {"id": "a", "title": "A", "url": "https://example.com", "foreground": True, "text_input": True},
        # custom-scheme action has no http shape for the reply webhook
        {"id": "a", "title": "A", "url": "homeassistant://navigate/0", "text_input": True},
        # placeholder without text_input
        {"id": "a", "title": "A", "url": "https://example.com", "text_input_placeholder": "x"},
    ):
        with pytest.raises(vol.MultipleInvalid):
            await hass.services.async_call(
                DOMAIN,
                "send_notification",
                {"title": "t", "body": "b", "actions": [bad]},
                blocking=True,
            )
    api.create_notification.assert_not_awaited()


# --- delete_widget service ---


async def test_delete_widget_service_registered(hass: HomeAssistant) -> None:
    """delete_widget registers at setup and persists after the entry unloads."""
    api = _mock_api()
    entry = await _setup_entry(hass, api)
    assert hass.services.has_service(DOMAIN, "delete_widget")

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "delete_widget")


async def test_delete_widget_by_slug(hass: HomeAssistant) -> None:
    """delete_widget with a slug calls api.delete_widget directly."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "delete_widget",
        {"slug": "ha-temp"},
        blocking=True,
    )

    api.delete_widget.assert_awaited_once_with("ha-temp")


async def test_delete_widget_unknown_entity_raises(hass: HomeAssistant) -> None:
    """delete_widget with an entity_id that no tracked widget owns raises a validation error."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "delete_widget",
            {"entity_id": "sensor.nope"},
            blocking=True,
        )
    api.delete_widget.assert_not_awaited()


async def test_delete_widget_by_entity_id_resolves_and_deletes(hass: HomeAssistant) -> None:
    """delete_widget with an entity_id resolves the bound widget's slug and deletes it."""
    api = _mock_api()
    api.create_widget = AsyncMock()
    api.patch_widget = AsyncMock()
    entry = await _setup_entry(hass, api)

    # Seed a tracked widget bound to an entity and expose it via hass.data so the service's
    # _find_widget_slug → slug_for_entity path has something to resolve.
    hass.states.async_set("sensor.users", "42")
    manager = WidgetManager(hass, api, [make_widget_config(slug="ha-users", entity_id="sensor.users")], entry)
    await manager.async_start()
    hass.data[DOMAIN][entry.entry_id]["widget_manager"] = manager

    await hass.services.async_call(DOMAIN, "delete_widget", {"entity_id": "sensor.users"}, blocking=True)

    api.delete_widget.assert_awaited_once_with("ha-users")
    await manager.async_stop()


async def test_async_remove_entry_deletes_server_widgets(hass: HomeAssistant) -> None:
    """Removing the whole integration deletes every tracked widget server-side (no orphan leak)."""
    api = _mock_api()
    subentries = [
        ConfigSubentryData(
            data=make_widget_config(slug=slug, entity_id=f"sensor.{slug}"),
            subentry_type=SUBENTRY_TYPE_WIDGET,
            title=slug,
            unique_id=slug,
        )
        for slug in ("ha-one", "ha-two")
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY},
        version=2,
        unique_id=DOMAIN,
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)

    with patch("custom_components.pushward.PushWardApiClient", return_value=api):
        await async_remove_entry(hass, entry)

    assert {c.args[0] for c in api.delete_widget.await_args_list} == {"ha-one", "ha-two"}


async def test_action_objects_satisfy_server_contract(hass: HomeAssistant) -> None:
    """The action objects the service forwards are accepted by the public server contract."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_generic",
        {
            "slug": "x",
            "state": "ongoing",
            "url": "https://example.com",
            "secondary_url": "homeassistant://navigate/lovelace/0",
            "tap_action": {"url": "homeassistant://navigate/lovelace/0"},
            "url_action": {"url": "https://example.com", "title": "Open", "method": "POST", "body": "go"},
            "secondary_url_action": {"url": "https://example.com/more", "title": "More"},
        },
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    # Would raise PushWardContractError if the server would reject the emitted action shapes.
    assert_valid_activity_content({**content, "template": "generic"})


async def test_timeline_history_seed_satisfies_server_contract(hass: HomeAssistant) -> None:
    """The timeline history seed the service forwards is contract-valid."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_timeline",
        {
            "slug": "t",
            "state": "ongoing",
            "value": {"Temp": 22.5},
            "history": {"Temp": [{"timestamp": 1700000000, "value": 21.0}]},
        },
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert_valid_activity_content({**content, "template": "timeline"})


def test_validate_tap_action_url_rejection_branches() -> None:
    """validate_tap_action_url rejects schemeless, hostless http(s), dangerous, and over-long URLs."""
    for bad in ("example.com", "http://", "https://", "javascript:alert(1)", "https://x/" + "a" * MAX_URL_LEN):
        with pytest.raises(vol.Invalid):
            validate_tap_action_url(bad)
    # Accepts http(s) with a host and any non-blocked custom scheme.
    assert validate_tap_action_url("https://example.com") == "https://example.com"
    assert validate_tap_action_url("homeassistant://x") == "homeassistant://x"


async def test_url_action_rejects_oversized_fields(hass: HomeAssistant) -> None:
    """body > 1024, title > 64, and icon > 64 are rejected by the schema caps."""
    api = _mock_api()
    await _setup_entry(hass, api)

    for action in (
        {"url": "https://example.com", "body": "x" * 1025},
        {"url": "https://example.com", "title": "t" * 65},
        {"url": "https://example.com", "icon": "i" * 65},
    ):
        with pytest.raises(vol.MultipleInvalid):
            await hass.services.async_call(
                DOMAIN,
                "update_activity_generic",
                {"slug": "x", "state": "ongoing", "url_action": action},
                blocking=True,
            )
    api.update_activity.assert_not_awaited()


async def test_url_action_rejects_http_fields_on_custom_scheme(hass: HomeAssistant) -> None:
    """method/headers/body on a url_action require an http(s) url (covers the url_action gate)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    with pytest.raises(vol.MultipleInvalid):
        await hass.services.async_call(
            DOMAIN,
            "update_activity_generic",
            {"slug": "x", "state": "ongoing", "url_action": {"url": "homeassistant://x", "method": "POST"}},
            blocking=True,
        )
    api.update_activity.assert_not_awaited()


async def test_action_headers_forwarded_and_validated(hass: HomeAssistant) -> None:
    """Valid headers on a tap_action forward; bad name, control chars, and oversize reject."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_generic",
        {
            "slug": "x",
            "state": "ongoing",
            "tap_action": {"url": "https://example.com", "headers": {"X-Token": "abc"}},
        },
        blocking=True,
    )
    assert api.update_activity.call_args[0][2]["tap_action"]["headers"] == {"X-Token": "abc"}

    for headers in (
        {"Bad Name": "v"},  # space is not an RFC 7230 token char
        {"X-Evil": "line1\r\nline2"},  # CR/LF forbidden
        {"X-Big": "v" * 1100},  # > 1024 bytes total
    ):
        with pytest.raises(vol.MultipleInvalid):
            await hass.services.async_call(
                DOMAIN,
                "update_activity_generic",
                {"slug": "x", "state": "ongoing", "tap_action": {"url": "https://example.com", "headers": headers}},
                blocking=True,
            )


async def test_update_activity_countdown_rejects_invalid_duration_string(hass: HomeAssistant) -> None:
    """A non-positive or malformed duration string is rejected (mirrors server ParseDuration)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    for bad in ("0", "0s", "abc", "1x", "-5"):
        with pytest.raises(vol.MultipleInvalid):
            await hass.services.async_call(
                DOMAIN,
                "update_activity_countdown",
                {"slug": "c", "state": "ongoing", "duration": bad},
                blocking=True,
            )
    api.update_activity.assert_not_awaited()


async def test_update_activity_countdown_accepts_plain_seconds_string(hass: HomeAssistant) -> None:
    """A plain-integer duration string is forwarded verbatim (server treats it as seconds)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity_countdown",
        {"slug": "c", "state": "ongoing", "duration": "90"},
        blocking=True,
    )

    assert api.update_activity.call_args[0][2]["duration"] == "90"


async def test_send_notification_action_accepts_custom_scheme_url(hass: HomeAssistant) -> None:
    """A notification action button can deep-link via a custom scheme (server parity)."""
    api = _mock_api()
    await _setup_entry(hass, api)

    actions = [{"id": "open", "title": "Open", "url": "homeassistant://navigate/lovelace/0"}]
    await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {"title": "t", "body": "b", "actions": actions},
        blocking=True,
    )

    assert api.create_notification.call_args[1]["actions"][0]["url"] == "homeassistant://navigate/lovelace/0"


async def test_deprecated_update_activity_accepts_action_and_template_fields(hass: HomeAssistant) -> None:
    """The deprecated update_activity union still accepts the new action + countdown fields."""
    api = _mock_api()
    await _setup_entry(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "update_activity",
        {
            "slug": "x",
            "state": "ongoing",
            "template": "countdown",
            "duration": "30m",
            "tap_action": {"url": "homeassistant://x"},
            "url_action": {"url": "https://example.com", "method": "POST"},
        },
        blocking=True,
    )

    content = api.update_activity.call_args[0][2]
    assert content["duration"] == "30m"
    assert content["tap_action"] == {"url": "homeassistant://x"}
    assert content["url_action"]["method"] == "POST"


# --- service error surfacing tests ---


async def test_update_activity_api_error_becomes_home_assistant_error(hass: HomeAssistant) -> None:
    """A generic API failure surfaces the server message, not a bare exception.

    Previously the handler had no try/except, so the failure reached the UI as
    "Unknown error". It now raises HomeAssistantError carrying the real reason.
    """
    api = _mock_api()
    api.update_activity = AsyncMock(side_effect=PushWardApiError("activity not found"))
    await _setup_entry(hass, api)

    with pytest.raises(HomeAssistantError, match="activity not found") as exc:
        await hass.services.async_call(
            DOMAIN,
            "update_activity",
            {"slug": "missing", "state": "ongoing"},
            blocking=True,
        )
    # A generic failure must NOT be downgraded to the user-fixable tier
    # (ServiceValidationError subclasses HomeAssistantError, so the match above alone
    # would still pass if the mapping regressed).
    assert not isinstance(exc.value, ServiceValidationError)


async def test_update_activity_forbidden_becomes_validation_error(hass: HomeAssistant) -> None:
    """A 403 is user-fixable, so it surfaces as a clean ServiceValidationError."""
    api = _mock_api()
    api.update_activity = AsyncMock(
        side_effect=PushWardForbiddenError("integration key lacks the required capability", status_code=403)
    )
    await _setup_entry(hass, api)

    with pytest.raises(ServiceValidationError, match="capability"):
        await hass.services.async_call(
            DOMAIN,
            "update_activity",
            {"slug": "x", "state": "ongoing"},
            blocking=True,
        )


async def test_create_activity_api_error_becomes_home_assistant_error(hass: HomeAssistant) -> None:
    """create_activity also surfaces the server message instead of a bare exception."""
    api = _mock_api()
    api.create_activity = AsyncMock(side_effect=PushWardApiError("slug already exists"))
    await _setup_entry(hass, api)

    with pytest.raises(HomeAssistantError, match="slug already exists") as exc:
        await hass.services.async_call(
            DOMAIN,
            "create_activity",
            {"slug": "dup", "name": "Dup", "priority": 1},
            blocking=True,
        )
    assert not isinstance(exc.value, ServiceValidationError)


async def test_create_activity_forbidden_becomes_validation_error(hass: HomeAssistant) -> None:
    """create_activity's 403 tier surfaces as a clean ServiceValidationError."""
    api = _mock_api()
    api.create_activity = AsyncMock(
        side_effect=PushWardForbiddenError("integration key lacks the required capability", status_code=403)
    )
    await _setup_entry(hass, api)

    with pytest.raises(ServiceValidationError, match="capability"):
        await hass.services.async_call(
            DOMAIN,
            "create_activity",
            {"slug": "x", "name": "X", "priority": 1},
            blocking=True,
        )


async def test_end_activity_api_error_becomes_home_assistant_error(hass: HomeAssistant) -> None:
    """end_activity routes through the shared error guard (it calls update_activity)."""
    api = _mock_api()
    api.update_activity = AsyncMock(side_effect=PushWardApiError("activity not found"))
    await _setup_entry(hass, api)

    with pytest.raises(HomeAssistantError, match="activity not found") as exc:
        await hass.services.async_call(DOMAIN, "end_activity", {"slug": "missing"}, blocking=True)
    assert not isinstance(exc.value, ServiceValidationError)


async def test_delete_activity_api_error_becomes_home_assistant_error(hass: HomeAssistant) -> None:
    """delete_activity surfaces the server message instead of a bare exception."""
    api = _mock_api()
    api.delete_activity = AsyncMock(side_effect=PushWardApiError("activity not found"))
    await _setup_entry(hass, api)

    with pytest.raises(HomeAssistantError, match="activity not found") as exc:
        await hass.services.async_call(DOMAIN, "delete_activity", {"slug": "missing"}, blocking=True)
    assert not isinstance(exc.value, ServiceValidationError)


async def test_send_notification_api_error_becomes_home_assistant_error(hass: HomeAssistant) -> None:
    """send_notification surfaces the server message instead of a bare exception."""
    api = _mock_api()
    api.create_notification = AsyncMock(side_effect=PushWardApiError("notification rejected"))
    await _setup_entry(hass, api)

    with pytest.raises(HomeAssistantError, match="notification rejected") as exc:
        await hass.services.async_call(
            DOMAIN,
            "send_notification",
            {"title": "t", "body": "b"},
            blocking=True,
        )
    assert not isinstance(exc.value, ServiceValidationError)


async def test_send_email_api_error_becomes_home_assistant_error(hass: HomeAssistant) -> None:
    """send_email's non-403 tier surfaces as HomeAssistantError (403 is covered separately)."""
    api = _mock_api()
    api.send_email = AsyncMock(side_effect=PushWardApiError("smtp temporarily unavailable"))
    await _setup_entry(hass, api)

    with pytest.raises(HomeAssistantError, match="smtp temporarily unavailable") as exc:
        await hass.services.async_call(
            DOMAIN,
            "send_email",
            {"to": "a@b.com", "subject": "s", "body": "x"},
            blocking=True,
        )
    assert not isinstance(exc.value, ServiceValidationError)
