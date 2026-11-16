"""Content options: state text, remaining progress, accent from progress, icon map, subtitle columns, time units."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.pushward.activity_manager import ActivityManager, _companion_entity_ids
from custom_components.pushward.config_flow import (
    _details_schema,
    _entity_details_to_config,
    _entity_reconfigure_defaults,
    _parse_entity_input,
)
from custom_components.pushward.const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_FROM_PROGRESS,
    CONF_CALENDAR_LEAD_MINUTES,
    CONF_END_STATES,
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_ICON_MAP,
    CONF_ICON_MAP_ENTITY,
    CONF_LABEL,
    CONF_LOG_COLUMNS,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_PROGRESS_ENTITY,
    CONF_PROGRESS_MODE,
    CONF_REMAINING_TIME_ATTR,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SLUG,
    CONF_START_STATES,
    CONF_STATE_LABELS,
    CONF_STATE_TEXT_ATTRIBUTE,
    CONF_STATE_TEXT_ENTITY,
    CONF_STATE_TEXT_FORMAT,
    CONF_STATE_TEXT_PATTERN,
    CONF_SUBTITLE_COLUMNS,
    CONF_SUBTITLE_ENTITY,
    CONF_TEMPLATE,
    CONF_UNIT,
    CONF_UPDATE_INTERVAL,
    CONF_VALUE_ATTRIBUTE,
    LOG_COLUMN_VALUE_MAX,
    LOG_MAX_COLUMNS,
    MAX_ICON_LEN,
    MAX_TEXT_LEN,
    STATE_TEXT_PATTERN_MAX,
)
from custom_components.pushward.content_mapper import (
    _coerce_remaining_seconds,
    map_completion_content,
    map_content,
    progress_accent,
    resolve_icon,
)

from .conftest import activity_updates, make_activity_api, make_entity_config, make_mock_entry, make_mock_state
from .server_contract import assert_valid_activity_content
from .test_config_flow import (
    _add_entity_subentry,
    _all_field_defaults,
    _mock_details_input,
    _mock_entry,
    _reconfigure_to_details,
)

END_DELAY_TARGET = "custom_components.pushward.activity_manager.END_DELAY_SECONDS"

PHONE = "device_tracker.phone"
DIST = "sensor.trip_distance"
DIST_ATTR = "distance (meters)"
ETA = "sensor.trip_travel_time"
DIR = "sensor.trip_direction"
ICONS = {"Towards": "mdi:home-import-outline", "AwayFrom": "mdi:map-marker-distance"}


def _trip(**overrides) -> dict:
    """Someone on the way home: a generic activity with every content option set."""
    return make_entity_config(
        **{
            CONF_ENTITY_ID: PHONE,
            CONF_SLUG: "ha-trip-home",
            CONF_START_STATES: ["*"],
            CONF_END_STATES: ["home"],
            CONF_UPDATE_INTERVAL: 0,
            CONF_ICON: "mdi:map-marker",
            CONF_STATE_TEXT_ENTITY: DIST,
            CONF_STATE_TEXT_ATTRIBUTE: DIST_ATTR,
            CONF_STATE_TEXT_FORMAT: "distance_m",
            CONF_STATE_TEXT_PATTERN: "{value} from home",
            CONF_PROGRESS_ENTITY: DIST,
            CONF_PROGRESS_ATTRIBUTE: DIST_ATTR,
            CONF_PROGRESS_MODE: "remaining",
            CONF_ACCENT_FROM_PROGRESS: True,
            CONF_ICON_MAP: ICONS,
            CONF_ICON_MAP_ENTITY: DIR,
            CONF_REMAINING_TIME_ENTITY: ETA,
            CONF_SUBTITLE_COLUMNS: [{CONF_LABEL: "ETA", CONF_ENTITY_ID: ETA, CONF_UNIT: "min"}, {CONF_ENTITY_ID: DIR}],
            **overrides,
        }
    )


def _set_trip(hass: HomeAssistant, metres: float, minutes: str = "15", direction: str = "Towards") -> None:
    hass.states.async_set(DIST, str(round(metres / 1609.344, 2)), {DIST_ATTR: metres, "unit_of_measurement": "mi"})
    hass.states.async_set(ETA, minutes, {"unit_of_measurement": "min"})
    hass.states.async_set(DIR, direction)


def _attr_config(**overrides) -> dict:
    """State text read from an attribute of the tracked entity."""
    return make_entity_config(**{CONF_ENTITY_ID: PHONE, CONF_STATE_TEXT_ATTRIBUTE: "v", **overrides})


# --- duration units on remaining_time ---


@pytest.mark.parametrize(
    ("raw", "attrs", "seconds"),
    [
        ("15", {"unit_of_measurement": "min"}, 900),
        ("2", {"unit_of_measurement": "h"}, 7200),
        ("1.5", {"unit_of_measurement": "h", "device_class": "duration"}, 5400),
        ("45", {"unit_of_measurement": "s"}, 45),
        ("30", {}, 30),
        ("30", {"unit_of_measurement": "km"}, 30),
    ],
)
def test_remaining_time_converts_any_time_unit(raw: str, attrs: dict, seconds: int) -> None:
    assert _coerce_remaining_seconds(raw, make_mock_state(raw, attrs, ETA), 0) == (seconds, None)


async def test_travel_time_in_minutes_without_device_class(hass: HomeAssistant) -> None:
    hass.states.async_set(ETA, "15", {"unit_of_measurement": "min"})
    state = make_mock_state("not_home", {}, PHONE)
    content = map_content(state, make_entity_config(**{CONF_REMAINING_TIME_ENTITY: ETA}), hass=hass)
    assert content["remaining_time"] == 900


async def test_remaining_time_attribute_ignores_the_state_unit(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.job", "2", {"unit_of_measurement": "h", "seconds_left": 120})
    washer = make_mock_state("on", {"left": 30, "unit_of_measurement": "min"}, "sensor.washer")

    def remaining(**cfg) -> int:
        return map_content(washer, make_entity_config(**cfg), hass=hass)["remaining_time"]

    job = {CONF_REMAINING_TIME_ENTITY: "sensor.job", CONF_REMAINING_TIME_ATTR: "seconds_left"}
    assert remaining(**job) == 120
    assert remaining(**{CONF_REMAINING_TIME_ATTR: "left"}) == 30
    # Under the duration device class an attribute converts, as it did before.
    duration = {"unit_of_measurement": "h", "device_class": "duration", "seconds_left": 2}
    hass.states.async_set("sensor.job", "2", duration)
    assert remaining(**job) == 7200


# --- state text ---


@pytest.mark.parametrize(
    ("fmt", "raw", "text"),
    [
        ("distance_m", 850.4, "850 m"),
        ("distance_m", 999.6, "1.0 km"),
        ("distance_m", 3240, "3.2 km"),
        ("distance_m", "1500", "1.5 km"),
        ("duration_s", 45, "45 s"),
        ("duration_s", 900, "15 min"),
        ("duration_s", 3900, "1 h 5 min"),
        ("duration_s", 7200, "2 h"),
        ("duration_s", -5, "0 s"),
        ("duration_min", 15, "15 min"),
        ("duration_min", 65, "1 h 5 min"),
        ("duration_min", 0.5, "30 s"),
        ("auto", 1234.5678, "1234.5678"),
        ("auto", "Towards", "Towards"),
        ("distance_m", "far", "far"),
        ("duration_s", True, "True"),
    ],
)
def test_state_text_formats(fmt: str, raw: object, text: str) -> None:
    state = make_mock_state("not_home", {"v": raw}, PHONE)
    assert map_content(state, _attr_config(**{CONF_STATE_TEXT_FORMAT: fmt}))["state"] == text


async def test_state_text_auto_reads_an_entity_state_the_way_ha_shows_it(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    temp = registry.async_get_or_create("sensor", "test", "temp", suggested_object_id="temp")
    registry.async_update_entity_options(temp.entity_id, "sensor", {"display_precision": 1})
    hass.states.async_set(temp.entity_id, "21.456", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.battery", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.raw", "3.31", {"unit_of_measurement": "km"})
    hass.states.async_set(DIR, "Towards", {"unit_of_measurement": "x"})
    state = make_mock_state("not_home", {}, PHONE)

    def text(entity_id: str) -> str:
        return map_content(state, make_entity_config(**{CONF_STATE_TEXT_ENTITY: entity_id}), hass=hass)["state"]

    assert text(temp.entity_id) == "21.5 °C"
    assert text("sensor.battery") == "50%"
    assert text("sensor.raw") == "3.31 km"
    assert text(DIR) == "Towards"


async def test_state_text_reads_a_companion_attribute_with_a_pattern(hass: HomeAssistant) -> None:
    _set_trip(hass, 850)
    content = map_content(make_mock_state("not_home", {}, PHONE), _trip(), hass=hass)
    assert content["state"] == "850 m from home"


def test_state_text_pattern_is_a_plain_replace_and_the_text_is_capped() -> None:
    state = make_mock_state("not_home", {"v": 850}, PHONE)
    cfg = _attr_config(**{CONF_STATE_TEXT_FORMAT: "distance_m"})
    assert map_content(state, {**cfg, CONF_STATE_TEXT_PATTERN: "{value} {other} {{value}}"})["state"] == (
        "850 m {other} {850 m}"
    )
    assert map_content(state, {**cfg, CONF_STATE_TEXT_PATTERN: ""})["state"] == "850 m"
    long = make_mock_state("not_home", {"v": "x" * 300}, PHONE)
    assert map_content(long, {**cfg, CONF_STATE_TEXT_PATTERN: "{value}!"})["state"] == "x" * MAX_TEXT_LEN


async def test_state_text_falls_back_to_the_state_label(hass: HomeAssistant) -> None:
    labels = {CONF_STATE_LABELS: {"not_home": "Away"}}
    state = make_mock_state("not_home", {"v": ""}, PHONE)
    hass.states.async_set(DIST, "unavailable", {DIST_ATTR: 850})

    assert map_content(state, _trip(**labels), hass=hass)["state"] == "Away"
    assert map_content(state, _trip(**{**labels, CONF_STATE_TEXT_ENTITY: "sensor.missing"}), hass=hass)["state"] == (
        "Away"
    )
    assert map_content(state, _trip(**labels))["state"] == "Away"  # a companion needs hass
    assert map_content(state, _attr_config(**labels))["state"] == "Away"  # empty attribute
    assert map_content(state, _attr_config(**{CONF_STATE_TEXT_ATTRIBUTE: "gone", **labels}))["state"] == "Away"
    # No source: state_labels as before.
    assert map_content(state, make_entity_config(**labels))["state"] == "Away"


async def test_calendar_title_stays_unless_a_state_text_source_resolves(hass: HomeAssistant, freezer) -> None:
    tz = "Europe/Warsaw"
    await hass.config.async_set_time_zone(tz)
    freezer.move_to(datetime(2026, 11, 16, 9, 45, tzinfo=dt_util.get_time_zone(tz)))
    attrs = {"message": "Dentist", "all_day": False, "start_time": "2026-11-16 10:00:00"}
    attrs["end_time"] = "2026-11-16 11:00:00"
    cfg = make_entity_config(
        **{
            CONF_ENTITY_ID: "calendar.family",
            CONF_TEMPLATE: "countdown",
            CONF_CALENDAR_LEAD_MINUTES: 30,
            CONF_STATE_TEXT_ATTRIBUTE: "location",
            CONF_STATE_TEXT_PATTERN: "at {value}",
        }
    )

    assert map_content(make_mock_state("off", attrs, "calendar.family"), cfg)["state"] == "Dentist"
    with_place = make_mock_state("off", {**attrs, "location": "Main St"}, "calendar.family")
    assert map_content(with_place, cfg)["state"] == "at Main St"


def test_completion_keeps_its_message() -> None:
    completion = map_completion_content(_trip(), {"state": "850 m from home", "progress": 0.4})
    assert completion["state"] == "Complete"


# --- remaining progress ---


def _remaining(metres: object, **overrides) -> tuple[object, dict]:
    state = make_mock_state("not_home", {"distance": metres}, PHONE)
    cfg = make_entity_config(**{CONF_PROGRESS_ATTRIBUTE: "distance", CONF_PROGRESS_MODE: "remaining", **overrides})
    return state, cfg


@pytest.mark.parametrize(
    ("metres", "peak", "progress"),
    [
        (1000, None, 0.0),
        (1000, 2000.0, 0.5),
        (1000, 500.0, 0.0),
        (0, 2000.0, 1.0),
        (0, None, 0.0),
        (0, 0.0, 0.0),
        (1, 3.0, 0.67),
        ("far", 2000.0, 0.0),
    ],
)
def test_remaining_progress_fills_from_the_farthest_point(metres: object, peak, progress: float) -> None:
    state, cfg = _remaining(metres)
    assert map_content(state, cfg, progress_max=peak)["progress"] == progress


def test_direct_progress_is_unchanged() -> None:
    state, cfg = _remaining(50, **{CONF_PROGRESS_MODE: "direct"})
    assert map_content(state, cfg, progress_max=2000.0)["progress"] == 0.5
    legacy = {k: v for k, v in cfg.items() if k != CONF_PROGRESS_MODE}
    assert map_content(state, legacy) == map_content(state, cfg, progress_max=2000.0)


# --- accent from progress ---


@pytest.mark.parametrize(
    ("progress", "color"),
    [(0, "#FF0000"), (0.25, "#FF8000"), (0.5, "#FFFF00"), (0.75, "#80FF00"), (1, "#00FF00"), (-1, "#FF0000")],
)
def test_progress_accent_gradient(progress: float, color: str) -> None:
    assert progress_accent(progress) == color


def test_accent_from_progress_overrides_the_static_accent() -> None:
    state = make_mock_state("on", {"pct": 25, "level": 75}, "sensor.tank")
    cfg = make_entity_config(
        **{CONF_PROGRESS_ATTRIBUTE: "pct", CONF_ACCENT_COLOR: "red", CONF_ACCENT_FROM_PROGRESS: True}
    )
    assert map_content(state, cfg)["accent_color"] == "#FF8000"
    assert map_content(state, {**cfg, CONF_ACCENT_FROM_PROGRESS: False})["accent_color"] == "red"
    gauge = {**cfg, CONF_TEMPLATE: "gauge", CONF_VALUE_ATTRIBUTE: "level", CONF_MIN_VALUE: 0.0, CONF_MAX_VALUE: 100.0}
    assert map_content(state, gauge)["accent_color"] == "#80FF00"


def test_completion_stays_green() -> None:
    last = {"progress": 0.3, "accent_color": "#FF9900", "subtitle": "ETA: 15min"}
    assert map_completion_content(_trip(), last)["accent_color"] == "green"


# --- icon by state ---


async def test_icon_map_wins_over_every_other_icon_source(hass: HomeAssistant) -> None:
    hass.states.async_set(DIR, "AwayFrom")
    tracked = make_mock_state("Towards", {"dyn": "mdi:x", "icon": "mdi:y"}, PHONE)
    cfg = make_entity_config(**{CONF_ICON: "mdi:map-marker", CONF_ICON_ATTRIBUTE: "dyn", CONF_ICON_MAP: ICONS})

    assert resolve_icon(tracked, cfg) == "mdi:home-import-outline"
    assert map_content(tracked, {**cfg, CONF_ICON_MAP_ENTITY: DIR}, hass=hass)["icon"] == "mdi:map-marker-distance"
    assert map_content(tracked, {**cfg, CONF_ICON_MAP_ENTITY: PHONE}, hass=hass)["icon"] == "mdi:home-import-outline"


async def test_icon_map_without_a_match_falls_through(hass: HomeAssistant) -> None:
    hass.states.async_set(DIR, "inStatZone")
    tracked = make_mock_state("not_home", {}, PHONE)
    cfg = make_entity_config(**{CONF_ICON: "mdi:map-marker", CONF_ICON_MAP: ICONS})

    assert resolve_icon(tracked, cfg) == "mdi:map-marker"
    assert resolve_icon(tracked, {**cfg, CONF_ICON_MAP_ENTITY: DIR}, hass=hass) == "mdi:map-marker"
    assert resolve_icon(tracked, {**cfg, CONF_ICON_MAP_ENTITY: "sensor.missing"}, hass=hass) == "mdi:map-marker"
    hass.states.async_set(DIR, "Towards")
    assert resolve_icon(tracked, {**cfg, CONF_ICON_MAP_ENTITY: DIR}) == "mdi:map-marker"  # no hass, no companion


# --- subtitle columns ---


async def test_subtitle_columns_with_and_without_labels(hass: HomeAssistant) -> None:
    _set_trip(hass, 850)
    state = make_mock_state("not_home", {"friendly_name": "Phone", "zone": "Work"}, PHONE)
    cfg = _trip(**{CONF_SUBTITLE_ENTITY: DIR})

    assert map_content(state, cfg, hass=hass)["subtitle"] == "ETA: 15min · Towards"
    bare = {**cfg, CONF_SUBTITLE_COLUMNS: [{CONF_ENTITY_ID: DIR}, {"attribute": "zone"}]}
    assert map_content(state, bare, hass=hass)["subtitle"] == "Towards · Work"


async def test_subtitle_columns_that_resolve_nothing_leave_the_subtitle(hass: HomeAssistant) -> None:
    hass.states.async_set(ETA, "unavailable")
    hass.states.async_set(DIR, "unavailable")
    hass.states.async_set("sensor.mode", "Driving")
    state = make_mock_state("not_home", {"friendly_name": "Phone"}, PHONE)

    assert map_content(state, _trip(**{CONF_SUBTITLE_ENTITY: "sensor.mode"}), hass=hass)["subtitle"] == "Driving"
    assert map_content(state, _trip(), hass=hass)["subtitle"] == "Phone"


def test_subtitle_columns_are_capped_and_beat_the_media_artist() -> None:
    long = {f"a{i}": "x" * LOG_COLUMN_VALUE_MAX for i in range(LOG_MAX_COLUMNS)}
    state = make_mock_state("on", long, PHONE)
    cfg = make_entity_config(**{CONF_SUBTITLE_COLUMNS: [{"attribute": key} for key in long]})
    assert len(map_content(state, cfg)["subtitle"]) == MAX_TEXT_LEN

    player = make_mock_state("playing", {"media_artist": "Artist", "media_album_name": "Album"}, "media_player.tv")
    media = make_entity_config(**{CONF_ENTITY_ID: "media_player.tv", CONF_TEMPLATE: "media"})
    assert map_content(player, media)["subtitle"] == "Artist"
    with_columns = {**media, CONF_SUBTITLE_COLUMNS: [{"attribute": "media_album_name"}]}
    assert map_content(player, with_columns)["subtitle"] == "Album"


def test_log_lines_render_exactly_as_before() -> None:
    state = make_mock_state("on", {"brightness": 153, "color_temp_kelvin": 4000}, "light.lamp")
    cfg = make_entity_config(
        **{
            CONF_ENTITY_ID: "light.lamp",
            CONF_TEMPLATE: "log",
            CONF_LOG_COLUMNS: [
                {CONF_LABEL: "Bright", "attribute": "brightness"},
                {"attribute": "color_temp_kelvin", CONF_UNIT: "K"},
            ],
        }
    )
    assert map_content(state, cfg)["lines"][0]["text"] == "On · Bright: 153 · 4000K"


def test_content_companions_are_tracked() -> None:
    sources = {CONF_STATE_TEXT_ENTITY: "sensor.text", CONF_ICON_MAP_ENTITY: "sensor.icon"}
    assert _companion_entity_ids(make_entity_config(**sources)) == ["sensor.text", "sensor.icon"]
    assert _companion_entity_ids(_trip()) == [ETA, DIST, DIR]
    log = _trip(**{CONF_TEMPLATE: "log", CONF_LOG_COLUMNS: [{CONF_ENTITY_ID: "sensor.door"}]})
    assert _companion_entity_ids(log) == [ETA, DIST, DIR, "sensor.door"]


# --- the trip through the manager ---


async def test_trip_through_the_manager(hass: HomeAssistant) -> None:
    api = make_activity_api()
    _set_trip(hass, 1200, direction="AwayFrom")
    hass.states.async_set(PHONE, "home")
    manager = ActivityManager(hass, api, [_trip()], make_mock_entry())
    with patch(END_DELAY_TARGET, 0):
        await manager.async_start()
        hass.states.async_set(PHONE, "not_home")
        await hass.async_block_till_done()

        first = activity_updates(api, "ongoing")[-1]
        assert first["state"] == "1.2 km from home"
        assert (first["progress"], first["accent_color"]) == (0.0, "#FF0000")
        assert first["icon"] == "mdi:map-marker-distance"
        assert first["remaining_time"] == 900
        assert first["subtitle"] == "ETA: 15min · AwayFrom"
        assert_valid_activity_content(first)

        # Still driving away: the farthest point grows, the bar stays empty.
        _set_trip(hass, 1500, direction="AwayFrom")
        await hass.async_block_till_done()
        assert activity_updates(api, "ongoing")[-1]["progress"] == 0.0

        _set_trip(hass, 750, minutes="8")
        await hass.async_block_till_done()
        halfway = activity_updates(api, "ongoing")[-1]
        assert (halfway["state"], halfway["progress"], halfway["accent_color"]) == ("750 m from home", 0.5, "#FFFF00")
        assert (halfway["icon"], halfway["remaining_time"]) == ("mdi:home-import-outline", 480)
        assert manager._tracked[PHONE].progress_max == 1500
        assert_valid_activity_content(halfway)

        hass.states.async_set(PHONE, "home")
        await hass.async_block_till_done()
        await manager._tracked[PHONE].end_task
        ended = activity_updates(api, "ended")[-1]
        assert (ended["accent_color"], ended["state"]) == ("green", "Complete")

        # A new trip measures from its own start, not the last trip's farthest point.
        _set_trip(hass, 300)
        hass.states.async_set(PHONE, "not_home")
        await hass.async_block_till_done()
        assert manager._tracked[PHONE].progress_max == 300
        assert activity_updates(api, "ongoing")[-1]["progress"] == 0.0
        await manager.async_stop()


async def test_content_sources_refresh_the_card_and_fall_back_when_lost(hass: HomeAssistant) -> None:
    api = make_activity_api()
    hass.states.async_set("sensor.text", "Driving")
    hass.states.async_set(DIR, "AwayFrom")
    hass.states.async_set(ETA, "15", {"unit_of_measurement": "min"})
    hass.states.async_set(PHONE, "home")
    cfg = make_entity_config(
        **{
            CONF_ENTITY_ID: PHONE,
            CONF_START_STATES: ["*"],
            CONF_END_STATES: ["home"],
            CONF_UPDATE_INTERVAL: 0,
            CONF_ICON: "mdi:map-marker",
            CONF_STATE_LABELS: {"not_home": "Away"},
            CONF_STATE_TEXT_ENTITY: "sensor.text",
            CONF_ICON_MAP: ICONS,
            CONF_ICON_MAP_ENTITY: DIR,
            CONF_SUBTITLE_COLUMNS: [{CONF_LABEL: "ETA", CONF_ENTITY_ID: ETA, CONF_UNIT: "min"}, {"attribute": "zone"}],
        }
    )
    manager = ActivityManager(hass, api, [cfg], make_mock_entry())
    await manager.async_start()

    async def card(entity_id: str, value: str) -> dict:
        hass.states.async_set(entity_id, value)
        await hass.async_block_till_done()
        latest = activity_updates(api, "ongoing")[-1]
        return {key: latest[key] for key in ("state", "icon", "subtitle")}

    first = await card(PHONE, "not_home")
    assert first == {"state": "Driving", "icon": "mdi:map-marker-distance", "subtitle": "ETA: 15min"}
    # Each source alone refreshes the card, and losing it falls back.
    assert (await card(DIR, "Towards"))["icon"] == "mdi:home-import-outline"
    assert (await card("sensor.text", "unavailable"))["state"] == "Away"
    assert (await card(DIR, "unavailable"))["icon"] == "mdi:map-marker"
    hass.states.async_set(PHONE, "not_home", {"zone": "Work"})
    assert (await card(ETA, "unknown"))["subtitle"] == "Work"
    await manager.async_stop()


# --- config flow ---

_STEP1 = {CONF_ENTITY_ID: PHONE, CONF_TEMPLATE: "generic"}


def test_parse_stores_the_content_options() -> None:
    cfg = _parse_entity_input(
        {
            **_STEP1,
            CONF_STATE_TEXT_ENTITY: DIST,
            CONF_STATE_TEXT_ATTRIBUTE: DIST_ATTR,
            CONF_STATE_TEXT_FORMAT: "distance_m",
            CONF_STATE_TEXT_PATTERN: "  {value} from home ",
            CONF_PROGRESS_MODE: "remaining",
            CONF_ACCENT_FROM_PROGRESS: True,
            CONF_ICON_MAP: [{"state": "Towards", CONF_ICON: "mdi:home"}, {"state": "Unknown"}],
            CONF_ICON_MAP_ENTITY: DIR,
            CONF_SUBTITLE_COLUMNS: [
                {CONF_LABEL: " ETA ", CONF_ENTITY_ID: ETA, CONF_UNIT: "min"},
                {CONF_ENTITY_ID: DIR},
            ],
        }
    )
    assert cfg[CONF_STATE_TEXT_PATTERN] == "{value} from home"
    assert (cfg[CONF_STATE_TEXT_FORMAT], cfg[CONF_PROGRESS_MODE], cfg[CONF_ACCENT_FROM_PROGRESS]) == (
        "distance_m",
        "remaining",
        True,
    )
    assert cfg[CONF_ICON_MAP] == {"Towards": "mdi:home"}
    assert cfg[CONF_SUBTITLE_COLUMNS] == [
        {CONF_LABEL: "ETA", CONF_ENTITY_ID: ETA, CONF_UNIT: "min"},
        {CONF_ENTITY_ID: DIR},
    ]

    plain = _parse_entity_input(dict(_STEP1))
    assert (plain[CONF_STATE_TEXT_FORMAT], plain[CONF_PROGRESS_MODE], plain[CONF_ACCENT_FROM_PROGRESS]) == (
        "auto",
        "direct",
        False,
    )
    assert (plain[CONF_ICON_MAP], plain[CONF_SUBTITLE_COLUMNS], plain[CONF_STATE_TEXT_PATTERN]) == ({}, [], "")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (CONF_ICON_MAP, [{"state": "on", CONF_ICON: "x" * (MAX_ICON_LEN + 1)}], "invalid_icon_map"),
        (CONF_STATE_TEXT_PATTERN, "x" * (STATE_TEXT_PATTERN_MAX + 1), "invalid_state_text_pattern"),
        (CONF_SUBTITLE_COLUMNS, [{CONF_LABEL: "ETA"}], "invalid_log_column"),
        (CONF_SUBTITLE_COLUMNS, [{"attribute": f"a{i}"} for i in range(LOG_MAX_COLUMNS + 1)], "too_many_log_columns"),
    ],
)
def test_parse_errors_land_on_the_field(field: str, value: object, code: str) -> None:
    cfg, errors = _entity_details_to_config(_STEP1, {field: value}, None)
    assert cfg is None
    assert errors == {field: code}


def test_progress_options_follow_the_templates_that_show_progress() -> None:
    def fields(template: str) -> set[str]:
        return set(_all_field_defaults(_details_schema(PHONE, template, {})))

    for template in ("generic", "steps"):
        assert {CONF_PROGRESS_MODE, CONF_ACCENT_FROM_PROGRESS} <= fields(template)
    assert CONF_ACCENT_FROM_PROGRESS in fields("gauge")
    assert CONF_PROGRESS_MODE not in fields("gauge")
    for template in ("countdown", "alert", "timeline", "board", "log", "media"):
        assert not {CONF_PROGRESS_MODE, CONF_ACCENT_FROM_PROGRESS} & fields(template), template


_FORM = {
    CONF_STATE_TEXT_ENTITY: DIST,
    CONF_STATE_TEXT_ATTRIBUTE: DIST_ATTR,
    CONF_STATE_TEXT_FORMAT: "distance_m",
    CONF_STATE_TEXT_PATTERN: "{value} from home",
    CONF_PROGRESS_ENTITY: DIST,
    CONF_PROGRESS_ATTRIBUTE: DIST_ATTR,
    CONF_PROGRESS_MODE: "remaining",
    CONF_ACCENT_FROM_PROGRESS: True,
    CONF_ICON_MAP: [{"state": state, CONF_ICON: icon} for state, icon in ICONS.items()],
    CONF_ICON_MAP_ENTITY: DIR,
    CONF_SUBTITLE_COLUMNS: [{CONF_LABEL: "ETA", CONF_ENTITY_ID: ETA, CONF_UNIT: "min"}, {CONF_ENTITY_ID: DIR}],
}


async def test_flow_saves_and_reconfigures_the_content_options(hass: HomeAssistant) -> None:
    entry = _mock_entry()
    entry.add_to_hass(hass)
    result = await _add_entity_subentry(hass, entry, details_overrides=_FORM)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry_id, stored = next((sid, sub.data) for sid, sub in entry.subentries.items())
    assert stored[CONF_ICON_MAP] == ICONS
    assert stored[CONF_SUBTITLE_COLUMNS] == _FORM[CONF_SUBTITLE_COLUMNS]
    assert {key: stored[key] for key in _FORM if key not in (CONF_ICON_MAP,)} == {
        key: value for key, value in _FORM.items() if key != CONF_ICON_MAP
    }

    defaults = _entity_reconfigure_defaults(stored)
    assert defaults[CONF_ICON_MAP] == _FORM[CONF_ICON_MAP]

    result = await _reconfigure_to_details(hass, entry, subentry_id, "generic")
    form_defaults = _all_field_defaults(result["data_schema"])
    assert form_defaults[CONF_ICON_MAP] == _FORM[CONF_ICON_MAP]
    assert form_defaults[CONF_SUBTITLE_COLUMNS] == _FORM[CONF_SUBTITLE_COLUMNS]
    assert (form_defaults[CONF_STATE_TEXT_FORMAT], form_defaults[CONF_PROGRESS_MODE]) == ("distance_m", "remaining")
    assert form_defaults[CONF_ACCENT_FROM_PROGRESS] is True

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input=_mock_details_input("generic", **_FORM)
    )
    assert result["type"] is FlowResultType.ABORT
    assert entry.subentries[subentry_id].data == stored


def test_reconfigure_opens_a_subentry_saved_before_the_options() -> None:
    old = {
        key: value
        for key, value in make_entity_config().items()
        if key not in (CONF_ICON_MAP, CONF_SUBTITLE_COLUMNS, CONF_STATE_TEXT_FORMAT, CONF_PROGRESS_MODE)
    }
    defaults = _all_field_defaults(_details_schema(PHONE, "generic", _entity_reconfigure_defaults(old)))
    assert (defaults[CONF_ICON_MAP], defaults[CONF_SUBTITLE_COLUMNS]) == ([], [])
    assert (defaults[CONF_STATE_TEXT_FORMAT], defaults[CONF_PROGRESS_MODE]) == ("auto", "direct")
