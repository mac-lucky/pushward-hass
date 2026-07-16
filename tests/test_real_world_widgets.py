"""Widget tests driven by realistic HA entities across all five widget templates.

Covers the home-screen / lock-screen widget surface with real-world sources:
a battery gauge from the Companion app, a temperature value tile, an electricity
price tile, an alarm-panel status tile, and a multi-sensor stat_list dashboard.
Every rendered payload is asserted against the public PushWard widget content
contract via :mod:`tests.server_contract`.
"""

from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from custom_components.pushward.const import (
    CONF_ENTITY_ID,
    CONF_LABEL,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_SEVERITY,
    CONF_SLUG,
    CONF_STAT_ROWS,
    CONF_UNIT,
    CONF_WIDGET_NAME,
    CONF_WIDGET_POLL_INTERVAL,
    CONF_WIDGET_TEMPLATE,
    CONF_WIDGET_TRIGGER_MODE,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_PROGRESS,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_STATUS,
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TRIGGER_POLL,
)
from custom_components.pushward.widget_manager import WidgetManager
from custom_components.pushward.widget_mapper import map_widget_content

from .conftest import make_mock_entry, make_widget_api, make_widget_config
from .server_contract import assert_valid_widget_content

# ---------------------------------------------------------------------------
# value template — temperature & electricity price tiles
# ---------------------------------------------------------------------------


def test_living_room_temperature_value(hass: HomeAssistant) -> None:
    """A temperature sensor renders a value tile with unit and a trend arrow."""
    hass.states.async_set(
        "sensor.living_room_temperature",
        "21.4",
        {"friendly_name": "Living Room Temperature", "device_class": "temperature", "unit_of_measurement": "°C"},
    )
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.living_room_temperature",
            CONF_SLUG: "ha-living-room-temp",
            CONF_WIDGET_NAME: "Living Room",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
            CONF_UNIT: "°C",
        }
    )

    # Rising temperature → trend up vs the previous reading.
    content = map_widget_content(hass, config, prev_value=20.9)
    assert content is not None
    assert content["value"] == 21.4
    assert content["unit"] == "°C"
    assert content["trend"] == "up"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_VALUE)


def test_nord_pool_price_value(hass: HomeAssistant) -> None:
    """A Nord Pool spot-price sensor renders a value tile with a currency-per-kWh unit."""
    hass.states.async_set(
        "sensor.nord_pool_se3_current_price",
        "0.0847",
        {"friendly_name": "Nord Pool SE3 Current Price", "unit_of_measurement": "SEK/kWh"},
    )
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.nord_pool_se3_current_price",
            CONF_SLUG: "ha-nordpool-se3",
            CONF_UNIT: "SEK/kWh",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
        }
    )
    content = map_widget_content(hass, config, prev_value=0.1234)
    assert content is not None
    assert content["value"] == 0.0847
    assert content["unit"] == "SEK/kWh"
    assert content["trend"] == "down"  # cheaper than the previous hour
    assert_valid_widget_content(content, WIDGET_TEMPLATE_VALUE)


def test_system_monitor_cpu_gauge(hass: HomeAssistant) -> None:
    """System Monitor processor-use percentage on a 0..100 gauge."""
    hass.states.async_set(
        "sensor.system_monitor_processor_use",
        "67",
        {"friendly_name": "System Monitor Processor Use", "unit_of_measurement": "%", "state_class": "measurement"},
    )
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.system_monitor_processor_use",
            CONF_SLUG: "ha-cpu-use",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_GAUGE,
            CONF_MIN_VALUE: 0.0,
            CONF_MAX_VALUE: 100.0,
            CONF_UNIT: "%",
        }
    )
    content = map_widget_content(hass, config, prev_value=23.0)
    assert content is not None
    assert content["value"] == 67.0
    assert content["trend"] == "up"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_GAUGE)


def test_non_numeric_value_tile_has_no_value(hass: HomeAssistant) -> None:
    """A value widget bound to a text sensor still renders (label/icon) but no value."""
    hass.states.async_set(
        "sensor.weather_condition",
        "partlycloudy",
        {"friendly_name": "Condition"},
    )
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.weather_condition",
            CONF_SLUG: "ha-weather-condition",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
        }
    )
    content = map_widget_content(hass, config)
    assert content is not None
    assert "value" not in content  # non-coercible → omitted, server accepts value-less tile
    assert_valid_widget_content(content, WIDGET_TEMPLATE_VALUE)


# ---------------------------------------------------------------------------
# gauge template — phone & CPU percentages
# ---------------------------------------------------------------------------


def test_phone_battery_gauge(hass: HomeAssistant) -> None:
    """Companion-app battery sensor (0-100 %) renders a 0..100 gauge."""
    hass.states.async_set(
        "sensor.pixel_8_battery_level",
        "72",
        {"friendly_name": "Pixel 8 Battery Level", "device_class": "battery", "unit_of_measurement": "%"},
    )
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.pixel_8_battery_level",
            CONF_SLUG: "ha-pixel-battery",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_GAUGE,
            CONF_MIN_VALUE: 0.0,
            CONF_MAX_VALUE: 100.0,
            CONF_UNIT: "%",
        }
    )
    content = map_widget_content(hass, config, prev_value=80.0)
    assert content is not None
    assert content["value"] == 72.0
    assert content["min_value"] == 0.0
    assert content["max_value"] == 100.0
    assert content["trend"] == "down"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_GAUGE)


def test_gauge_clamps_out_of_range_value(hass: HomeAssistant) -> None:
    """A reading above max is clamped to max so the payload stays server-valid."""
    hass.states.async_set(
        "sensor.cpu_load_percent",
        "135",  # spurious spike above 100
        {"friendly_name": "CPU Load", "unit_of_measurement": "%"},
    )
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.cpu_load_percent",
            CONF_SLUG: "ha-cpu-load",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_GAUGE,
            CONF_MIN_VALUE: 0.0,
            CONF_MAX_VALUE: 100.0,
            CONF_UNIT: "%",
        }
    )
    content = map_widget_content(hass, config)
    assert content is not None
    assert content["value"] == 100.0  # clamped
    assert_valid_widget_content(content, WIDGET_TEMPLATE_GAUGE)


# ---------------------------------------------------------------------------
# progress template — fractional 0..1 source
# ---------------------------------------------------------------------------


def test_backup_progress_fraction(hass: HomeAssistant) -> None:
    """progress widgets expect a 0..1 fraction (rendered as a bar)."""
    hass.states.async_set(
        "sensor.backup_progress",
        "0.65",
        {"friendly_name": "Backup Progress"},
    )
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.backup_progress",
            CONF_SLUG: "ha-backup-progress",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS,
        }
    )
    content = map_widget_content(hass, config)
    assert content is not None
    assert content["value"] == 0.65
    assert_valid_widget_content(content, WIDGET_TEMPLATE_PROGRESS)


def test_progress_percent_source_is_rescaled(hass: HomeAssistant) -> None:
    """A 0-100 percentage fed to a progress widget rescales rather than pinning to 100%."""
    hass.states.async_set("sensor.download_percent", "87", {"friendly_name": "Download"})
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.download_percent",
            CONF_SLUG: "ha-download-progress",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS,
        }
    )
    content = map_widget_content(hass, config)
    assert content is not None
    assert content["value"] == 0.87
    assert_valid_widget_content(content, WIDGET_TEMPLATE_PROGRESS)


# ---------------------------------------------------------------------------
# status template — alarm panel
# ---------------------------------------------------------------------------


def test_alarm_panel_status_widget(hass: HomeAssistant) -> None:
    """An alarm panel renders a status tile carrying severity + static label/icon."""
    hass.states.async_set(
        "alarm_control_panel.home",
        "armed_away",
        {"friendly_name": "Home Alarm"},
    )
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "alarm_control_panel.home",
            CONF_SLUG: "ha-home-alarm",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STATUS,
            CONF_LABEL: "Armed Away",
            CONF_SEVERITY: "warning",
        }
    )
    content = map_widget_content(hass, config)
    assert content is not None
    assert content["severity"] == "warning"
    assert content["label"] == "Armed Away"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_STATUS)


def test_status_widget_unavailable_uses_static_fallback(hass: HomeAssistant) -> None:
    """When the alarm panel is unavailable, the status tile still renders statically."""
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "alarm_control_panel.home",
            CONF_SLUG: "ha-home-alarm",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STATUS,
            CONF_LABEL: "Alarm",
            CONF_SEVERITY: "info",
        }
    )
    # No state set at all == unavailable.
    content = map_widget_content(hass, config)
    assert content is not None
    assert content["severity"] == "info"
    assert content["label"] == "Alarm"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_STATUS)


# ---------------------------------------------------------------------------
# stat_list template — a home dashboard across several sensors
# ---------------------------------------------------------------------------


def test_home_dashboard_stat_list(hass: HomeAssistant) -> None:
    """stat_list binds several distinct sensors into one multi-row tile."""
    hass.states.async_set(
        "sensor.living_room_temperature",
        "21.4",
        {"device_class": "temperature", "unit_of_measurement": "°C"},
    )
    hass.states.async_set(
        "sensor.bedroom_humidity",
        "48",
        {"device_class": "humidity", "unit_of_measurement": "%"},
    )
    hass.states.async_set(
        "sensor.house_power",
        "742",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    config = make_widget_config(
        **{
            CONF_SLUG: "ha-home-dashboard",
            CONF_WIDGET_NAME: "Home",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_STAT_ROWS: [
                {CONF_ENTITY_ID: "sensor.living_room_temperature", CONF_LABEL: "Living Room", CONF_UNIT: "°C"},
                {CONF_ENTITY_ID: "sensor.bedroom_humidity", CONF_LABEL: "Bedroom RH", CONF_UNIT: "%"},
                {CONF_ENTITY_ID: "sensor.house_power", CONF_LABEL: "Power", CONF_UNIT: "W"},
            ],
        }
    )
    content = map_widget_content(hass, config)
    assert content is not None
    rows = content["stat_rows"]
    assert [r["label"] for r in rows] == ["Living Room", "Bedroom RH", "Power"]
    assert rows[0]["value"] == "21.4"
    assert rows[2]["unit"] == "W"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_STAT_LIST)


def test_stat_list_skips_unavailable_rows(hass: HomeAssistant) -> None:
    """Rows whose entity is unavailable are dropped, the rest still render."""
    hass.states.async_set("sensor.living_room_temperature", "21.4", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.bedroom_humidity", STATE_UNAVAILABLE)
    config = make_widget_config(
        **{
            CONF_SLUG: "ha-home-dashboard",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_STAT_ROWS: [
                {CONF_ENTITY_ID: "sensor.living_room_temperature", CONF_LABEL: "Living Room", CONF_UNIT: "°C"},
                {CONF_ENTITY_ID: "sensor.bedroom_humidity", CONF_LABEL: "Bedroom RH", CONF_UNIT: "%"},
            ],
        }
    )
    content = map_widget_content(hass, config)
    assert content is not None
    assert len(content["stat_rows"]) == 1
    assert content["stat_rows"][0]["label"] == "Living Room"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_STAT_LIST)


# ---------------------------------------------------------------------------
# manager flow — create on start, PATCH on change, all payloads server-valid
# ---------------------------------------------------------------------------


async def test_temperature_widget_create_and_update_flow(hass: HomeAssistant) -> None:
    """End-to-end: initial create + a state-change PATCH, both server-valid."""
    api = make_widget_api()
    hass.states.async_set(
        "sensor.living_room_temperature",
        "21.0",
        {"friendly_name": "Living Room Temperature", "device_class": "temperature", "unit_of_measurement": "°C"},
    )
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.living_room_temperature",
            CONF_SLUG: "ha-living-room-temp",
            CONF_WIDGET_NAME: "Living Room",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
            CONF_UNIT: "°C",
        }
    )
    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()

    api.create_widget.assert_awaited_once()
    create = api.create_widget.call_args.kwargs
    assert create["slug"] == "ha-living-room-temp"
    assert create["content"]["value"] == 21.0
    assert_valid_widget_content(create["content"], create["template"])

    api.reset_mock()
    hass.states.async_set(
        "sensor.living_room_temperature",
        "22.5",
        {"friendly_name": "Living Room Temperature", "device_class": "temperature", "unit_of_measurement": "°C"},
    )
    await hass.async_block_till_done()

    assert api.patch_widget.await_count == 1
    body = api.patch_widget.call_args.args[1]
    assert body["content"]["value"] == 22.5
    assert body["content"]["trend"] == "up"
    assert_valid_widget_content(body["content"], WIDGET_TEMPLATE_VALUE)

    await manager.async_stop()


async def test_price_widget_poll_mode_sets_push_throttle(hass: HomeAssistant) -> None:
    """A price tile polled on an interval couples push_throttle to that interval."""
    api = make_widget_api()
    hass.states.async_set(
        "sensor.nord_pool_se3_current_price",
        "0.0847",
        {"friendly_name": "Nord Pool SE3 Current Price", "unit_of_measurement": "SEK/kWh"},
    )
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.nord_pool_se3_current_price",
            CONF_SLUG: "ha-nordpool-se3",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
            CONF_UNIT: "SEK/kWh",
            CONF_WIDGET_TRIGGER_MODE: WIDGET_TRIGGER_POLL,
            CONF_WIDGET_POLL_INTERVAL: 300,
        }
    )
    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()

    create = api.create_widget.call_args.kwargs
    assert create["push_throttle"] == 300
    assert_valid_widget_content(create["content"], create["template"])

    await manager.async_stop()


async def test_gauge_widget_deferred_create_until_valid(hass: HomeAssistant) -> None:
    """A gauge bound to an unavailable entity defers its create until a real value arrives."""
    api = make_widget_api()
    hass.states.async_set("sensor.pixel_8_battery_level", STATE_UNAVAILABLE)
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.pixel_8_battery_level",
            CONF_SLUG: "ha-pixel-battery",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_GAUGE,
            CONF_MIN_VALUE: 0.0,
            CONF_MAX_VALUE: 100.0,
            CONF_UNIT: "%",
        }
    )
    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    # A gauge cannot render without a numeric value → the initial POST is deferred.
    api.create_widget.assert_not_called()

    hass.states.async_set(
        "sensor.pixel_8_battery_level",
        "64",
        {"device_class": "battery", "unit_of_measurement": "%"},
    )
    await hass.async_block_till_done()

    # The first valid state fires the deferred create (a POST, not a PATCH).
    api.create_widget.assert_awaited_once()
    api.patch_widget.assert_not_called()
    create = api.create_widget.call_args.kwargs
    assert create["content"]["value"] == 64.0
    assert_valid_widget_content(create["content"], create["template"])

    await manager.async_stop()


async def test_status_widget_skips_patch_when_content_unchanged(hass: HomeAssistant) -> None:
    """The diff cache suppresses a PATCH when a state change renders identical content."""
    api = make_widget_api()
    hass.states.async_set("alarm_control_panel.home", "armed_away", {"friendly_name": "Home Alarm"})
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "alarm_control_panel.home",
            CONF_SLUG: "ha-home-alarm",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STATUS,
            CONF_LABEL: "Armed",
            CONF_SEVERITY: "warning",
        }
    )
    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    api.create_widget.assert_awaited_once()

    # armed_away → armed_home: the status tile's content is static (label/severity
    # from config), so the rendered payload is unchanged and no PATCH is sent.
    hass.states.async_set("alarm_control_panel.home", "armed_home", {"friendly_name": "Home Alarm"})
    await hass.async_block_till_done()
    api.patch_widget.assert_not_called()

    await manager.async_stop()
