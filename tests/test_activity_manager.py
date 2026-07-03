"""Tests for the PushWard activity manager."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.pushward.activity_manager import (
    _ACTIVITY_LIMIT_NOTIFICATION_ID,
    ActivityManager,
    TrackedEntity,
    _companion_entity_ids,
    _forbidden_notification_id,
)
from custom_components.pushward.api import PushWardApiError, PushWardAuthError, PushWardForbiddenError
from custom_components.pushward.const import (
    CONF_END_STATES,
    CONF_ENDED_TTL,
    CONF_ENTITY_ID,
    CONF_HISTORY_PERIOD,
    CONF_LABEL,
    CONF_LOG_COLUMNS,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SERIES_ENTITIES,
    CONF_SLUG,
    CONF_SOUND,
    CONF_STALE_TTL,
    CONF_START_STATES,
    CONF_TEMPLATE,
    CONF_TILES,
    CONF_UNIT,
    CONF_UPDATE_INTERVAL,
    CONF_VALUE_ENTITY,
    LOG_MAX_LINES,
)

from .conftest import activity_updates, bump_state, end_activity_via_state
from .conftest import make_entity_config as _entity_config
from .server_contract import assert_valid_activity_content


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
    assert call_args[0][1] == "ongoing"

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
    assert calls[0][0][1] == "ongoing"
    assert calls[1][0][1] == "ended"
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
    assert call_args[0][1] == "ended"


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
    assert calls[0][0][1] == "ongoing"

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


# ---------------------------------------------------------------------------
# Companion value entity tests
# ---------------------------------------------------------------------------


def _companion_config():
    """Countdown config whose remaining time comes from a separate entity."""
    return _entity_config(
        **{
            CONF_TEMPLATE: "countdown",
            CONF_REMAINING_TIME_ENTITY: "sensor.washer_time",
            CONF_UPDATE_INTERVAL: 0,
        }
    )


async def test_companion_entity_subscribed(hass: HomeAssistant) -> None:
    """A configured companion value entity is subscribed alongside the primary."""
    api = _mock_api()
    manager = ActivityManager(hass, api, [_companion_config()], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    hass.states.async_set("sensor.washer_time", "1000")
    await manager.async_start()

    tracked = manager._tracked["binary_sensor.washer"]
    assert len(tracked.companion_unsubs) == 1

    await manager.async_stop()


async def test_companion_change_updates_active_activity(hass: HomeAssistant) -> None:
    """A companion value change refreshes an active activity."""
    api = _mock_api()
    manager = ActivityManager(hass, api, [_companion_config()], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    hass.states.async_set("sensor.washer_time", "1000")
    await manager.async_start()

    # Start the activity via the primary entity.
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()
    count_after_start = api.update_activity.await_count
    assert count_after_start >= 1

    # Change only the companion → activity should refresh with new remaining_time.
    hass.states.async_set("sensor.washer_time", "500")
    await hass.async_block_till_done()

    assert api.update_activity.await_count == count_after_start + 1
    last_content = api.update_activity.call_args[0][2]
    assert last_content["remaining_time"] == 500

    await manager.async_stop()


async def test_companion_change_ignored_when_inactive(hass: HomeAssistant) -> None:
    """A companion change does not start an activity or send updates when inactive."""
    api = _mock_api()
    manager = ActivityManager(hass, api, [_companion_config()], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    hass.states.async_set("sensor.washer_time", "1000")
    await manager.async_start()

    # Primary never enters a start state; only the companion changes.
    hass.states.async_set("sensor.washer_time", "500")
    await hass.async_block_till_done()

    api.create_activity.assert_not_called()
    api.update_activity.assert_not_called()

    await manager.async_stop()


async def test_companion_unsubscribed_on_stop(hass: HomeAssistant) -> None:
    """async_stop releases the real companion subscriptions set up by async_start."""
    api = _mock_api()
    manager = ActivityManager(hass, api, [_companion_config()], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    hass.states.async_set("sensor.washer_time", "1000")
    await manager.async_start()

    tracked = manager._tracked["binary_sensor.washer"]
    # Don't bypass the wiring: real subscriptions must exist, then wrap each real
    # unsub in a call-through spy so the assertion exercises subscribe→unsubscribe.
    assert tracked.companion_unsubs
    spies = [MagicMock(side_effect=u) for u in tracked.companion_unsubs]
    tracked.companion_unsubs = spies

    await manager.async_stop()

    for spy in spies:
        spy.assert_called_once()
    assert tracked.companion_unsubs == []


def test_companion_entity_ids_dedup() -> None:
    """The same entity in two companion fields yields a single subscription target."""
    config = _entity_config(
        **{
            CONF_REMAINING_TIME_ENTITY: "sensor.shared",
            CONF_VALUE_ENTITY: "sensor.shared",
        }
    )
    assert _companion_entity_ids(config) == ["sensor.shared"]


async def test_companion_equal_primary_not_subscribed(hass: HomeAssistant) -> None:
    """A companion field pointing at the tracked entity itself is not separately subscribed."""
    config = _entity_config(
        **{
            CONF_TEMPLATE: "countdown",
            CONF_REMAINING_TIME_ENTITY: "binary_sensor.washer",  # == the primary entity
            CONF_UPDATE_INTERVAL: 0,
        }
    )
    api = _mock_api()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    await manager.async_start()

    tracked = manager._tracked["binary_sensor.washer"]
    assert tracked.companion_unsubs == []

    await manager.async_stop()


async def test_companion_duration_unit_end_to_end(hass: HomeAssistant) -> None:
    """Real hass + real State: an LG-style duration companion (minutes) is parsed to seconds."""
    config = _entity_config(
        **{
            CONF_TEMPLATE: "countdown",
            CONF_REMAINING_TIME_ENTITY: "sensor.washer_time",
            CONF_UPDATE_INTERVAL: 0,
        }
    )
    api = _mock_api()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("binary_sensor.washer", "off")
    # A real duration sensor: numeric state + device_class/unit attributes.
    hass.states.async_set("sensor.washer_time", "25", {"device_class": "duration", "unit_of_measurement": "min"})
    await manager.async_start()

    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    last_content = api.update_activity.call_args[0][2]
    assert last_content["remaining_time"] == 25 * 60

    await manager.async_stop()


async def test_companion_timestamp_anchors_end_date_end_to_end(hass: HomeAssistant) -> None:
    """Real hass + real State: a timestamp finish-time companion anchors end_date to the absolute epoch."""
    config = _entity_config(
        **{
            CONF_TEMPLATE: "countdown",
            CONF_REMAINING_TIME_ENTITY: "sensor.washer_finish",
            CONF_UPDATE_INTERVAL: 0,
        }
    )
    api = _mock_api()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    finish = dt_util.utcnow() + timedelta(minutes=30)
    hass.states.async_set("binary_sensor.washer", "off")
    # A real timestamp sensor exposes its state as an ISO 8601 string.
    hass.states.async_set("sensor.washer_finish", finish.isoformat(), {"device_class": "timestamp"})
    await manager.async_start()

    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    last_content = api.update_activity.call_args[0][2]
    # end_date is the absolute finish epoch (drift-free), independent of "now".
    assert last_content["end_date"] == int(finish.timestamp())
    assert 0 < last_content["remaining_time"] <= 30 * 60

    await manager.async_stop()


# ---------------------------------------------------------------------------
# Board template tests
# ---------------------------------------------------------------------------


def _board_config():
    """Board config: an anchor entity owns lifecycle; tiles bind to separate entities.

    ``binary_sensor.home`` drives start/end via the default on/off states, while the
    two tiles read ``sensor.cpu`` and ``binary_sensor.door`` (tracked as companions).
    """
    return _entity_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.home",
            CONF_SLUG: "ha-board",
            CONF_TEMPLATE: "board",
            CONF_START_STATES: ["on"],
            CONF_END_STATES: ["off"],
            CONF_TILES: [
                {CONF_LABEL: "CPU", CONF_ENTITY_ID: "sensor.cpu", CONF_UNIT: "%"},
                {CONF_LABEL: "Door", CONF_ENTITY_ID: "binary_sensor.door"},
            ],
        }
    )


async def test_board_start_sends_tiles(hass: HomeAssistant) -> None:
    """A board's anchor entering a start state emits ONGOING content with both tiles."""
    api = _mock_api()
    config = _board_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    # Seed tile values + hold the anchor in an end state so nothing starts on setup.
    hass.states.async_set("sensor.cpu", "42")
    hass.states.async_set("binary_sensor.door", "on")
    hass.states.async_set("binary_sensor.home", "off")
    await manager.async_start()
    api.create_activity.assert_not_called()

    # Anchor enters the start state → activity is created with the tile snapshot.
    hass.states.async_set("binary_sensor.home", "on")
    await hass.async_block_till_done()

    ongoing = activity_updates(api, "ongoing")
    assert ongoing, "expected an ONGOING update when the board starts"
    content = ongoing[-1]
    assert content["progress"] == 0.0
    by_label = {tile["label"]: tile for tile in content["tiles"]}
    assert set(by_label) == {"CPU", "Door"}
    assert by_label["CPU"]["value"] == "42"
    assert by_label["CPU"]["unit"] == "%"
    assert by_label["Door"]["value"] == "on"
    assert_valid_activity_content(content)

    await manager.async_stop()


async def test_board_refreshes_on_tile_change(hass: HomeAssistant) -> None:
    """Changing a tile entity refreshes the active board with the new tile value."""
    api = _mock_api()
    config = _board_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("sensor.cpu", "42")
    hass.states.async_set("binary_sensor.door", "on")
    hass.states.async_set("binary_sensor.home", "off")
    await manager.async_start()

    # Tile entities are companions, not lifecycle drivers.
    assert "sensor.cpu" in _companion_entity_ids(config)

    hass.states.async_set("binary_sensor.home", "on")
    await hass.async_block_till_done()
    ongoing_before = len(activity_updates(api, "ongoing"))

    # Drive a tile change through the real companion subscription (cooldown reset).
    await bump_state(manager, hass, "binary_sensor.home", "sensor.cpu", "99", {})

    ongoing_after = activity_updates(api, "ongoing")
    assert len(ongoing_after) == ongoing_before + 1
    by_label = {tile["label"]: tile for tile in ongoing_after[-1]["tiles"]}
    assert by_label["CPU"]["value"] == "99"
    assert_valid_activity_content(ongoing_after[-1])

    await manager.async_stop()


async def test_board_two_phase_end_carries_tiles(hass: HomeAssistant) -> None:
    """The ENDED board frame carries the last tiles (server requires >=1 tile)."""
    api = _mock_api()
    config = _board_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("sensor.cpu", "42")
    hass.states.async_set("binary_sensor.door", "on")
    hass.states.async_set("binary_sensor.home", "off")
    await manager.async_start()

    hass.states.async_set("binary_sensor.home", "on")
    await hass.async_block_till_done()
    assert manager._tracked["binary_sensor.home"].is_active

    await end_activity_via_state(manager, hass, "binary_sensor.home", "off", {})

    ended = activity_updates(api, "ended")
    assert ended, "expected an ENDED frame"
    content = ended[-1]
    assert content["tiles"], "ENDED board frame must carry tiles"
    by_label = {tile["label"]: tile for tile in content["tiles"]}
    assert by_label["CPU"]["value"] == "42"
    assert_valid_activity_content(content)
    assert not manager._tracked["binary_sensor.home"].is_active

    await manager.async_stop()


async def test_board_defers_start_when_all_tiles_unavailable(hass: HomeAssistant) -> None:
    """A board with no renderable tile defers its start (no tile-less create), then
    starts once a tile entity becomes available (server requires >=1 tile)."""
    api = _mock_api()
    config = _board_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    # Anchor already in the start state, but both tile entities are unavailable —
    # the classic post-restart ordering where the anchor resumes before sensors init.
    hass.states.async_set("sensor.cpu", "unavailable")
    hass.states.async_set("binary_sensor.door", "unavailable")
    hass.states.async_set("binary_sensor.home", "on")
    await manager.async_start()
    await hass.async_block_till_done()

    # No tile-less activity created; start is deferred.
    api.create_activity.assert_not_called()
    assert manager._tracked["binary_sensor.home"].is_active is False

    # A tile becomes available → the deferred start fires with that tile.
    hass.states.async_set("sensor.cpu", "73")
    await hass.async_block_till_done()

    api.create_activity.assert_called()
    assert manager._tracked["binary_sensor.home"].is_active is True
    ongoing = activity_updates(api, "ongoing")
    assert ongoing, "expected an ONGOING update once a tile became available"
    by_label = {tile["label"]: tile for tile in ongoing[-1]["tiles"]}
    assert by_label["CPU"]["value"] == "73"
    assert_valid_activity_content(ongoing[-1])

    await manager.async_stop()


async def test_board_drops_tile_when_companion_unavailable(hass: HomeAssistant) -> None:
    """A tile entity going unavailable refreshes the active board and drops that tile,
    rather than leaving the stale last value on screen."""
    api = _mock_api()
    config = _board_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("sensor.cpu", "42")
    hass.states.async_set("binary_sensor.door", "on")
    hass.states.async_set("binary_sensor.home", "off")
    await manager.async_start()

    hass.states.async_set("binary_sensor.home", "on")
    await hass.async_block_till_done()
    assert manager._tracked["binary_sensor.home"].is_active

    # CPU tile goes unavailable → board re-renders without it (Door remains).
    await bump_state(manager, hass, "binary_sensor.home", "sensor.cpu", "unavailable", {})

    ongoing = activity_updates(api, "ongoing")
    labels = {tile["label"] for tile in ongoing[-1]["tiles"]}
    assert "CPU" not in labels, "unavailable tile must be dropped, not left stale"
    assert "Door" in labels
    assert_valid_activity_content(ongoing[-1])

    await manager.async_stop()


# ---------------------------------------------------------------------------
# Log template tests
# ---------------------------------------------------------------------------


def _log_config():
    """Log config: each state change appends to a newest-first ring buffer."""
    return _entity_config(
        **{
            CONF_ENTITY_ID: "sensor.events",
            CONF_SLUG: "ha-log",
            CONF_TEMPLATE: "log",
            CONF_START_STATES: ["on"],
            CONF_END_STATES: ["off"],
        }
    )


async def test_log_appends_line_per_state_change_and_caps_at_20(hass: HomeAssistant) -> None:
    """The log buffer accumulates on every change (even inactive) and caps at LOG_MAX_LINES."""
    api = _mock_api()
    config = _log_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    eid = "sensor.events"
    hass.states.async_set(eid, "boot")
    await manager.async_start()

    # Drive 25 DISTINCT changes — HA only fires on a real change. None match the
    # start states, so the activity stays inactive while the buffer still grows.
    for i in range(25):
        hass.states.async_set(eid, f"event_{i}")
        await hass.async_block_till_done()

    assert manager._tracked[eid].is_active is False
    assert len(manager._tracked[eid].log_buffer) == LOG_MAX_LINES == 20

    await manager.async_stop()


async def test_log_start_sends_lines_newest_first(hass: HomeAssistant) -> None:
    """Starting a log activity emits ONGOING content with the buffer newest-first."""
    api = _mock_api()
    config = _log_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    eid = "sensor.events"
    hass.states.async_set(eid, "off")
    await manager.async_start()

    for value in ("1", "2", "3"):
        hass.states.async_set(eid, value)
        await hass.async_block_till_done()

    hass.states.async_set(eid, "on")
    await hass.async_block_till_done()
    assert manager._tracked[eid].is_active

    ongoing = activity_updates(api, "ongoing")
    assert ongoing, "expected an ONGOING update when the log starts"
    content = ongoing[-1]
    lines = content["lines"]
    assert isinstance(lines, list) and lines
    texts = [line["text"] for line in lines]
    # Newest-first: the most recent state ("On") leads, the seed ("Off") trails.
    assert texts[0] == "On"
    assert texts == ["On", "3", "2", "1", "Off"]
    assert texts == [line["text"] for line in manager._tracked[eid].log_buffer]
    assert_valid_activity_content(content)

    await manager.async_stop()


async def test_log_two_phase_end_carries_lines(hass: HomeAssistant) -> None:
    """The ENDED log frame carries the last lines (server requires >=1 line)."""
    api = _mock_api()
    config = _log_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    eid = "sensor.events"
    hass.states.async_set(eid, "off")
    await manager.async_start()

    for value in ("1", "2"):
        hass.states.async_set(eid, value)
        await hass.async_block_till_done()

    hass.states.async_set(eid, "on")
    await hass.async_block_till_done()
    assert manager._tracked[eid].is_active

    await end_activity_via_state(manager, hass, eid, "off", {})

    ended = activity_updates(api, "ended")
    assert ended, "expected an ENDED frame"
    content = ended[-1]
    lines = content["lines"]
    assert isinstance(lines, list) and lines, "ENDED log frame must carry lines"
    assert_valid_activity_content(content)
    assert not manager._tracked[eid].is_active

    await manager.async_stop()


async def test_log_collapses_consecutive_same_text(hass: HomeAssistant) -> None:
    """Consecutive lines with identical text+level collapse to one, regardless of `at`.

    A log tracks state *changes*: an entity re-reporting the same displayed state
    (turn-on attribute churn, periodic re-reports, the restart re-seed whose
    last_updated is regenerated) is not a new event and must not spam duplicates.
    The first occurrence's timestamp is kept.
    """
    api = _mock_api()
    config = _log_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())

    tracked = TrackedEntity(config=config)
    hass.states.async_set("sensor.events", "on")
    state = hass.states.get("sensor.events")

    manager._record_log_sample(tracked, state)
    assert len(tracked.log_buffer) == 1
    first_at = tracked.log_buffer[0]["at"]

    # A re-report with the SAME text but a fresh/older `at` is collapsed (covers
    # both turn-on attribute churn and the cross-restart re-seed).
    tracked.log_buffer[0] = {**tracked.log_buffer[0], "at": 1}
    manager._record_log_sample(tracked, state)
    assert len(tracked.log_buffer) == 1, "consecutive identical line was not collapsed"
    assert tracked.log_buffer[0]["at"] == 1, "kept the first occurrence, not the re-report"
    assert first_at != 1  # sanity: the re-report really did carry a different epoch

    # A genuine change (distinct text) still appends.
    hass.states.async_set("sensor.events", "off")
    manager._record_log_sample(tracked, hass.states.get("sensor.events"))
    assert len(tracked.log_buffer) == 2


async def test_log_rehydration_collapses_persisted_duplicates(hass: HomeAssistant) -> None:
    """Consecutive same-text lines persisted by older builds collapse on load."""
    api = _mock_api()
    config = _log_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())
    eid = "sensor.events"
    hass.states.async_set(eid, "on")

    # Newest-first persisted buffer with three duplicate "On" lines (the bug a
    # pre-fix build wrote on every turn-on / restart).
    persisted = [
        {"text": "On", "at": 30},
        {"text": "On", "at": 20},
        {"text": "On", "at": 10},
        {"text": "Off", "at": 5},
    ]
    with patch.object(manager, "_async_load_history", AsyncMock(return_value=({}, {eid: persisted}))):
        await manager.async_start()

    texts = [line["text"] for line in manager._tracked[eid].log_buffer]
    # Three "On" collapse to one (keeping the earliest, at=30); "Off" kept; the
    # current-state seed ("On") collapses into the head.
    assert texts == ["On", "Off"]
    assert manager._tracked[eid].log_buffer[0]["at"] == 30

    await manager.async_stop()


def _log_columns_config(columns):
    """Log config tracking a light, with the given log_columns."""
    return _entity_config(
        **{
            CONF_ENTITY_ID: "light.lamp",
            CONF_SLUG: "ha-lamp",
            CONF_TEMPLATE: "log",
            CONF_START_STATES: ["on"],
            CONF_END_STATES: ["off"],
            CONF_LOG_COLUMNS: columns,
        }
    )


async def test_log_columns_attribute_change_yields_distinct_line(hass: HomeAssistant) -> None:
    """With an attribute column, a brightness-only change is a distinct line, not collapsed.

    The state stays "on" (which would collapse to a single "On"), but the composed
    text changes because the brightness column moved — so a new line is appended.
    """
    api = _mock_api()
    config = _log_columns_config([{"attribute": "brightness"}])
    manager = ActivityManager(hass, api, [config], _mock_entry())

    eid = "light.lamp"
    hass.states.async_set(eid, "on", {"brightness": 153})
    await manager.async_start()
    assert manager._tracked[eid].is_active

    hass.states.async_set(eid, "on", {"brightness": 200})
    await hass.async_block_till_done()

    texts = [line["text"] for line in manager._tracked[eid].log_buffer]
    assert texts == ["On · 200", "On · 153"]

    await manager.async_stop()


async def test_log_refreshes_on_column_entity_change(hass: HomeAssistant) -> None:
    """A change to a column entity appends a freshly composed line via the companion path."""
    api = _mock_api()
    config = _log_columns_config([{CONF_ENTITY_ID: "sensor.cpu"}])
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("sensor.cpu", "42")
    hass.states.async_set("light.lamp", "on")
    await manager.async_start()
    assert manager._tracked["light.lamp"].is_active

    # The column entity is a companion (not a lifecycle driver).
    assert "sensor.cpu" in _companion_entity_ids(config)

    ongoing_before = len(activity_updates(api, "ongoing"))

    # Drive a column change through the real companion subscription (cooldown reset).
    await bump_state(manager, hass, "light.lamp", "sensor.cpu", "99", {})

    ongoing_after = activity_updates(api, "ongoing")
    assert len(ongoing_after) == ongoing_before + 1
    texts = [line["text"] for line in ongoing_after[-1]["lines"]]
    assert texts[0] == "On · 99"
    assert_valid_activity_content(ongoing_after[-1])

    await manager.async_stop()


async def test_log_columns_collapse_when_composed_text_unchanged(hass: HomeAssistant) -> None:
    """Collapse still holds when the composed text is unchanged (re-report of the same values)."""
    api = _mock_api()
    config = _log_columns_config([{"attribute": "brightness"}])
    manager = ActivityManager(hass, api, [config], _mock_entry())

    tracked = TrackedEntity(config=config)
    hass.states.async_set("light.lamp", "on", {"brightness": 153})
    state = hass.states.get("light.lamp")

    manager._record_log_sample(tracked, state)
    assert len(tracked.log_buffer) == 1
    assert tracked.log_buffer[0]["text"] == "On · 153"

    # Same state + same brightness → same composed text → collapsed.
    manager._record_log_sample(tracked, state)
    assert len(tracked.log_buffer) == 1

    # A real brightness change makes the composed text distinct → appended.
    hass.states.async_set("light.lamp", "on", {"brightness": 200})
    manager._record_log_sample(tracked, hass.states.get("light.lamp"))
    assert len(tracked.log_buffer) == 2
    assert tracked.log_buffer[0]["text"] == "On · 200"


# ---------------------------------------------------------------------------
# Timeline history backfill (recorder seed)
# ---------------------------------------------------------------------------


def _timeline_numeric_config(**overrides):
    """Timeline config for a plain numeric sensor (recorder-eligible backfill)."""
    return _entity_config(
        **{
            CONF_ENTITY_ID: "sensor.power",
            CONF_SLUG: "ha-power",
            CONF_TEMPLATE: "timeline",
            CONF_START_STATES: [],
            CONF_END_STATES: [],
            CONF_HISTORY_PERIOD: 30,
            **overrides,
        }
    )


def _recorder_state(value: str, ts: int):
    """A minimal recorder row with a real ``last_updated`` datetime."""
    return SimpleNamespace(state=value, last_updated=dt_util.utc_from_timestamp(ts))


async def test_seed_timeline_recorder_labels_match_live_series(hass: HomeAssistant) -> None:
    """Recorder-seeded points land in the same series label as live values (the
    tracked entity's friendly name), not under the raw entity_id."""
    api = _mock_api()
    config = _timeline_numeric_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())
    hass.states.async_set("sensor.power", "42.0", {"friendly_name": "Grid Power"})
    await manager.async_start()

    now = int(time.time())
    manager._recorder_states = AsyncMock(
        return_value={
            "sensor.power": [{"timestamp": now - 600, "value": 40.0}, {"timestamp": now - 300, "value": 41.0}]
        }
    )
    history = await manager._seed_timeline_history("sensor.power", config, {})

    assert history is not None
    assert set(history) == {"Grid Power"}
    assert "sensor.power" not in history

    await manager.async_stop()


async def test_seed_timeline_recorder_queries_value_entity(hass: HomeAssistant) -> None:
    """With a value entity configured, the recorder is queried for THAT entity,
    while points still land in the tracked entity's series label."""
    api = _mock_api()
    config = _timeline_numeric_config(
        **{CONF_ENTITY_ID: "sensor.meter", CONF_SLUG: "ha-meter", CONF_VALUE_ENTITY: "sensor.power_raw"}
    )
    manager = ActivityManager(hass, api, [config], _mock_entry())
    hass.states.async_set("sensor.meter", "reading", {"friendly_name": "Meter"})
    hass.states.async_set("sensor.power_raw", "42.0")
    await manager.async_start()

    recorder = AsyncMock(return_value={"sensor.power_raw": [{"timestamp": int(time.time()) - 300, "value": 40.0}]})
    manager._recorder_states = recorder
    history = await manager._seed_timeline_history("sensor.meter", config, {})

    recorder.assert_awaited_once()
    assert recorder.await_args.args[0] == ["sensor.power_raw"]
    assert set(history) == {"Meter"}

    await manager.async_stop()


async def test_seed_timeline_merges_buffer_and_recorder(hass: HomeAssistant) -> None:
    """The recorder is not skipped when the buffer already has a point: both
    sources' points appear, deduped and sorted by timestamp."""
    api = _mock_api()
    config = _timeline_numeric_config()
    manager = ActivityManager(hass, api, [config], _mock_entry())
    hass.states.async_set("sensor.power", "42.0", {"friendly_name": "Grid Power"})
    await manager.async_start()

    tracked = manager._tracked["sensor.power"]
    buffer_ts = tracked.history_buffer[-1][0]
    manager._recorder_states = AsyncMock(
        return_value={
            "sensor.power": [
                {"timestamp": buffer_ts - 600, "value": 40.0},
                {"timestamp": buffer_ts - 300, "value": 41.0},
            ]
        }
    )
    history = await manager._seed_timeline_history("sensor.power", config, {})

    timestamps = [p["timestamp"] for p in history["Grid Power"]]
    assert timestamps == sorted(timestamps)
    assert {buffer_ts, buffer_ts - 600, buffer_ts - 300} <= set(timestamps)

    await manager.async_stop()


async def test_seed_timeline_series_entities_batched_and_labeled(hass: HomeAssistant) -> None:
    """Per-entity series seed from ONE batched recorder query, each entity's
    points landing under that series' frozen label."""
    api = _mock_api()
    config = _timeline_numeric_config(
        **{
            CONF_SERIES_ENTITIES: [
                {CONF_LABEL: "Bedroom", CONF_ENTITY_ID: "sensor.bedroom_pm25"},
                {CONF_LABEL: "Office", CONF_ENTITY_ID: "sensor.office_pm25"},
            ],
        }
    )
    manager = ActivityManager(hass, api, [config], _mock_entry())
    hass.states.async_set("sensor.power", "0", {"friendly_name": "Anchor"})
    hass.states.async_set("sensor.bedroom_pm25", "12")
    hass.states.async_set("sensor.office_pm25", "8")
    await manager.async_start()

    now = int(time.time())
    recorder = AsyncMock(
        return_value={
            "sensor.bedroom_pm25": [{"timestamp": now - 300, "value": 11.0}],
            "sensor.office_pm25": [{"timestamp": now - 300, "value": 7.0}],
        }
    )
    manager._recorder_states = recorder
    history = await manager._seed_timeline_history("sensor.power", config, {})

    recorder.assert_awaited_once()
    # Anchor's own state is NOT recorder-seeded, only the two series entities.
    assert set(recorder.await_args.args[0]) == {"sensor.bedroom_pm25", "sensor.office_pm25"}
    assert {"Bedroom", "Office"} <= set(history)
    assert any(p["value"] == 11.0 for p in history["Bedroom"])
    assert any(p["value"] == 7.0 for p in history["Office"])

    await manager.async_stop()


async def test_recorder_states_returns_points_per_entity(hass: HomeAssistant) -> None:
    """The batched recorder query returns ascending {timestamp, value} points
    per entity, skipping unavailable / non-numeric rows."""
    api = _mock_api()
    manager = ActivityManager(hass, api, [_timeline_numeric_config()], _mock_entry())

    t0 = int(time.time()) - 600
    rows = {
        "sensor.a": [
            _recorder_state("40.0", t0),
            _recorder_state("unavailable", t0 + 60),
            _recorder_state("not-a-number", t0 + 120),
            _recorder_state("41.5", t0 + 180),
        ],
        "sensor.b": [_recorder_state("unknown", t0)],
    }
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value=rows)
    with patch("homeassistant.helpers.recorder.get_instance", return_value=instance):
        result = await manager._recorder_states(["sensor.a", "sensor.b"], 30)

    # sensor.b yields no parseable points, so it is absent from the result map.
    assert result == {"sensor.a": [{"timestamp": t0, "value": 40.0}, {"timestamp": t0 + 180, "value": 41.5}]}


async def test_recorder_states_empty_for_no_entities(hass: HomeAssistant) -> None:
    """No recorder-eligible entities means no recorder query at all."""
    api = _mock_api()
    manager = ActivityManager(hass, api, [_timeline_numeric_config()], _mock_entry())
    assert await manager._recorder_states([], 30) == {}


async def test_timeline_series_entity_subscribed_and_refreshes(hass: HomeAssistant) -> None:
    """A timeline series entity is a companion: subscribed, and a change re-samples
    the sparkline for its own series label."""
    api = _mock_api()
    config = _entity_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.air",
            CONF_SLUG: "ha-air",
            CONF_TEMPLATE: "timeline",
            CONF_START_STATES: ["on"],
            CONF_END_STATES: ["off"],
            CONF_UPDATE_INTERVAL: 0,
            CONF_SERIES_ENTITIES: [{CONF_LABEL: "Bedroom", CONF_ENTITY_ID: "sensor.bedroom_pm25"}],
        }
    )
    manager = ActivityManager(hass, api, [config], _mock_entry())

    hass.states.async_set("sensor.bedroom_pm25", "12")
    hass.states.async_set("binary_sensor.air", "off")
    await manager.async_start()

    assert "sensor.bedroom_pm25" in _companion_entity_ids(config)

    hass.states.async_set("binary_sensor.air", "on")
    await hass.async_block_till_done()
    ongoing_before = len(activity_updates(api, "ongoing"))
    assert activity_updates(api, "ongoing")[-1]["value"] == {"Bedroom": 12.0}

    await bump_state(manager, hass, "binary_sensor.air", "sensor.bedroom_pm25", "20", {})

    ongoing_after = activity_updates(api, "ongoing")
    assert len(ongoing_after) == ongoing_before + 1
    assert ongoing_after[-1]["value"] == {"Bedroom": 20.0}
    assert_valid_activity_content(ongoing_after[-1])

    await manager.async_stop()
