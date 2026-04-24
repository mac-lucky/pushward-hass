"""Tests for the PushWard activity manager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.pushward.activity_manager import (
    _ACTIVITY_LIMIT_NOTIFICATION_ID,
    ActivityManager,
    _forbidden_notification_id,
)
from custom_components.pushward.api import PushWardApiError, PushWardAuthError, PushWardForbiddenError
from custom_components.pushward.const import (
    CONF_ENDED_TTL,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_SOUND,
    CONF_STALE_TTL,
    CONF_UPDATE_INTERVAL,
)

from .conftest import make_entity_config as _entity_config


def _mock_api() -> AsyncMock:
    """Create a mock PushWard API client."""
    api = AsyncMock()
    api.create_activity = AsyncMock()
    api.update_activity = AsyncMock()
    api.delete_activity = AsyncMock()
    return api


def _mock_entry() -> MagicMock:
    """Create a mock ConfigEntry for the activity manager."""
    entry = MagicMock()
    entry.async_start_reauth = MagicMock()
    return entry


async def test_start_activity_on_state_change(hass: HomeAssistant) -> None:
    """Entity going from off→on triggers activity creation and ONGOING update."""
    api = _mock_api()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    # Set initial state to "off"
    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # Verify no activity started yet
    api.create_activity.assert_not_called()

    # Simulate state change to "on"
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    api.create_activity.assert_awaited_once()
    api.update_activity.assert_awaited_once()
    call_args = api.update_activity.call_args
    assert call_args[0][0] == "ha-washer"
    assert call_args[0][1] == "ONGOING"

    await manager.async_stop()


async def test_end_activity_two_phase(hass: HomeAssistant) -> None:
    """Two-phase end sends ONGOING (completion), then ENDED after delay."""
    api = _mock_api()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # Start activity
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    # Mark as active and reset mock to only capture end calls
    assert manager._tracked["binary_sensor.washer"].is_active
    api.reset_mock()

    # Directly call the two-phase end (avoids async_create_task timing)
    with patch(
        "custom_components.pushward.activity_manager.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        await manager._async_end_activity("binary_sensor.washer")

    # Should have: ONGOING (completion) + ENDED
    assert api.update_activity.await_count == 2
    calls = api.update_activity.call_args_list
    assert calls[0][0][1] == "ONGOING"
    assert calls[1][0][1] == "ENDED"
    assert not manager._tracked["binary_sensor.washer"].is_active

    await manager.async_stop()


async def test_throttled_update_dedup(hass: HomeAssistant) -> None:
    """Throttled update skips if content hasn't changed."""
    api = _mock_api()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # Start activity
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    initial_count = api.update_activity.await_count

    # Trigger send_update with same state — should be deduped
    await manager._send_update("binary_sensor.washer")

    # Should not have sent another update (same content)
    assert api.update_activity.await_count == initial_count

    await manager.async_stop()


async def test_resume_on_start(hass: HomeAssistant) -> None:
    """If entity is already in start state when manager starts, resume activity."""
    api = _mock_api()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    # Entity already "on" before manager starts
    hass.states.async_set("binary_sensor.washer", "on")
    await manager.async_start()
    await hass.async_block_till_done()

    api.create_activity.assert_awaited_once()
    api.update_activity.assert_awaited_once()

    await manager.async_stop()


async def test_stop_ends_all_active(hass: HomeAssistant) -> None:
    """async_stop sends ENDED for all active activities."""
    api = _mock_api()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "on")
    await manager.async_start()
    await hass.async_block_till_done()
    api.reset_mock()

    await manager.async_stop()

    # Should have sent ENDED
    api.update_activity.assert_awaited_once()
    call_args = api.update_activity.call_args
    assert call_args[0][0] == "ha-washer"
    assert call_args[0][1] == "ENDED"


async def test_rapid_on_off_cancels_end(hass: HomeAssistant) -> None:
    """Rapid on→off→on cancels the end task and keeps activity active."""
    api = _mock_api()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # Start activity
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    # End activity (schedules two-phase end with sleep)
    with patch(
        "custom_components.pushward.activity_manager.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        # Make sleep block so end task is pending
        sleep_event = asyncio.Event()
        mock_sleep.side_effect = lambda _: sleep_event.wait()

        hass.states.async_set("binary_sensor.washer", "off")
        await hass.async_block_till_done()

        # Quickly turn back on — should cancel end task
        hass.states.async_set("binary_sensor.washer", "on")
        await hass.async_block_till_done()

        # Release sleep to avoid dangling tasks
        sleep_event.set()
        await hass.async_block_till_done()

    tracked = manager._tracked["binary_sensor.washer"]
    assert tracked.is_active

    await manager.async_stop()


async def test_stale_end_skips_ended_after_restart(hass: HomeAssistant) -> None:
    """A stale end task should not send ENDED if the activity was restarted."""
    api = _mock_api()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # Start activity
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()
    api.reset_mock()

    tracked = manager._tracked["binary_sensor.washer"]

    # Mock sleep to simulate a restart happening during the delay:
    # when sleep is called, bump the generation as if _start_activity ran again.
    async def bump_generation_during_sleep(_seconds):
        tracked.generation += 1

    with patch(
        "custom_components.pushward.activity_manager.asyncio.sleep",
        side_effect=bump_generation_during_sleep,
    ):
        await manager._async_end_activity("binary_sensor.washer")

    # Should have sent phase 1 (ONGOING) but NOT phase 2 (ENDED)
    calls = api.update_activity.call_args_list
    assert len(calls) == 1
    assert calls[0][0][1] == "ONGOING"

    await manager.async_stop()


async def test_create_activity_with_custom_ttls(hass: HomeAssistant) -> None:
    """When TTLs are configured, they are passed to create_activity."""
    api = _mock_api()
    config = _entity_config(**{CONF_ENDED_TTL: 60, CONF_STALE_TTL: 120})
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "on")
    await manager.async_start()
    await hass.async_block_till_done()

    api.create_activity.assert_awaited_once()
    call_kwargs = api.create_activity.call_args
    assert call_kwargs[1]["ended_ttl"] == 60
    assert call_kwargs[1]["stale_ttl"] == 120

    await manager.async_stop()


async def test_create_activity_with_no_ttls(hass: HomeAssistant) -> None:
    """When TTLs are None, they are passed as None (server defaults)."""
    api = _mock_api()
    config = _entity_config(**{CONF_ENDED_TTL: None, CONF_STALE_TTL: None})
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "on")
    await manager.async_start()
    await hass.async_block_till_done()

    api.create_activity.assert_awaited_once()
    call_kwargs = api.create_activity.call_args
    assert call_kwargs[1]["ended_ttl"] is None
    assert call_kwargs[1]["stale_ttl"] is None

    await manager.async_stop()


async def test_create_activity_with_one_ttl(hass: HomeAssistant) -> None:
    """When only one TTL is configured, only that one is set."""
    api = _mock_api()
    config = _entity_config(**{CONF_ENDED_TTL: 600, CONF_STALE_TTL: None})
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "on")
    await manager.async_start()
    await hass.async_block_till_done()

    call_kwargs = api.create_activity.call_args
    assert call_kwargs[1]["ended_ttl"] == 600
    assert call_kwargs[1]["stale_ttl"] is None

    await manager.async_stop()


async def test_reload(hass: HomeAssistant) -> None:
    """async_reload stops old entities and starts new ones."""
    api = _mock_api()
    config1 = _entity_config()
    config2 = _entity_config(
        entity_id="switch.light",
        slug="ha-light",
        activity_name="Light",
        icon="lightbulb",
    )
    manager = ActivityManager(hass, api, [config1], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    hass.states.async_set("switch.light", "off")
    await manager.async_start()

    assert "binary_sensor.washer" in manager._tracked
    assert "switch.light" not in manager._tracked

    await manager.async_reload([config2])

    assert "binary_sensor.washer" not in manager._tracked
    assert "switch.light" in manager._tracked

    await manager.async_stop()


async def test_auth_error_triggers_reauth(hass: HomeAssistant) -> None:
    """PushWardAuthError during start triggers reauth flow."""
    api = _mock_api()
    api.create_activity = AsyncMock(side_effect=PushWardAuthError("bad key", status_code=401))
    entry = _mock_entry()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], entry)

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # Trigger state change → _start_activity → auth error
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    entry.async_start_reauth.assert_called_once_with(hass)
    await manager.async_stop()


async def test_auth_error_triggers_reauth_only_once(hass: HomeAssistant) -> None:
    """Multiple auth errors only trigger reauth once."""
    api = _mock_api()
    api.create_activity = AsyncMock(side_effect=PushWardAuthError("bad key", status_code=401))
    entry = _mock_entry()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], entry)

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # First auth error
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    # Second auth error (off → on again)
    hass.states.async_set("binary_sensor.washer", "off")
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    # Reauth triggered exactly once despite two failures
    entry.async_start_reauth.assert_called_once()
    await manager.async_stop()


async def test_auth_error_on_update_triggers_reauth(hass: HomeAssistant) -> None:
    """PushWardAuthError during ONGOING update triggers reauth."""
    api = _mock_api()
    entry = _mock_entry()
    config = _entity_config(**{CONF_UPDATE_INTERVAL: 0, CONF_PROGRESS_ATTRIBUTE: "brightness"})
    manager = ActivityManager(hass, api, [config], entry)

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # Start activity successfully
    hass.states.async_set("binary_sensor.washer", "on", {"brightness": 0})
    await hass.async_block_till_done()
    assert api.create_activity.call_count == 1

    # Now make update_activity fail with auth error
    api.update_activity = AsyncMock(side_effect=PushWardAuthError("bad key", status_code=403))

    # Trigger an update with changed content (progress changes → different content)
    hass.states.async_set("binary_sensor.washer", "on", {"brightness": 50})
    await hass.async_block_till_done()

    entry.async_start_reauth.assert_called_once_with(hass)
    await manager.async_stop()


# ---------------------------------------------------------------------------
# 409 activity limit tests
# ---------------------------------------------------------------------------


async def test_activity_limit_409_triggers_persistent_notification(hass: HomeAssistant) -> None:
    """409 on create_activity fires a persistent notification."""
    api = _mock_api()
    api.create_activity = AsyncMock(side_effect=PushWardApiError("limit", status_code=409))
    entry = _mock_entry()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], entry)

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    with patch("custom_components.pushward.activity_manager.persistent_notification.async_create") as mock_notify:
        hass.states.async_set("binary_sensor.washer", "on")
        await hass.async_block_till_done()

        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs.get("notification_id") == _ACTIVITY_LIMIT_NOTIFICATION_ID

    await manager.async_stop()


async def test_activity_limit_409_does_not_trigger_reauth(hass: HomeAssistant) -> None:
    """409 on create_activity must not trigger reauth."""
    api = _mock_api()
    api.create_activity = AsyncMock(side_effect=PushWardApiError("limit", status_code=409))
    entry = _mock_entry()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], entry)

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    with patch("custom_components.pushward.activity_manager.persistent_notification.async_create"):
        hass.states.async_set("binary_sensor.washer", "on")
        await hass.async_block_till_done()

    entry.async_start_reauth.assert_not_called()
    await manager.async_stop()


# ---------------------------------------------------------------------------
# 403 forbidden tests
# ---------------------------------------------------------------------------


async def test_forbidden_403_triggers_persistent_notification_not_reauth(hass: HomeAssistant) -> None:
    """403 on update_activity fires a persistent notification but not reauth."""
    api = _mock_api()
    entry = _mock_entry()
    config = _entity_config(**{CONF_UPDATE_INTERVAL: 0, CONF_PROGRESS_ATTRIBUTE: "brightness"})
    manager = ActivityManager(hass, api, [config], entry)

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # Start successfully
    hass.states.async_set("binary_sensor.washer", "on", {"brightness": 0})
    await hass.async_block_till_done()

    api.update_activity = AsyncMock(
        side_effect=PushWardForbiddenError("account owner's subscription is not active", status_code=403)
    )

    with patch("custom_components.pushward.activity_manager.persistent_notification.async_create") as mock_notify:
        hass.states.async_set("binary_sensor.washer", "on", {"brightness": 50})
        await hass.async_block_till_done()

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert call_args.kwargs.get("notification_id") == _forbidden_notification_id("ha-washer")
        message_arg = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("message", "")
        assert "subscription" in message_arg.lower() or "active" in message_arg.lower()

    entry.async_start_reauth.assert_not_called()
    await manager.async_stop()


async def test_forbidden_403_on_create_also_triggers_notification(hass: HomeAssistant) -> None:
    """403 from create_activity also fires a persistent notification."""
    api = _mock_api()
    api.create_activity = AsyncMock(side_effect=PushWardForbiddenError("subscription not active", status_code=403))
    entry = _mock_entry()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], entry)

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    with patch("custom_components.pushward.activity_manager.persistent_notification.async_create") as mock_notify:
        hass.states.async_set("binary_sensor.washer", "on")
        await hass.async_block_till_done()

        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs.get("notification_id") == _forbidden_notification_id("ha-washer")

    entry.async_start_reauth.assert_not_called()
    await manager.async_stop()


async def test_forbidden_403_on_end_also_triggers_notification(hass: HomeAssistant) -> None:
    """403 in _async_end_activity fires a notification and does not reauth."""
    api = _mock_api()
    entry = _mock_entry()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config], entry)

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # Start the activity
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    # Make all update_activity calls raise 403
    api.update_activity = AsyncMock(side_effect=PushWardForbiddenError("subscription not active", status_code=403))

    with patch("custom_components.pushward.activity_manager.persistent_notification.async_create") as mock_notify:
        with patch(
            "custom_components.pushward.activity_manager.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await manager._async_end_activity("binary_sensor.washer")

        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs.get("notification_id") == _forbidden_notification_id("ha-washer")

    entry.async_start_reauth.assert_not_called()
    await manager.async_stop()


# ---------------------------------------------------------------------------
# Regression: 401 must still trigger reauth
# ---------------------------------------------------------------------------


async def test_401_still_triggers_reauth(hass: HomeAssistant) -> None:
    """401 on update_activity still triggers reauth (regression guard)."""
    api = _mock_api()
    entry = _mock_entry()
    config = _entity_config(**{CONF_UPDATE_INTERVAL: 0, CONF_PROGRESS_ATTRIBUTE: "brightness"})
    manager = ActivityManager(hass, api, [config], entry)

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    hass.states.async_set("binary_sensor.washer", "on", {"brightness": 0})
    await hass.async_block_till_done()

    api.update_activity = AsyncMock(side_effect=PushWardAuthError("bad key", status_code=401))

    with patch("custom_components.pushward.activity_manager.persistent_notification.async_create") as mock_notify:
        hass.states.async_set("binary_sensor.washer", "on", {"brightness": 50})
        await hass.async_block_till_done()

        mock_notify.assert_not_called()

    entry.async_start_reauth.assert_called_once_with(hass)
    await manager.async_stop()


# ---------------------------------------------------------------------------
# Sound kwarg tests
# ---------------------------------------------------------------------------


async def test_sound_passed_to_update_activity_on_start(hass: HomeAssistant) -> None:
    """On start, ONGOING update_activity call includes sound from config."""
    api = _mock_api()
    config = _entity_config(**{CONF_SOUND: "chime"})
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    # The ONGOING PATCH after create_activity should carry sound="chime"
    api.update_activity.assert_awaited_once()
    assert api.update_activity.call_args.kwargs.get("sound") == "chime"

    await manager.async_stop()


async def test_sound_passed_to_update_activity_on_update(hass: HomeAssistant) -> None:
    """Throttled update PATCH also carries sound from config."""
    api = _mock_api()
    config = _entity_config(**{CONF_SOUND: "chime", CONF_UPDATE_INTERVAL: 0, CONF_PROGRESS_ATTRIBUTE: "brightness"})
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    # Start
    hass.states.async_set("binary_sensor.washer", "on", {"brightness": 0})
    await hass.async_block_till_done()
    api.reset_mock()

    # Trigger a content-changing update
    hass.states.async_set("binary_sensor.washer", "on", {"brightness": 50})
    await hass.async_block_till_done()

    api.update_activity.assert_awaited_once()
    assert api.update_activity.call_args.kwargs.get("sound") == "chime"

    await manager.async_stop()


async def test_empty_sound_passed_as_none(hass: HomeAssistant) -> None:
    """Empty string CONF_SOUND is coerced to None before passing to update_activity."""
    api = _mock_api()
    config = _entity_config(**{CONF_SOUND: ""})
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    api.update_activity.assert_awaited_once()
    assert api.update_activity.call_args.kwargs.get("sound") is None

    await manager.async_stop()


async def test_sound_not_passed_on_end(hass: HomeAssistant) -> None:
    """ENDED phase update_activity call does not include a sound kwarg (or has None)."""
    api = _mock_api()
    config = _entity_config(**{CONF_SOUND: "chime"})
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()
    api.reset_mock()

    with patch(
        "custom_components.pushward.activity_manager.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        await manager._async_end_activity("binary_sensor.washer")

    # Two calls: phase-1 ONGOING (completion) + phase-2 ENDED
    assert api.update_activity.await_count == 2
    calls = api.update_activity.call_args_list

    for call in calls:
        sound_kwarg = call.kwargs.get("sound")
        assert sound_kwarg is None or "sound" not in call.kwargs

    await manager.async_stop()
