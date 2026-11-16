"""Lifecycle options: the '*' start state, run-while conditions and calendar windows."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.pushward.activity_manager import (
    ActivityManager,
    _companion_entity_ids,
    _is_start,
    run_while_rows,
)
from custom_components.pushward.const import (
    CONF_CALENDAR_LEAD_MINUTES,
    CONF_CALENDAR_SUBTITLE_BEFORE,
    CONF_CALENDAR_SUBTITLE_DURING,
    CONF_DISMISSAL_TTL,
    CONF_END_STATES,
    CONF_ENTITY_ID,
    CONF_LABEL,
    CONF_REMOVE_ON_END,
    CONF_RUN_WHILE,
    CONF_RUN_WHILE_STATES,
    CONF_SLUG,
    CONF_START_STATES,
    CONF_SUBTITLE_ENTITY,
    CONF_TEMPLATE,
    CONF_TILES,
    CONF_UPDATE_INTERVAL,
    MAX_TEXT_LEN,
    RUN_WHILE_MAX,
)
from custom_components.pushward.content_mapper import (
    _coerce_epoch,
    _coerce_remaining_seconds,
    calendar_next_boundary,
    calendar_phase,
    get_domain_defaults,
    map_content,
    parse_ha_datetime,
)
from custom_components.pushward.quota import quota_released_signal

from .conftest import (
    activity_updates,
    end_activity_via_state,
    make_activity_api,
    make_entity_config,
    make_mock_entry,
    make_mock_state,
    make_quota_error,
)
from .server_contract import assert_valid_activity_content
from .test_activity_manager import SLEEP_TARGET, _gated_sleep, _pump

END_DELAY_TARGET = "custom_components.pushward.activity_manager.END_DELAY_SECONDS"

PHONE = "device_tracker.phone"
ME = "device_tracker.me"


def _trip_config(**overrides) -> dict:
    """A trip: up while the phone is away, only while I am home."""
    return make_entity_config(
        **{
            CONF_ENTITY_ID: PHONE,
            CONF_SLUG: "ha-trip",
            CONF_START_STATES: ["*"],
            CONF_END_STATES: ["home"],
            CONF_UPDATE_INTERVAL: 0,
            CONF_RUN_WHILE: [{CONF_ENTITY_ID: ME, CONF_RUN_WHILE_STATES: ["home"]}],
            **overrides,
        }
    )


async def _start(hass: HomeAssistant, api: AsyncMock, config: dict, entry=None) -> ActivityManager:
    manager = ActivityManager(hass, api, [config], entry or make_mock_entry())
    await manager.async_start()
    await hass.async_block_till_done()
    return manager


async def _set(hass: HomeAssistant, entity_id: str, state: str, attrs: dict | None = None) -> None:
    hass.states.async_set(entity_id, state, attrs)
    await hass.async_block_till_done()


async def _end_window(manager: ActivityManager, hass: HomeAssistant, entity_id: str) -> None:
    """Let a scheduled two-phase end run to its ENDED push."""
    end_task = manager._tracked[entity_id].end_task
    if end_task is not None:
        await end_task
    await hass.async_block_till_done()


# --- '*' start state ---


def test_is_start_wildcard_matches_any_non_end_state() -> None:
    cfg = {CONF_START_STATES: ["*"], CONF_END_STATES: ["home"]}
    assert _is_start("not_home", cfg)
    assert _is_start("Work", cfg)
    assert not _is_start("home", cfg)
    assert not _is_start("unavailable", cfg)
    assert not _is_start("unknown", cfg)


def test_is_start_never_starts_on_unavailable_even_when_listed() -> None:
    assert not _is_start("unavailable", {CONF_START_STATES: ["unavailable", "on"], CONF_END_STATES: []})
    assert not _is_start("unknown", {CONF_START_STATES: ["unknown"], CONF_END_STATES: []})
    assert _is_start("on", {CONF_START_STATES: ["unavailable", "on"], CONF_END_STATES: []})


def test_is_start_empty_list_never_starts() -> None:
    assert not _is_start("on", {CONF_START_STATES: [], CONF_END_STATES: []})
    assert not _is_start("on", {CONF_START_STATES: None, CONF_END_STATES: None})


def test_device_tracker_person_and_calendar_defaults() -> None:
    for domain in ("device_tracker", "person"):
        defaults = get_domain_defaults(domain)
        assert defaults["start_states"] == ["*"]
        assert defaults["end_states"] == ["home"]
        assert defaults["icon"].startswith("mdi:")
    calendar = get_domain_defaults("calendar")
    assert calendar == {"icon": "mdi:calendar", "start_states": [], "end_states": []}


async def test_wildcard_starts_on_leaving_and_ends_on_coming_home(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, PHONE, "home")
    manager = await _start(hass, api, _trip_config(**{CONF_RUN_WHILE: []}))
    api.create_activity.assert_not_awaited()

    await _set(hass, PHONE, "not_home")
    api.create_activity.assert_awaited_once()

    # A zone change while away is an update, not a second start.
    await _set(hass, PHONE, "Work")
    api.create_activity.assert_awaited_once()
    assert activity_updates(api, "ongoing")[-1]["state"] == "Work"

    await end_activity_via_state(manager, hass, PHONE, "home", {})
    assert activity_updates(api, "ended")
    assert not manager._tracked[PHONE].is_active
    await manager.async_stop()


async def test_wildcard_ignores_unavailable_and_unknown(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, PHONE, "unknown")
    manager = await _start(hass, api, _trip_config(**{CONF_RUN_WHILE: []}))
    await _set(hass, PHONE, "unavailable")
    api.create_activity.assert_not_awaited()
    await manager.async_stop()


async def test_empty_start_states_never_start(hass: HomeAssistant) -> None:
    api = make_activity_api()
    config = make_entity_config(**{CONF_ENTITY_ID: "sensor.power", CONF_START_STATES: [], CONF_END_STATES: []})
    await _set(hass, "sensor.power", "12")
    manager = await _start(hass, api, config)
    await _set(hass, "sensor.power", "40")
    api.create_activity.assert_not_awaited()
    await manager.async_stop()


# --- run_while ---


def test_run_while_rows_tolerates_hand_written_configs() -> None:
    rows = run_while_rows(
        [
            {CONF_ENTITY_ID: ME, CONF_RUN_WHILE_STATES: "home, work"},
            {CONF_ENTITY_ID: "input_boolean.trip", CONF_RUN_WHILE_STATES: ["on", " "]},
            {CONF_ENTITY_ID: "sensor.no_states", CONF_RUN_WHILE_STATES: []},
            {CONF_RUN_WHILE_STATES: ["home"]},
            "not a row",
        ]
    )
    assert rows == [
        {CONF_ENTITY_ID: ME, CONF_RUN_WHILE_STATES: ["home", "work"]},
        {CONF_ENTITY_ID: "input_boolean.trip", CONF_RUN_WHILE_STATES: ["on"]},
    ]
    assert run_while_rows(None) == []
    many = [{CONF_ENTITY_ID: f"sensor.s{i}", CONF_RUN_WHILE_STATES: ["on"]} for i in range(RUN_WHILE_MAX + 2)]
    assert len(run_while_rows(many)) == RUN_WHILE_MAX


def test_conditions_are_not_companions() -> None:
    """A condition gates the lifecycle; companions only refresh values."""
    assert ME not in _companion_entity_ids(_trip_config())


async def test_run_while_starts_only_once_the_condition_holds(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, ME, "not_home")
    await _set(hass, PHONE, "home")
    manager = await _start(hass, api, _trip_config())

    await _set(hass, PHONE, "not_home")
    api.create_activity.assert_not_awaited()

    await _set(hass, ME, "home")
    api.create_activity.assert_awaited_once()
    assert manager._tracked[PHONE].is_active
    await manager.async_stop()


async def test_run_while_needs_the_tracked_entity_in_a_start_state(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, ME, "not_home")
    await _set(hass, PHONE, "home")
    manager = await _start(hass, api, _trip_config())

    await _set(hass, ME, "home")
    api.create_activity.assert_not_awaited()
    await manager.async_stop()


async def test_run_while_ends_when_a_condition_stops_and_restarts_when_it_holds(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, ME, "home")
    await _set(hass, PHONE, "not_home")
    manager = await _start(hass, api, _trip_config())
    tracked = manager._tracked[PHONE]
    assert tracked.is_active

    with patch(END_DELAY_TARGET, 0):
        await _set(hass, ME, "not_home")
        await _end_window(manager, hass, PHONE)
    assert len(activity_updates(api, "ended")) == 1
    assert not tracked.is_active

    await _set(hass, ME, "home")
    assert api.create_activity.await_count == 2
    assert tracked.is_active
    await manager.async_stop()


async def test_run_while_unchanged_outcome_sends_nothing(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, ME, "home")
    await _set(hass, PHONE, "not_home")
    manager = await _start(hass, api, _trip_config())
    api.reset_mock()

    await _set(hass, ME, "home", {"gps_accuracy": 12})
    api.create_activity.assert_not_awaited()
    api.update_activity.assert_not_awaited()
    await manager.async_stop()


async def test_run_while_flap_during_the_end_window_keeps_the_activity(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, ME, "home")
    await _set(hass, PHONE, "not_home")
    manager = await _start(hass, api, _trip_config())
    tracked = manager._tracked[PHONE]
    api.reset_mock()

    gate = asyncio.Event()
    with patch(SLEEP_TARGET, _gated_sleep(gate)):
        hass.states.async_set(ME, "not_home")
        await _pump()
        end_task = tracked.end_task
        assert end_task is not None and not end_task.done()

        hass.states.async_set(ME, "home")
        await _pump()
        gate.set()
        await _pump()

    assert end_task.done()
    assert not activity_updates(api, "ended")
    api.create_activity.assert_awaited_once()
    assert tracked.is_active
    await manager.async_stop()


async def test_run_while_condition_lost_while_starting_ends_the_activity(hass: HomeAssistant) -> None:
    api = make_activity_api()
    gate = asyncio.Event()

    async def _on_the_wire(*_args, **_kwargs) -> None:
        await gate.wait()

    api.create_activity.side_effect = _on_the_wire
    await _set(hass, ME, "home")
    await _set(hass, PHONE, "home")
    manager = await _start(hass, api, _trip_config())
    tracked = manager._tracked[PHONE]

    with patch(END_DELAY_TARGET, 0):
        hass.states.async_set(PHONE, "not_home")
        await _pump()
        api.create_activity.assert_awaited_once()
        # The condition fails before the create returns, while nothing is active yet.
        hass.states.async_set(ME, "not_home")
        await _pump()
        gate.set()
        await hass.async_block_till_done()
        await _end_window(manager, hass, PHONE)

    assert len(activity_updates(api, "ended")) == 1
    assert not tracked.is_active
    await manager.async_stop()


async def test_end_state_reached_while_starting_ends_the_activity(hass: HomeAssistant) -> None:
    """The state handler sees nothing active mid-start, so the end state has to be caught after it."""
    api = make_activity_api()
    gate = asyncio.Event()

    async def _on_the_wire(*_args, **_kwargs) -> None:
        await gate.wait()

    api.create_activity.side_effect = _on_the_wire
    await _set(hass, PHONE, "home")
    manager = await _start(hass, api, _trip_config(**{CONF_RUN_WHILE: []}))
    tracked = manager._tracked[PHONE]

    with patch(END_DELAY_TARGET, 0):
        hass.states.async_set(PHONE, "not_home")
        await _pump()
        api.create_activity.assert_awaited_once()
        hass.states.async_set(PHONE, "home")
        await _pump()
        gate.set()
        await hass.async_block_till_done()
        await _end_window(manager, hass, PHONE)

    assert len(activity_updates(api, "ended")) == 1
    assert not tracked.is_active
    await manager.async_stop()


async def test_run_while_gates_the_deferred_board_start(hass: HomeAssistant) -> None:
    api = make_activity_api()
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.home",
            CONF_SLUG: "ha-board",
            CONF_TEMPLATE: "board",
            CONF_START_STATES: ["on"],
            CONF_END_STATES: ["off"],
            CONF_UPDATE_INTERVAL: 0,
            CONF_TILES: [{CONF_LABEL: "CPU", CONF_ENTITY_ID: "sensor.cpu"}],
            CONF_RUN_WHILE: [{CONF_ENTITY_ID: ME, CONF_RUN_WHILE_STATES: ["home"]}],
        }
    )
    await _set(hass, ME, "not_home")
    await _set(hass, "sensor.cpu", "unavailable")
    await _set(hass, "binary_sensor.home", "on")
    manager = await _start(hass, api, config)

    # A tile turning up retries a deferred board start; the condition still fails.
    await _set(hass, "sensor.cpu", "42")
    api.create_activity.assert_not_awaited()

    await _set(hass, ME, "home")
    api.create_activity.assert_awaited_once()
    await manager.async_stop()


async def test_run_while_unavailable_condition_never_matches(hass: HomeAssistant) -> None:
    api = make_activity_api()
    config = _trip_config(**{CONF_RUN_WHILE: [{CONF_ENTITY_ID: ME, CONF_RUN_WHILE_STATES: ["home", "unavailable"]}]})
    await _set(hass, ME, "unavailable")
    await _set(hass, PHONE, "not_home")
    manager = await _start(hass, api, config)
    await _set(hass, PHONE, "Work")
    api.create_activity.assert_not_awaited()
    await manager.async_stop()


async def test_run_while_missing_condition_entity_never_matches(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, PHONE, "not_home")
    manager = await _start(hass, api, _trip_config())
    api.create_activity.assert_not_awaited()
    await manager.async_stop()


async def test_run_while_checked_at_startup(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, ME, "home")
    await _set(hass, PHONE, "not_home")
    manager = await _start(hass, api, _trip_config())
    api.create_activity.assert_awaited_once()
    await manager.async_stop()


async def test_run_while_checked_on_quota_release(hass: HomeAssistant) -> None:
    api = make_activity_api()
    api.create_activity = AsyncMock(side_effect=make_quota_error())
    entry = make_mock_entry("quota_entry")
    await _set(hass, ME, "home")
    await _set(hass, PHONE, "not_home")
    manager = await _start(hass, api, _trip_config(), entry)
    tracked = manager._tracked[PHONE]
    assert not tracked.is_active

    # The condition stopped holding while the quota was out: the release starts nothing.
    await _set(hass, ME, "not_home")
    api.create_activity = AsyncMock()
    async_dispatcher_send(hass, quota_released_signal(entry.entry_id), "live_activity_updates")
    await hass.async_block_till_done()
    api.create_activity.assert_not_awaited()

    # Back home, but that start is refused too; the release then catches up.
    api.create_activity = AsyncMock(side_effect=make_quota_error())
    await _set(hass, ME, "home")
    assert not tracked.is_active
    api.create_activity = AsyncMock()
    async_dispatcher_send(hass, quota_released_signal(entry.entry_id), "live_activity_updates")
    await hass.async_block_till_done()
    api.create_activity.assert_awaited_once()
    assert tracked.is_active
    await manager.async_stop()


async def test_stop_drops_the_condition_listener(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, ME, "not_home")
    await _set(hass, PHONE, "not_home")
    manager = await _start(hass, api, _trip_config())
    tracked = manager._tracked[PHONE]
    unsub = tracked.condition_unsub = MagicMock(side_effect=tracked.condition_unsub)
    await manager.async_stop()
    unsub.assert_called_once()

    await _set(hass, ME, "home")
    api.create_activity.assert_not_awaited()


async def test_reload_resubscribes_the_new_conditions(hass: HomeAssistant) -> None:
    api = make_activity_api()
    await _set(hass, ME, "not_home")
    await _set(hass, "input_boolean.trip", "off")
    await _set(hass, PHONE, "not_home")
    manager = await _start(hass, api, _trip_config())

    other = [{CONF_ENTITY_ID: "input_boolean.trip", CONF_RUN_WHILE_STATES: ["on"]}]
    await manager.async_reload([_trip_config(**{CONF_RUN_WHILE: other})])
    await _set(hass, ME, "home")
    api.create_activity.assert_not_awaited()

    await _set(hass, "input_boolean.trip", "on")
    api.create_activity.assert_awaited_once()
    await manager.async_stop()


# --- naive datetimes are HA local time ---


async def test_naive_datetimes_read_as_ha_local_time(hass: HomeAssistant) -> None:
    # An offset no process runs in, so a process-time-zone read cannot pass by luck.
    await hass.config.async_set_time_zone("Asia/Kathmandu")
    expected = datetime(2026, 11, 16, 4, 15, tzinfo=UTC)

    assert parse_ha_datetime("2026-11-16 10:00:00") == expected
    assert parse_ha_datetime(datetime(2026, 11, 16, 10, 0)) == expected
    assert parse_ha_datetime("2026-11-16T04:15:00+00:00") == expected
    assert parse_ha_datetime(expected.timestamp()) == expected
    assert parse_ha_datetime("") is None
    assert parse_ha_datetime(True) is None
    assert parse_ha_datetime(float("nan")) is None

    assert _coerce_epoch("2026-11-16 10:00:00") == int(expected.timestamp())
    assert _coerce_epoch("inf") is None
    source = make_mock_state("2026-11-16 10:00:00", {"device_class": "timestamp"}, "sensor.finish")
    now = int(expected.timestamp()) - 600
    assert _coerce_remaining_seconds("2026-11-16 10:00:00", source, now) == (600, int(expected.timestamp()))


# --- calendar windows ---

TZ = "Europe/Warsaw"
CAL = "calendar.family"


def _local(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 16, hour, minute, tzinfo=dt_util.get_time_zone(TZ))


def _ts(hour: int, minute: int = 0) -> int:
    return int(_local(hour, minute).timestamp())


def _event(start: str = "10:00", end: str = "11:00", message: str = "Dentist", all_day: bool = False) -> dict:
    """HA's calendar attributes: naive local times for the current or next event."""
    return {
        "message": message,
        "all_day": all_day,
        "start_time": f"2026-11-16 {start}:00",
        "end_time": f"2026-11-16 {end}:00",
        "friendly_name": "Family",
    }


def _cal_config(**overrides) -> dict:
    return make_entity_config(
        **{
            CONF_ENTITY_ID: CAL,
            CONF_SLUG: "ha-calendar",
            CONF_TEMPLATE: "countdown",
            CONF_START_STATES: [],
            CONF_END_STATES: [],
            CONF_UPDATE_INTERVAL: 0,
            CONF_CALENDAR_LEAD_MINUTES: 30,
            **overrides,
        }
    )


@pytest.fixture
async def calendar(hass: HomeAssistant, freezer):
    """Start a calendar activity manager at 09:00 Warsaw time; END_DELAY collapsed to 0."""
    freezer.move_to(_local(9))
    await hass.config.async_set_time_zone(TZ)
    api = make_activity_api()
    managers: list[ActivityManager] = []

    async def _start_calendar(attrs: dict | None, state: str = "off", **overrides) -> ActivityManager:
        hass.states.async_set(CAL, state, attrs)
        manager = ActivityManager(hass, api, [_cal_config(**overrides)], make_mock_entry())
        managers.append(manager)
        await manager.async_start()
        await hass.async_block_till_done()
        return manager

    with patch(END_DELAY_TARGET, 0):
        yield _start_calendar, api
        for manager in managers:
            await manager.async_stop()


async def _at(hass: HomeAssistant, freezer, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


async def test_calendar_waits_for_the_window(calendar) -> None:
    start, api = calendar
    manager = await start(_event())
    api.create_activity.assert_not_awaited()
    assert manager._tracked[CAL].calendar_unsub is not None


async def test_calendar_opens_lead_minutes_before_counting_to_the_start(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    await start(_event())

    await _at(hass, freezer, _local(9, 29))
    api.create_activity.assert_not_awaited()

    await _at(hass, freezer, _local(9, 30))
    api.create_activity.assert_awaited_once()
    content = activity_updates(api, "ongoing")[-1]
    assert content["state"] == "Dentist"
    assert (content["start_date"], content["end_date"]) == (_ts(9, 30), _ts(10))
    assert_valid_activity_content(content)


async def test_calendar_counts_to_the_end_once_the_event_starts(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    await start(_event())
    await _at(hass, freezer, _local(9, 30))

    await _at(hass, freezer, _local(10))
    api.create_activity.assert_awaited_once()
    content = activity_updates(api, "ongoing")[-1]
    assert (content["start_date"], content["end_date"]) == (_ts(10), _ts(11))
    assert_valid_activity_content(content)


async def test_calendar_ends_with_the_event(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    manager = await start(_event())
    await _at(hass, freezer, _local(9, 30))
    await _at(hass, freezer, _local(10))

    await _at(hass, freezer, _local(11))
    await _end_window(manager, hass, CAL)
    assert len(activity_updates(api, "ended")) == 1
    tracked = manager._tracked[CAL]
    assert not tracked.is_active
    assert tracked.calendar_unsub is None


async def test_calendar_rearms_when_the_event_changes(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    await start(_event("12:00", "13:00", "Lunch"))

    # The entity now shows an earlier event than the one the 11:30 timer was armed for,
    # so only a re-armed timer opens its window at 09:30.
    await _set(hass, CAL, "off", _event("10:00", "11:00", "Dentist"))
    await _at(hass, freezer, _local(9, 29))
    api.create_activity.assert_not_awaited()

    await _at(hass, freezer, _local(9, 30))
    api.create_activity.assert_awaited_once()
    content = activity_updates(api, "ongoing")[-1]
    assert content["state"] == "Dentist"
    assert (content["start_date"], content["end_date"]) == (_ts(9, 30), _ts(10))


async def test_calendar_switches_the_target_at_the_start_despite_the_throttle(
    hass: HomeAssistant, calendar, freezer
) -> None:
    """The server ends a countdown whose end_date has passed, so the switch cannot wait."""
    start, api = calendar
    await start(_event(), **{CONF_UPDATE_INTERVAL: 60})
    await _at(hass, freezer, _local(9, 30))
    # A frame 20s before the start puts the boundary inside the update interval.
    freezer.move_to(_local(9, 59) + timedelta(seconds=40))
    await _set(hass, CAL, "off", _event(message="Dentist, room 4"))
    assert activity_updates(api, "ongoing")[-1]["state"] == "Dentist, room 4"

    await _at(hass, freezer, _local(10))
    assert activity_updates(api, "ongoing")[-1]["end_date"] == _ts(11)


async def test_calendar_without_lead_starts_once_when_ha_flips_on_too(hass: HomeAssistant, calendar, freezer) -> None:
    """HA's own alarm writes 'on' at the event start, the same instant as the boundary timer."""
    start, api = calendar

    gate = asyncio.Event()

    async def _on_the_wire(*_args, **_kwargs) -> None:
        await gate.wait()

    api.create_activity.side_effect = _on_the_wire
    await start(_event(), **{CONF_CALENDAR_LEAD_MINUTES: 0})

    freezer.move_to(_local(10))
    async_fire_time_changed(hass, _local(10))
    hass.states.async_set(CAL, "on", _event())
    await _pump()
    gate.set()
    await hass.async_block_till_done()
    api.create_activity.assert_awaited_once()


async def test_calendar_moves_on_to_the_next_event(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    manager = await start(_event())
    await _at(hass, freezer, _local(9, 30))
    await _at(hass, freezer, _local(11))
    await _end_window(manager, hass, CAL)

    await _set(hass, CAL, "off", _event("14:00", "15:00", "Swim"))
    assert api.create_activity.await_count == 1
    await _at(hass, freezer, _local(13, 30))
    assert api.create_activity.await_count == 2
    assert activity_updates(api, "ongoing")[-1]["state"] == "Swim"


async def test_calendar_skips_all_day_events(calendar) -> None:
    start, api = calendar
    attrs = {**_event(all_day=True), "start_time": "2026-11-16", "end_time": "2026-11-17"}
    manager = await start(attrs, state="on")
    api.create_activity.assert_not_awaited()
    assert manager._tracked[CAL].calendar_unsub is None


async def test_calendar_without_start_time_starts_nothing(calendar) -> None:
    start, api = calendar
    manager = await start({"friendly_name": "Family"})
    api.create_activity.assert_not_awaited()
    assert manager._tracked[CAL].calendar_unsub is None


async def test_calendar_event_already_over_starts_nothing(calendar) -> None:
    start, api = calendar
    manager = await start(_event("07:00", "08:00"))
    api.create_activity.assert_not_awaited()
    assert manager._tracked[CAL].calendar_unsub is None


async def test_calendar_stop_cancels_the_timer(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    manager = await start(_event())
    tracked = manager._tracked[CAL]
    unsub = tracked.calendar_unsub = MagicMock(side_effect=tracked.calendar_unsub)
    await manager.async_stop()
    unsub.assert_called_once()

    await _at(hass, freezer, _local(9, 30))
    api.create_activity.assert_not_awaited()


async def test_calendar_starts_on_startup_inside_the_window(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    freezer.move_to(_local(10, 15))
    await start(_event(), state="on")
    api.create_activity.assert_awaited_once()
    content = activity_updates(api, "ongoing")[-1]
    assert (content["start_date"], content["end_date"]) == (_ts(10), _ts(11))


async def test_calendar_ignores_start_and_end_states(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    await start(_event("12:00", "13:00"), state="on", **{CONF_START_STATES: ["on"], CONF_END_STATES: ["off"]})
    api.create_activity.assert_not_awaited()

    await _set(hass, CAL, "off", _event("12:00", "13:00"))
    await _at(hass, freezer, _local(11, 30))
    api.create_activity.assert_awaited_once()


async def test_calendar_saved_before_the_lead_time_keeps_its_states(hass: HomeAssistant, calendar) -> None:
    """No lead-time key means a subentry from before calendar windows: start/end states still rule."""
    start, api = calendar
    config = {CONF_START_STATES: ["on"], CONF_END_STATES: ["off"]}
    manager = await start(_event("12:00", "13:00"), state="on", **config)
    api.create_activity.assert_not_awaited()
    await manager.async_stop()

    legacy = _cal_config(**config)
    del legacy[CONF_CALENDAR_LEAD_MINUTES]
    manager = ActivityManager(hass, api, [legacy], make_mock_entry())
    await manager.async_start()
    api.create_activity.assert_awaited_once()
    assert manager._tracked[CAL].calendar_unsub is None
    await manager.async_stop()


async def test_calendar_window_still_needs_the_run_while_conditions(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    await _set(hass, ME, "not_home")
    await start(_event(), **{CONF_RUN_WHILE: [{CONF_ENTITY_ID: ME, CONF_RUN_WHILE_STATES: ["home"]}]})

    await _at(hass, freezer, _local(9, 30))
    api.create_activity.assert_not_awaited()

    await _set(hass, ME, "home")
    api.create_activity.assert_awaited_once()


async def test_calendar_content_follows_the_phase(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(_local(9))
    await hass.config.async_set_time_zone(TZ)
    config = _cal_config()
    state = make_mock_state("off", _event(message="  Dentist  "), CAL)

    before = map_content(state, config)
    assert before["state"] == "Off"
    assert calendar_phase(state, config, _local(9).timestamp()) is None
    assert calendar_next_boundary(state, config, _local(9).timestamp()) == _ts(9, 30)

    freezer.move_to(_local(9, 45))
    content = map_content(state, config)
    assert content["state"] == "Dentist"
    assert (content["start_date"], content["end_date"]) == (_ts(9, 30), _ts(10))
    assert calendar_next_boundary(state, config, _local(9, 45).timestamp()) == _ts(10)

    # The held countdown window gives way to the event's own dates at the phase switch.
    freezer.move_to(_local(10, 5))
    during = map_content(state, config, last_content=content)
    assert (during["start_date"], during["end_date"]) == (_ts(10), _ts(11))

    long_title = make_mock_state("on", _event(message="x" * (MAX_TEXT_LEN + 20)), CAL)
    assert map_content(long_title, config)["state"] == "x" * MAX_TEXT_LEN

    # A non-countdown template still shows the event title, without the clock fields.
    generic = map_content(state, {**config, CONF_TEMPLATE: "generic"})
    assert generic["state"] == "Dentist"
    assert "end_date" not in generic


def test_calendar_zero_length_event_never_opens() -> None:
    config = _cal_config(**{CONF_CALENDAR_LEAD_MINUTES: 0})
    state = make_mock_state("off", {**_event(), "end_time": None}, CAL)
    start = parse_ha_datetime(state.attributes["start_time"]).timestamp()
    assert calendar_phase(state, config, start) is None
    assert calendar_next_boundary(state, config, start - 1) == int(start)


_PHASE_SUBTITLES = {CONF_CALENDAR_SUBTITLE_BEFORE: "Starts soon", CONF_CALENDAR_SUBTITLE_DURING: "In progress"}


async def test_calendar_subtitle_follows_the_phase(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    await start(_event(), **_PHASE_SUBTITLES)

    await _at(hass, freezer, _local(9, 30))
    assert activity_updates(api, "ongoing")[-1]["subtitle"] == "Starts soon"

    await _at(hass, freezer, _local(10))
    content = activity_updates(api, "ongoing")[-1]
    assert content["subtitle"] == "In progress"
    assert_valid_activity_content(content)


async def test_calendar_subtitle_empty_keeps_the_calendar_name(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    await start(_event(), **{CONF_CALENDAR_SUBTITLE_DURING: "In progress"})

    await _at(hass, freezer, _local(9, 30))
    assert activity_updates(api, "ongoing")[-1]["subtitle"] == "Family"


async def test_calendar_subtitle_source_wins_over_the_phase_text(hass: HomeAssistant, calendar, freezer) -> None:
    start, api = calendar
    hass.states.async_set("sensor.room", "Room 4")
    await start(_event(), **_PHASE_SUBTITLES, **{CONF_SUBTITLE_ENTITY: "sensor.room"})

    await _at(hass, freezer, _local(10))
    assert activity_updates(api, "ongoing")[-1]["subtitle"] == "Room 4"


WASHER = "binary_sensor.washer"


def _washer_config(**overrides) -> dict:
    return make_entity_config(
        **{
            CONF_ENTITY_ID: WASHER,
            CONF_SLUG: "ha-washer",
            CONF_START_STATES: ["on"],
            CONF_END_STATES: ["off"],
            CONF_UPDATE_INTERVAL: 0,
            **overrides,
        }
    )


async def _running_washer(hass: HomeAssistant, api: AsyncMock, **overrides) -> ActivityManager:
    hass.states.async_set(WASHER, "on")
    manager = ActivityManager(hass, api, [_washer_config(**overrides)], make_mock_entry())
    await manager.async_start()
    await hass.async_block_till_done()
    assert manager._tracked[WASHER].is_active
    return manager


async def test_remove_on_end_sends_one_ended_push_that_dismisses_at_once(hass: HomeAssistant) -> None:
    api = make_activity_api()
    manager = await _running_washer(hass, api, **{CONF_REMOVE_ON_END: True, CONF_DISMISSAL_TTL: 120})
    api.reset_mock()

    with patch(SLEEP_TARGET, new_callable=AsyncMock) as sleep:
        await manager._async_end_activity(WASHER)

    sleep.assert_not_awaited()
    api.update_activity.assert_awaited_once()
    call = api.update_activity.call_args
    assert call.args[1] == "ended"
    assert call.kwargs["dismissal_ttl"] == 0
    assert not manager._tracked[WASHER].is_active
    await manager.async_stop()


async def test_remove_on_end_creates_with_zero_dismissal(hass: HomeAssistant) -> None:
    api = make_activity_api()
    manager = await _running_washer(hass, api, **{CONF_REMOVE_ON_END: True, CONF_DISMISSAL_TTL: 120})
    assert api.create_activity.call_args.kwargs["dismissal_ttl"] == 0
    await manager.async_stop()


async def test_remove_on_end_shutdown_end_dismisses_at_once(hass: HomeAssistant) -> None:
    api = make_activity_api()
    manager = await _running_washer(hass, api, **{CONF_REMOVE_ON_END: True})
    api.reset_mock()

    await manager.async_stop()
    call = api.update_activity.call_args
    assert call.args[1] == "ended"
    assert call.kwargs["dismissal_ttl"] == 0


async def test_without_remove_on_end_the_configured_dismissal_stays(hass: HomeAssistant) -> None:
    api = make_activity_api()
    manager = await _running_washer(hass, api, **{CONF_DISMISSAL_TTL: 120})
    assert api.create_activity.call_args.kwargs["dismissal_ttl"] == 120
    api.reset_mock()

    await manager.async_stop()
    assert api.update_activity.call_args.kwargs["dismissal_ttl"] is None
