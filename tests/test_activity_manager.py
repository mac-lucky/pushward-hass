"""Tests for the PushWard activity manager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant, State

from custom_components.pushward.activity_manager import ActivityManager
from custom_components.pushward.const import (
    CONF_ACCENT_COLOR,
    CONF_ACTIVITY_NAME,
    CONF_END_STATES,
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_PRIORITY,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SLUG,
    CONF_START_STATES,
    CONF_TEMPLATE,
    CONF_UPDATE_INTERVAL,
)


def _entity_config(**overrides) -> dict:
    """Build a test entity configuration."""
    config = {
        CONF_ENTITY_ID: "binary_sensor.washer",
        CONF_SLUG: "ha-washer",
        CONF_ACTIVITY_NAME: "Washer",
        CONF_ICON: "washer",
        CONF_PRIORITY: 1,
        CONF_TEMPLATE: "generic",
        CONF_START_STATES: ["on"],
        CONF_END_STATES: ["off"],
        CONF_UPDATE_INTERVAL: 5,
        CONF_PROGRESS_ATTRIBUTE: "",
        CONF_REMAINING_TIME_ATTR: "",
        CONF_ACCENT_COLOR: "",
    }
    config.update(overrides)
    return config


def _mock_state(entity_id: str, state: str, attributes: dict | None = None) -> State:
    """Create a mock HA State."""
    mock = MagicMock(spec=State)
    mock.entity_id = entity_id
    mock.state = state
    mock.attributes = attributes or {"friendly_name": entity_id}
    return mock


def _mock_api() -> AsyncMock:
    """Create a mock PushWard API client."""
    api = AsyncMock()
    api.create_activity = AsyncMock()
    api.update_activity = AsyncMock()
    api.delete_activity = AsyncMock()
    return api


async def test_start_activity_on_state_change(hass: HomeAssistant) -> None:
    """Entity going from off→on triggers activity creation and ONGOING update."""
    api = _mock_api()
    config = _entity_config()
    manager = ActivityManager(hass, api, [config])

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
    manager = ActivityManager(hass, api, [config])

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
    manager = ActivityManager(hass, api, [config])

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
    manager = ActivityManager(hass, api, [config])

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
    manager = ActivityManager(hass, api, [config])

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
    manager = ActivityManager(hass, api, [config])

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
    manager = ActivityManager(hass, api, [config])

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
    manager = ActivityManager(hass, api, [config1])

    hass.states.async_set("binary_sensor.washer", "off")
    hass.states.async_set("switch.light", "off")
    await manager.async_start()

    assert "binary_sensor.washer" in manager._tracked
    assert "switch.light" not in manager._tracked

    await manager.async_reload([config2])

    assert "binary_sensor.washer" not in manager._tracked
    assert "switch.light" in manager._tracked

    await manager.async_stop()
