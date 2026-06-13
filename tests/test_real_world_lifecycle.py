"""End-to-end Live Activity lifecycle tests driven by realistic HA state machines.

Each test wires a real-world entity (Roborock vacuum, Sonos media player, a garage
cover, a smart lock) into ``ActivityManager`` and drives it through the same
state transitions a real device produces — start, mid-cycle updates and the
two-phase end all flow through the real ``async_track_state_change_event`` path,
not by poking manager internals. Every ONGOING / ENDED payload is also checked
against the public PushWard API contract via :mod:`tests.server_contract`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant

from custom_components.pushward.activity_manager import ActivityManager
from custom_components.pushward.const import (
    ACTIVITY_STATE_ENDED,
    ACTIVITY_STATE_ONGOING,
    CONF_ACTIVITY_NAME,
    CONF_END_STATES,
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_SLUG,
    CONF_START_STATES,
    CONF_SUBTITLE_ATTRIBUTE,
)

from .conftest import (
    activity_updates,
    bump_state,
    end_activity_via_state,
    make_activity_api,
    make_entity_config,
    make_mock_entry,
)
from .server_contract import assert_valid_activity_content

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ongoing(api: AsyncMock) -> list[dict]:
    return activity_updates(api, ACTIVITY_STATE_ONGOING)


def _ended(api: AsyncMock) -> list[dict]:
    return activity_updates(api, ACTIVITY_STATE_ENDED)


def _assert_all_ongoing_valid(api: AsyncMock) -> None:
    contents = _ongoing(api)
    assert contents, "expected at least one ONGOING update"
    for content in contents:
        assert_valid_activity_content(content)


def _assert_ended_valid_completion(api: AsyncMock) -> None:
    """Assert the ENDED payload is server-valid AND carries the completion styling."""
    contents = _ended(api)
    assert contents, "expected at least one ENDED update"
    for content in contents:
        assert_valid_activity_content(content)
        # The two-phase end ships completion content: a green checkmark finish.
        assert content["accent_color"] == "green"
        assert content["icon"] == "checkmark.circle.fill"


async def _send_now(manager: ActivityManager, hass: HomeAssistant, entity_id: str, new_state: str, attrs: dict) -> None:
    """Set a new (non-start/non-end) state and send it through the real throttle path."""
    await bump_state(manager, hass, entity_id, entity_id, new_state, attrs)


# ---------------------------------------------------------------------------
# Roborock vacuum — generic activity over the cleaning state machine
# ---------------------------------------------------------------------------


async def test_roborock_vacuum_full_clean_cycle(hass: HomeAssistant) -> None:
    """vacuum: docked → cleaning (start) → returning (update) → docked (end)."""
    api = make_activity_api()
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "vacuum.roborock_s7",
            CONF_SLUG: "ha-roborock-s7",
            CONF_ACTIVITY_NAME: "Roborock S7",
            CONF_ICON: "",  # let the icon-resolution chain pick mdi:robot-vacuum
            CONF_START_STATES: ["cleaning"],
            CONF_END_STATES: ["docked", "idle"],
        }
    )
    manager = ActivityManager(hass, api, [config], make_mock_entry())

    # The Roborock integration exposes fan_speed on the vacuum entity, but battery
    # is a separate sensor (sensor.roborock_s7_battery), so it is NOT an attribute.
    hass.states.async_set(
        "vacuum.roborock_s7",
        "docked",
        {"friendly_name": "Roborock S7", "fan_speed": "balanced"},
    )
    await manager.async_start()
    api.create_activity.assert_not_called()

    # Start cleaning
    hass.states.async_set(
        "vacuum.roborock_s7",
        "cleaning",
        {"friendly_name": "Roborock S7", "fan_speed": "turbo"},
    )
    await hass.async_block_till_done()

    api.create_activity.assert_awaited_once()
    assert api.create_activity.call_args.args[0] == "ha-roborock-s7"
    start_content = _ongoing(api)[0]
    assert start_content["template"] == "generic"
    assert start_content["icon"] == "mdi:robot-vacuum"  # domain default fallback
    assert start_content["subtitle"] == "Roborock S7"

    # Mid-cycle: returning to dock is still "active" — through the real send path.
    await _send_now(
        manager,
        hass,
        "vacuum.roborock_s7",
        "returning",
        {"friendly_name": "Roborock S7", "fan_speed": "turbo"},
    )
    assert _ongoing(api)[-1]["state"] == "Returning"

    # Dock → two-phase end via the real state-change path
    await end_activity_via_state(manager, hass, "vacuum.roborock_s7", "docked", {"friendly_name": "Roborock S7"})

    _assert_all_ongoing_valid(api)
    _assert_ended_valid_completion(api)
    assert not manager._tracked["vacuum.roborock_s7"].is_active

    await manager.async_stop()


async def test_roborock_resume_when_already_cleaning(hass: HomeAssistant) -> None:
    """A vacuum already cleaning at HA restart resumes the activity on start."""
    api = make_activity_api()
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "vacuum.roborock_s7",
            CONF_SLUG: "ha-roborock-s7",
            CONF_ICON: "",
            CONF_START_STATES: ["cleaning"],
            CONF_END_STATES: ["docked", "idle"],
        }
    )
    manager = ActivityManager(hass, api, [config], make_mock_entry())

    hass.states.async_set("vacuum.roborock_s7", "cleaning", {"friendly_name": "Roborock S7"})
    await manager.async_start()
    await hass.async_block_till_done()

    api.create_activity.assert_awaited_once()
    _assert_all_ongoing_valid(api)

    await manager.async_stop()


# ---------------------------------------------------------------------------
# Sonos media player — track shown as subtitle
# ---------------------------------------------------------------------------


async def test_sonos_playback_session(hass: HomeAssistant) -> None:
    """media_player: idle → playing (start) → new track (update) → paused (end).

    A Sonos speaker only ever reports playing/paused/idle — never 'off'.
    """
    api = make_activity_api()
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "media_player.living_room",
            CONF_SLUG: "ha-living-room-sonos",
            CONF_ACTIVITY_NAME: "Living Room",
            CONF_ICON: "",
            CONF_START_STATES: ["playing"],
            CONF_END_STATES: ["paused", "idle"],
            CONF_SUBTITLE_ATTRIBUTE: "media_title",
        }
    )
    manager = ActivityManager(hass, api, [config], make_mock_entry())

    hass.states.async_set("media_player.living_room", "idle", {"friendly_name": "Living Room"})
    await manager.async_start()
    api.create_activity.assert_not_called()

    hass.states.async_set(
        "media_player.living_room",
        "playing",
        {
            "friendly_name": "Living Room",
            "media_title": "Black Hole Sun",
            "media_artist": "Soundgarden",
            "media_album_name": "Superunknown",
            "media_duration": 318,
            "media_position": 0,
            "volume_level": 0.35,
        },
    )
    await hass.async_block_till_done()
    api.create_activity.assert_awaited_once()
    assert _ongoing(api)[0]["subtitle"] == "Black Hole Sun"

    # Track changes — subtitle should follow, through the real send path
    await _send_now(
        manager,
        hass,
        "media_player.living_room",
        "playing",
        {
            "friendly_name": "Living Room",
            "media_title": "Spoonman",
            "media_artist": "Soundgarden",
            "media_duration": 246,
            "media_position": 0,
        },
    )
    assert _ongoing(api)[-1]["subtitle"] == "Spoonman"

    # Pause → end
    await end_activity_via_state(manager, hass, "media_player.living_room", "paused", {"friendly_name": "Living Room"})

    _assert_all_ongoing_valid(api)
    _assert_ended_valid_completion(api)
    await manager.async_stop()


# ---------------------------------------------------------------------------
# Garage door cover — progress driven by current_position
# ---------------------------------------------------------------------------


async def test_garage_door_opening_progress(hass: HomeAssistant) -> None:
    """cover (garage): closed → opening (progress from current_position) → open (end)."""
    api = make_activity_api()
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "cover.garage_door",
            CONF_SLUG: "ha-garage-door",
            CONF_ACTIVITY_NAME: "Garage Door",
            CONF_ICON: "",
            CONF_START_STATES: ["opening", "closing"],
            CONF_END_STATES: ["open", "closed"],
            CONF_PROGRESS_ATTRIBUTE: "current_position",
        }
    )
    manager = ActivityManager(hass, api, [config], make_mock_entry())

    hass.states.async_set(
        "cover.garage_door",
        "closed",
        {"friendly_name": "Garage Door", "device_class": "garage", "current_position": 0},
    )
    await manager.async_start()

    hass.states.async_set(
        "cover.garage_door",
        "opening",
        {"friendly_name": "Garage Door", "device_class": "garage", "current_position": 25},
    )
    await hass.async_block_till_done()
    api.create_activity.assert_awaited_once()
    first = _ongoing(api)[0]
    assert first["icon"] == "mdi:garage-open"  # cover/garage device-class icon
    assert first["progress"] == 0.25

    # Position climbs — through the real send path
    await _send_now(
        manager,
        hass,
        "cover.garage_door",
        "opening",
        {"friendly_name": "Garage Door", "device_class": "garage", "current_position": 80},
    )
    assert _ongoing(api)[-1]["progress"] == 0.8

    await end_activity_via_state(
        manager,
        hass,
        "cover.garage_door",
        "open",
        {"friendly_name": "Garage Door", "device_class": "garage", "current_position": 100},
    )

    _assert_all_ongoing_valid(api)
    _assert_ended_valid_completion(api)
    await manager.async_stop()


# ---------------------------------------------------------------------------
# Smart lock — generic, device-class icon, quick open/close
# ---------------------------------------------------------------------------


async def test_smart_lock_unlock_then_lock(hass: HomeAssistant) -> None:
    """lock: locked → unlocked (start) → locked (end)."""
    api = make_activity_api()
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "lock.front_door",
            CONF_SLUG: "ha-front-door-lock",
            CONF_ACTIVITY_NAME: "Front Door",
            CONF_ICON: "",
            CONF_START_STATES: ["unlocked"],
            CONF_END_STATES: ["locked", "locking"],
        }
    )
    manager = ActivityManager(hass, api, [config], make_mock_entry())

    hass.states.async_set("lock.front_door", "locked", {"friendly_name": "Front Door"})
    await manager.async_start()

    hass.states.async_set("lock.front_door", "unlocked", {"friendly_name": "Front Door"})
    await hass.async_block_till_done()
    api.create_activity.assert_awaited_once()

    await end_activity_via_state(manager, hass, "lock.front_door", "locked", {"friendly_name": "Front Door"})

    _assert_all_ongoing_valid(api)
    _assert_ended_valid_completion(api)
    assert not manager._tracked["lock.front_door"].is_active
    await manager.async_stop()


# ---------------------------------------------------------------------------
# Robustness: unavailable does not spuriously start, and recovers
# ---------------------------------------------------------------------------


async def test_unavailable_does_not_start_then_recovers(hass: HomeAssistant) -> None:
    """A flapping device going unavailable must not start; a later real state does."""
    api = make_activity_api()
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "vacuum.roborock_s7",
            CONF_SLUG: "ha-roborock-s7",
            CONF_ICON: "",
            CONF_START_STATES: ["cleaning"],
            CONF_END_STATES: ["docked", "idle"],
        }
    )
    manager = ActivityManager(hass, api, [config], make_mock_entry())

    hass.states.async_set("vacuum.roborock_s7", "docked", {"friendly_name": "Roborock S7"})
    await manager.async_start()

    hass.states.async_set("vacuum.roborock_s7", "unavailable")
    await hass.async_block_till_done()
    api.create_activity.assert_not_called()

    hass.states.async_set("vacuum.roborock_s7", "cleaning", {"friendly_name": "Roborock S7"})
    await hass.async_block_till_done()
    api.create_activity.assert_awaited_once()
    _assert_all_ongoing_valid(api)

    await manager.async_stop()
