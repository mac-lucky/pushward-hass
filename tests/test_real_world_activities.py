"""Live Activity tests built from fact-checked real HA integration entities.

These exercise the headline PushWard use cases with the exact entity_ids,
attributes, device_classes and state machines real integrations expose:

* Bambu Lab X1C 3D print job — a countdown driven by ``sensor.bambu_x1c_remaining_time``
  (``device_class: duration``, minutes) with progress from ``sensor.bambu_x1c_print_progress``.
* Bosch / LG appliances — a countdown anchored to an absolute finish *timestamp*
  (``device_class: timestamp``), the format the mapper special-cases.
* Nest / ecobee thermostats — gauge and multi-series timeline from climate attributes.
* SolarEdge / Tesla Powerwall — production / state-of-charge gauges.
* Philips Hue — the 0-255 brightness rescale path.
* Home alarm panel — a security ``alert`` activity lifecycle.

Every emitted ONGOING / ENDED payload is asserted against the public PushWard
contract via :mod:`tests.server_contract`.
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.pushward.activity_manager import ActivityManager
from custom_components.pushward.const import (
    ACTIVITY_STATE_ENDED,
    ACTIVITY_STATE_ONGOING,
    CONF_ACTIVITY_NAME,
    CONF_CURRENT_STEP_ENTITY,
    CONF_END_STATES,
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_LABEL,
    CONF_LOG_LEVEL_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SERIES,
    CONF_SERIES_ENTITIES,
    CONF_SEVERITY,
    CONF_SLUG,
    CONF_START_STATES,
    CONF_STEP_LABELS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_TAP_ACTION_URL,
    CONF_TEMPLATE,
    CONF_TILES,
    CONF_TOTAL_STEPS,
    CONF_UNIT,
    CONF_VALUE_ATTRIBUTE,
)
from custom_components.pushward.content_mapper import map_content

from .conftest import (
    activity_updates,
    bump_state,
    end_activity_via_state,
    make_activity_api,
    make_entity_config,
    make_mock_entry,
    make_mock_state,
)
from .server_contract import assert_valid_activity_content


def _ongoing(api: AsyncMock) -> list[dict]:
    return activity_updates(api, ACTIVITY_STATE_ONGOING)


def _ended(api: AsyncMock) -> list[dict]:
    return activity_updates(api, ACTIVITY_STATE_ENDED)


# ===========================================================================
# Bambu Lab X1C — 3D print job countdown (the flagship Live Activity)
# ===========================================================================


def test_bambu_print_countdown_content(hass: HomeAssistant) -> None:
    """Print status drives a countdown; remaining (min, duration sensor) + progress (%)."""
    # Companion sensors: remaining time is device_class=duration in MINUTES,
    # progress is a plain 0-100 percentage.
    hass.states.async_set(
        "sensor.bambu_x1c_remaining_time",
        "47",
        {"device_class": "duration", "unit_of_measurement": "min"},
    )
    hass.states.async_set("sensor.bambu_x1c_print_progress", "62", {"unit_of_measurement": "%"})

    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "sensor.bambu_x1c_print_status",
            CONF_SLUG: "ha-bambu-x1c-print",
            CONF_ACTIVITY_NAME: "X1C Print",
            CONF_ICON: "mdi:printer-3d-nozzle",
            CONF_TEMPLATE: "countdown",
            CONF_START_STATES: ["running", "prepare"],
            CONF_END_STATES: ["finish", "failed", "idle"],
            CONF_REMAINING_TIME_ENTITY: "sensor.bambu_x1c_remaining_time",
            CONF_PROGRESS_ENTITY: "sensor.bambu_x1c_print_progress",
        }
    )
    state = make_mock_state("running", {"friendly_name": "X1C Print Status"}, "sensor.bambu_x1c_print_status")

    content = map_content(state, config, hass=hass)
    assert content["template"] == "countdown"
    assert content["remaining_time"] == 47 * 60  # 47 min → 2820 s via DurationConverter
    # end_date and start_date share a single clock read, so their delta is exact.
    assert content["end_date"] - content["start_date"] == 47 * 60
    assert content["progress"] == 0.62
    assert_valid_activity_content(content)


async def test_bambu_print_job_lifecycle(hass: HomeAssistant) -> None:
    """idle → running (start countdown) → progress climbs → finish (two-phase end)."""
    api = make_activity_api()
    hass.states.async_set(
        "sensor.bambu_x1c_remaining_time",
        "90",
        {"device_class": "duration", "unit_of_measurement": "min"},
    )
    hass.states.async_set("sensor.bambu_x1c_print_progress", "5", {"unit_of_measurement": "%"})
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "sensor.bambu_x1c_print_status",
            CONF_SLUG: "ha-bambu-x1c-print",
            CONF_ACTIVITY_NAME: "X1C Print",
            CONF_ICON: "mdi:printer-3d-nozzle",
            CONF_TEMPLATE: "countdown",
            CONF_START_STATES: ["running", "prepare"],
            CONF_END_STATES: ["finish", "failed", "idle"],
            CONF_REMAINING_TIME_ENTITY: "sensor.bambu_x1c_remaining_time",
            CONF_PROGRESS_ENTITY: "sensor.bambu_x1c_print_progress",
        }
    )
    manager = ActivityManager(hass, api, [config], make_mock_entry())

    hass.states.async_set("sensor.bambu_x1c_print_status", "idle", {"friendly_name": "X1C Print Status"})
    await manager.async_start()
    api.create_activity.assert_not_called()

    # Print starts
    hass.states.async_set("sensor.bambu_x1c_print_status", "running", {"friendly_name": "X1C Print Status"})
    await hass.async_block_till_done()
    api.create_activity.assert_awaited_once()
    start = _ongoing(api)[0]
    assert start["template"] == "countdown"
    assert start["progress"] == 0.05

    # Layer progresses — companion sensors climb, through the real subscription path
    await bump_state(
        manager,
        hass,
        "sensor.bambu_x1c_print_status",
        "sensor.bambu_x1c_remaining_time",
        "20",
        {"device_class": "duration", "unit_of_measurement": "min"},
    )
    await bump_state(
        manager,
        hass,
        "sensor.bambu_x1c_print_status",
        "sensor.bambu_x1c_print_progress",
        "73",
        {"unit_of_measurement": "%"},
    )
    assert _ongoing(api)[-1]["progress"] == 0.73

    # Print finishes
    await end_activity_via_state(
        manager, hass, "sensor.bambu_x1c_print_status", "finish", {"friendly_name": "X1C Print Status"}
    )

    for content in _ongoing(api):
        assert_valid_activity_content(content)
    for content in _ended(api):
        assert_valid_activity_content(content)
    assert _ended(api), "print finish should send an ENDED payload"

    await manager.async_stop()


# ===========================================================================
# Appliances — countdown anchored to an absolute finish timestamp
# ===========================================================================


def test_bosch_dishwasher_finish_timestamp_countdown(hass: HomeAssistant) -> None:
    """device_class=timestamp finish-time anchors end_date to the absolute finish."""
    finish = dt_util.utcnow() + timedelta(minutes=95)
    hass.states.async_set(
        "sensor.bosch_dishwasher_program_finish_time",
        finish.isoformat(),
        {"device_class": "timestamp"},
    )
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "sensor.bosch_dishwasher_operation_state",
            CONF_SLUG: "ha-bosch-dishwasher",
            CONF_ACTIVITY_NAME: "Dishwasher",
            CONF_ICON: "mdi:dishwasher",
            CONF_TEMPLATE: "countdown",
            CONF_START_STATES: ["run", "delayedstart"],
            CONF_END_STATES: ["finished", "inactive", "ready"],
            CONF_REMAINING_TIME_ENTITY: "sensor.bosch_dishwasher_program_finish_time",
        }
    )
    state = make_mock_state(
        "run", {"friendly_name": "Dishwasher Operation State"}, "sensor.bosch_dishwasher_operation_state"
    )

    content = map_content(state, config, hass=hass)
    # Anchored to the absolute finish, not derived from a relative countdown.
    assert content["end_date"] == int(finish.timestamp())
    # start_date is stamped with "now", not 0 / end_date / an arbitrary value.
    assert content["start_date"] == pytest.approx(int(time.time()), abs=5)
    assert_valid_activity_content(content)


def test_lg_washer_finish_timestamp_countdown(hass: HomeAssistant) -> None:
    """LG ThinQ exposes its remaining time as an absolute finish timestamp too."""
    finish = dt_util.utcnow() + timedelta(minutes=38)
    hass.states.async_set(
        "sensor.lg_washing_machine_remain",
        finish.isoformat(),
        {"device_class": "timestamp"},
    )
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "sensor.lg_washing_machine_current_state",
            CONF_SLUG: "ha-lg-washer",
            CONF_ACTIVITY_NAME: "Washing Machine",
            CONF_ICON: "mdi:washing-machine",
            CONF_TEMPLATE: "countdown",
            CONF_START_STATES: ["running", "detecting", "rinsing", "spinning"],
            CONF_END_STATES: ["end", "power_off", "error"],
            CONF_REMAINING_TIME_ENTITY: "sensor.lg_washing_machine_remain",
        }
    )
    state = make_mock_state("running", {"friendly_name": "Washer State"}, "sensor.lg_washing_machine_current_state")

    content = map_content(state, config, hass=hass)
    assert content["end_date"] == int(finish.timestamp())
    assert content["start_date"] == pytest.approx(int(time.time()), abs=5)
    assert_valid_activity_content(content)


# ===========================================================================
# Appliance phases — the steps template (current_step from a companion sensor)
# ===========================================================================


def test_dishwasher_steps_phase_progression(hass: HomeAssistant) -> None:
    """A multi-phase dishwasher renders the steps template with labelled phases.

    The phase index is bound to a companion sensor (a helper/template sensor the
    user maintains); PushWard reads it as ``current_step`` and auto-derives
    progress from current/total.
    """
    hass.states.async_set("sensor.dishwasher_phase_index", "2", {"friendly_name": "Dishwasher Phase"})
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "sensor.dishwasher_operation_state",
            CONF_SLUG: "ha-dishwasher-steps",
            CONF_ACTIVITY_NAME: "Dishwasher",
            CONF_ICON: "mdi:dishwasher",
            CONF_TEMPLATE: "steps",
            CONF_START_STATES: ["run"],
            CONF_END_STATES: ["finished", "ready"],
            CONF_TOTAL_STEPS: 4,
            CONF_CURRENT_STEP_ENTITY: "sensor.dishwasher_phase_index",
            CONF_STEP_LABELS: {"1": "Pre-Wash", "2": "Main Wash", "3": "Rinse", "4": "Dry"},
        }
    )
    state = make_mock_state("run", {"friendly_name": "Dishwasher"}, "sensor.dishwasher_operation_state")

    content = map_content(state, config, hass=hass)
    assert content["template"] == "steps"
    assert content["total_steps"] == 4
    assert content["current_step"] == 2
    assert content["progress"] == 0.5  # 2 / 4, auto-derived
    assert content["step_labels"] == ["Pre-Wash", "Main Wash", "Rinse", "Dry"]
    assert_valid_activity_content(content)


# ===========================================================================
# Climate — gauge and multi-series timeline from thermostat attributes
# ===========================================================================


def test_nest_thermostat_temperature_gauge() -> None:
    """Nest current_temperature on a gauge bounded by the thermostat's min/max."""
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "climate.nest_living_room",
            CONF_SLUG: "ha-nest-living-room",
            CONF_ACTIVITY_NAME: "Living Room",
            CONF_ICON: "",
            CONF_TEMPLATE: "gauge",
            CONF_VALUE_ATTRIBUTE: "current_temperature",
            CONF_MIN_VALUE: 10.0,
            CONF_MAX_VALUE: 32.0,
            CONF_UNIT: "°C",
        }
    )
    # In single-setpoint 'heat' mode a Nest reports `temperature` (the setpoint);
    # target_temp_high/low are null and only populate in heat_cool mode.
    state = make_mock_state(
        "heat",
        {
            "friendly_name": "Living Room",
            "current_temperature": 20.5,
            "temperature": 21.0,
            "current_humidity": 45.0,
            "hvac_action": "heating",
        },
        "climate.nest_living_room",
    )
    content = map_content(state, config)
    assert content["template"] == "gauge"
    assert content["value"] == 20.5
    assert content["min_value"] == 10.0
    assert content["max_value"] == 32.0
    assert content["unit"] == "°C"
    assert content["icon"] == "mdi:thermostat"  # climate domain default
    assert_valid_activity_content(content)


def test_ecobee_fahrenheit_gauge() -> None:
    """ecobee reports °F with a wide min/max; the value is clamped into range."""
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "climate.ecobee_main_floor",
            CONF_SLUG: "ha-ecobee-main",
            CONF_ICON: "",
            CONF_TEMPLATE: "gauge",
            CONF_VALUE_ATTRIBUTE: "current_temperature",
            CONF_MIN_VALUE: 44.6,
            CONF_MAX_VALUE: 95.0,
            CONF_UNIT: "°F",
        }
    )
    state = make_mock_state(
        "heat_cool",
        {"friendly_name": "Main Floor", "current_temperature": 74.5, "hvac_action": "idle"},
        "climate.ecobee_main_floor",
    )
    content = map_content(state, config)
    assert content["value"] == 74.5
    assert_valid_activity_content(content)


def test_climate_timeline_current_vs_target() -> None:
    """A two-series timeline: current temperature and the high setpoint."""
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "climate.nest_living_room",
            CONF_SLUG: "ha-nest-living-room",
            CONF_ICON: "",
            CONF_TEMPLATE: "timeline",
            CONF_SERIES: {"current_temperature": "Current", "target_temp_high": "Target"},
            CONF_UNIT: "°C",
        }
    )
    state = make_mock_state(
        "heat_cool",
        {
            "friendly_name": "Living Room",
            "current_temperature": 20.5,
            "target_temp_high": 24.0,
            "target_temp_low": 18.0,
        },
        "climate.nest_living_room",
    )
    content = map_content(state, config)
    assert content["template"] == "timeline"
    assert content["value"] == {"Current": 20.5, "Target": 24.0}
    assert content["unit"] == "°C"
    assert_valid_activity_content(content)


async def test_air_quality_timeline_three_entities(hass: HomeAssistant) -> None:
    """Three separate PM2.5 sensors bound as one multi-entity timeline (Alex's use case).

    Units auto-default from each sensor's unit_of_measurement and land only under
    labels that produced a value, satisfying the server's units-subset-of-values rule.
    """
    hass.states.async_set("sensor.bedroom_pm25", "12.5", {"unit_of_measurement": "ppm"})
    hass.states.async_set("sensor.office_pm25", "8.0", {"unit_of_measurement": "ppm"})
    hass.states.async_set("sensor.living_room_pm25", "21.0", {"unit_of_measurement": "ppm"})

    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.air_quality_monitor",
            CONF_SLUG: "ha-air-quality",
            CONF_ACTIVITY_NAME: "Air Quality",
            CONF_ICON: "mdi:air-filter",
            CONF_TEMPLATE: "timeline",
            CONF_SERIES_ENTITIES: [
                {CONF_LABEL: "Bedroom", CONF_ENTITY_ID: "sensor.bedroom_pm25"},
                {CONF_LABEL: "Office", CONF_ENTITY_ID: "sensor.office_pm25"},
                {CONF_LABEL: "Living Room", CONF_ENTITY_ID: "sensor.living_room_pm25"},
            ],
        }
    )
    state = make_mock_state("on", {"friendly_name": "Air Quality"}, "binary_sensor.air_quality_monitor")

    content = map_content(state, config, hass=hass)
    assert content["template"] == "timeline"
    assert content["value"] == {"Bedroom": 12.5, "Office": 8.0, "Living Room": 21.0}
    assert content["units"] == {"Bedroom": "ppm", "Office": "ppm", "Living Room": "ppm"}
    assert_valid_activity_content(content)


# ===========================================================================
# Energy / solar — production and state-of-charge gauges
# ===========================================================================


def test_solaredge_production_gauge() -> None:
    """SolarEdge instantaneous power (W) on a 0..rated gauge, read from the state."""
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "sensor.solaredge_current_power",
            CONF_SLUG: "ha-solar-power",
            CONF_ACTIVITY_NAME: "Solar Production",
            CONF_ICON: "",
            CONF_TEMPLATE: "gauge",
            CONF_MIN_VALUE: 0.0,
            CONF_MAX_VALUE: 6000.0,
            CONF_UNIT: "W",
        }
    )
    state = make_mock_state(
        "2483.12",
        {"friendly_name": "SolarEdge Current Power", "device_class": "power", "unit_of_measurement": "W"},
        "sensor.solaredge_current_power",
    )
    content = map_content(state, config)
    assert content["value"] == 2483.12
    assert content["icon"] == "mdi:flash"  # sensor/power device-class icon
    assert_valid_activity_content(content)


def test_powerwall_state_of_charge_gauge() -> None:
    """Tesla Powerwall battery SoC (%) on a 0..100 gauge."""
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "sensor.powerwall_charge",
            CONF_SLUG: "ha-powerwall-soc",
            CONF_ICON: "",
            CONF_TEMPLATE: "gauge",
            CONF_MIN_VALUE: 0.0,
            CONF_MAX_VALUE: 100.0,
            CONF_UNIT: "%",
        }
    )
    state = make_mock_state(
        "87",
        {"friendly_name": "Powerwall Charge", "device_class": "battery", "unit_of_measurement": "%"},
        "sensor.powerwall_charge",
    )
    content = map_content(state, config)
    assert content["value"] == 87.0
    assert content["icon"] == "mdi:battery"  # sensor/battery device-class icon
    assert_valid_activity_content(content)


# ===========================================================================
# Lighting — the 0-255 brightness rescale path
# ===========================================================================


def test_hue_brightness_gauge_rescaled_to_percent() -> None:
    """light brightness (0-255) is rescaled to a 0-100 gauge value."""
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "light.living_room_hue_bulb",
            CONF_SLUG: "ha-hue-brightness",
            CONF_ICON: "",
            CONF_TEMPLATE: "gauge",
            CONF_VALUE_ATTRIBUTE: "brightness",
            CONF_MIN_VALUE: 0.0,
            CONF_MAX_VALUE: 100.0,
            CONF_UNIT: "%",
        }
    )
    state = make_mock_state(
        "on",
        {"friendly_name": "Living Room", "brightness": 180, "color_temp_kelvin": 3200},
        "light.living_room_hue_bulb",
    )
    content = map_content(state, config)
    # 180 / 255 * 100 ≈ 71 (rounded by the 0-255 rescale helper)
    assert content["value"] == 71
    assert_valid_activity_content(content)


# ===========================================================================
# Security — alarm panel alert activity lifecycle
# ===========================================================================


async def test_alarm_triggered_alert_lifecycle(hass: HomeAssistant) -> None:
    """alarm_control_panel: armed_away → triggered (critical alert) → disarmed (end)."""
    api = make_activity_api()
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "alarm_control_panel.home_alarm",
            CONF_SLUG: "ha-home-alarm",
            CONF_ACTIVITY_NAME: "Home Alarm",
            CONF_ICON: "mdi:shield-home",
            CONF_TEMPLATE: "alert",
            CONF_SEVERITY: "critical",
            CONF_START_STATES: ["triggered", "pending"],
            CONF_END_STATES: ["disarmed"],
            CONF_SUBTITLE_ATTRIBUTE: "changed_by",
            # Tapping the alert deep-links into the alarm dashboard (the headline
            # "tap to act" affordance) — emitted on every template.
            CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/security",
        }
    )
    manager = ActivityManager(hass, api, [config], make_mock_entry())

    hass.states.async_set(
        "alarm_control_panel.home_alarm",
        "armed_away",
        {"friendly_name": "Home Alarm", "changed_by": "keypad_1"},
    )
    await manager.async_start()
    api.create_activity.assert_not_called()

    hass.states.async_set(
        "alarm_control_panel.home_alarm",
        "triggered",
        {"friendly_name": "Home Alarm", "changed_by": "Front Door"},
    )
    await hass.async_block_till_done()
    api.create_activity.assert_awaited_once()
    start = _ongoing(api)[0]
    assert start["template"] == "alert"
    assert start["severity"] == "critical"
    assert start["subtitle"] == "Front Door"
    assert start["tap_action"]["url"] == "homeassistant://navigate/lovelace/security"

    await end_activity_via_state(
        manager,
        hass,
        "alarm_control_panel.home_alarm",
        "disarmed",
        {"friendly_name": "Home Alarm", "changed_by": "keypad_1"},
    )

    for content in _ongoing(api):
        assert_valid_activity_content(content)
    for content in _ended(api):
        assert_valid_activity_content(content)
    assert _ended(api)

    await manager.async_stop()


# ===========================================================================
# Board — a multi-entity status dashboard (tiles read from separate entities)
# ===========================================================================


def test_home_status_board_content(hass: HomeAssistant) -> None:
    """A 'Home Status' board folds three unrelated entities into one Live Activity.

    The anchor (``binary_sensor.home_occupied``) owns start/end while each tile binds
    to its *own* entity, so temperature, a door and humidity — entities no single
    template could combine — render side by side. Tile values are read from
    ``hass.states`` at map time, which is why the real ``hass`` fixture holds them.
    """
    hass.states.async_set(
        "sensor.living_room_temperature",
        "21.5",
        {"friendly_name": "Living Room Temperature", "device_class": "temperature", "unit_of_measurement": "°C"},
    )
    hass.states.async_set(
        "binary_sensor.front_door",
        "open",
        {"friendly_name": "Front Door", "device_class": "door"},
    )
    hass.states.async_set(
        "sensor.living_room_humidity",
        "48",
        {"friendly_name": "Living Room Humidity", "device_class": "humidity", "unit_of_measurement": "%"},
    )

    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.home_occupied",
            CONF_SLUG: "ha-home-status",
            CONF_ACTIVITY_NAME: "Home Status",
            CONF_ICON: "mdi:home-account",
            CONF_TEMPLATE: "board",
            CONF_START_STATES: ["on"],
            CONF_END_STATES: ["off"],
            CONF_TILES: [
                {CONF_LABEL: "Temperature", CONF_ENTITY_ID: "sensor.living_room_temperature", CONF_UNIT: "°C"},
                {CONF_LABEL: "Front Door", CONF_ENTITY_ID: "binary_sensor.front_door"},
                {CONF_LABEL: "Humidity", CONF_ENTITY_ID: "sensor.living_room_humidity", CONF_UNIT: "%"},
            ],
        }
    )
    state = make_mock_state("on", {"friendly_name": "Home Occupied"}, "binary_sensor.home_occupied")

    content = map_content(state, config, hass=hass)
    assert content["template"] == "board"
    # Board has no progress bar, but the server still requires the field in [0, 1].
    assert content["progress"] == 0.0

    tiles = content["tiles"]
    assert len(tiles) == 3
    by_label = {tile["label"]: tile for tile in tiles}
    assert by_label["Temperature"]["value"] == "21.5"
    assert by_label["Temperature"]["unit"] == "°C"
    assert by_label["Front Door"]["value"] == "open"
    assert "unit" not in by_label["Front Door"]  # no unit configured for the door tile
    assert by_label["Humidity"]["value"] == "48"
    assert by_label["Humidity"]["unit"] == "%"

    assert_valid_activity_content(content)


def test_board_skips_unavailable_tile(hass: HomeAssistant) -> None:
    """An unavailable tile entity is dropped, not rendered with a blank value."""
    hass.states.async_set(
        "sensor.living_room_temperature",
        "20.0",
        {"friendly_name": "Living Room Temperature", "device_class": "temperature", "unit_of_measurement": "°C"},
    )
    hass.states.async_set("sensor.living_room_humidity", "unavailable", {"friendly_name": "Living Room Humidity"})

    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.home_occupied",
            CONF_SLUG: "ha-home-status",
            CONF_ACTIVITY_NAME: "Home Status",
            CONF_ICON: "mdi:home-account",
            CONF_TEMPLATE: "board",
            CONF_START_STATES: ["on"],
            CONF_END_STATES: ["off"],
            CONF_TILES: [
                {CONF_LABEL: "Temperature", CONF_ENTITY_ID: "sensor.living_room_temperature", CONF_UNIT: "°C"},
                {CONF_LABEL: "Humidity", CONF_ENTITY_ID: "sensor.living_room_humidity", CONF_UNIT: "%"},
            ],
        }
    )
    state = make_mock_state("on", {"friendly_name": "Home Occupied"}, "binary_sensor.home_occupied")

    content = map_content(state, config, hass=hass)
    labels = [tile["label"] for tile in content["tiles"]]
    assert labels == ["Temperature"]  # the unavailable humidity tile is skipped
    assert_valid_activity_content(content)


# ===========================================================================
# Log — an append-only event feed (one line per state, server-valid shape)
# ===========================================================================


def test_security_log_single_line_content(hass: HomeAssistant) -> None:
    """A security log renders the current state as one formatted, server-valid line.

    ``map_content`` emits a single line for the *current* state; the manager later
    overrides it with the accumulated newest-first ring buffer (covered by the log
    lifecycle test). Here we pin the per-state line shape that buffer is built from.
    """
    config = make_entity_config(
        **{
            CONF_ENTITY_ID: "sensor.security_event",
            CONF_SLUG: "ha-security-log",
            CONF_ACTIVITY_NAME: "Security Log",
            CONF_ICON: "mdi:shield-alert",
            CONF_TEMPLATE: "log",
            CONF_START_STATES: ["motion_detected"],
            CONF_END_STATES: ["cleared"],
            CONF_LOG_LEVEL_ATTRIBUTE: "severity",
        }
    )
    state = make_mock_state(
        "motion_detected",
        {"friendly_name": "Security Event", "severity": "warn"},
        "sensor.security_event",
    )

    content = map_content(state, config, hass=hass)
    assert content["template"] == "log"
    assert content["progress"] == 0.0
    assert len(content["lines"]) == 1
    line = content["lines"][0]
    # "motion_detected" → underscores spaced out, then capitalized.
    assert line["text"] == "Motion detected"
    # The severity attribute resolved to a recognised log level.
    assert line["level"] == "warn"

    assert_valid_activity_content(content)
