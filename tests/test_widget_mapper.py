"""Tests for the widget mapper."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from custom_components.pushward.const import (
    CONF_BATTERY_DEVICES,
    CONF_END_DATE_ATTRIBUTE,
    CONF_ENTITY_ID,
    CONF_EXPIRED_TEXT,
    CONF_FLOW_NODES,
    CONF_ICON,
    CONF_LABEL,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_SCHEDULE_ATTRIBUTES,
    CONF_SCHEDULE_HIGH_MIN,
    CONF_SCHEDULE_LOW_MAX,
    CONF_SECONDARY_URL,
    CONF_SECONDARY_URL_FOREGROUND,
    CONF_SECONDARY_URL_TITLE,
    CONF_SEVERITY,
    CONF_START_DATE_ATTRIBUTE,
    CONF_STAT_ROWS,
    CONF_SUBTITLE_TIMER_ENTITY,
    CONF_SUBTITLE_TIMER_STYLE,
    CONF_TAP_ACTION_FOREGROUND,
    CONF_TAP_ACTION_URL,
    CONF_UNIT,
    CONF_URL,
    CONF_URL_FOREGROUND,
    CONF_URL_TITLE,
    CONF_VALUE_ATTRIBUTE,
    CONF_VALUE_SCALE,
    CONF_WIDGET_BATTERY_SORT,
    CONF_WIDGET_TEMPLATE,
    VALUE_SCALE_FRACTION,
    VALUE_SCALE_PERCENT,
    WIDGET_BATTERY_SORT_KEYS,
    WIDGET_GROUP_TEMPLATES,
    WIDGET_MAX_FLOW_INPUTS,
    WIDGET_MAX_SCHEDULE_PERIODS,
    WIDGET_MAX_STAT_ROWS,
    WIDGET_MAX_TREND_POINTS,
    WIDGET_TEMPLATE_BATTERY,
    WIDGET_TEMPLATE_COUNTDOWN,
    WIDGET_TEMPLATE_FLOW,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_PROGRESS,
    WIDGET_TEMPLATE_SCHEDULE,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_STATUS,
    WIDGET_TEMPLATE_TREND,
    WIDGET_TEMPLATE_VALUE,
)
from custom_components.pushward.widget_mapper import (
    _GROUP_MAPPERS,
    map_widget_content,
    widget_name_from_config,
)
from tests.conftest import make_mock_state, make_widget_config
from tests.server_contract import assert_valid_widget_content


def _make_hass(states: dict[str, MagicMock]) -> MagicMock:
    """Build a mock HomeAssistant whose .states.get returns the provided dict."""
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=lambda eid: states.get(eid))
    return hass


def test_value_template_numeric_state():
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE, CONF_UNIT: "users"})
    state = make_mock_state("42", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)

    assert content is not None
    assert content["value"] == 42.0
    assert content["unit"] == "users"
    # No prev_value → no trend annotation.
    assert "trend" not in content


def test_value_template_trend_up():
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE})
    state = make_mock_state("100", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config, prev_value=50.0)
    assert content["trend"] == "up"

    content = map_widget_content(hass, config, prev_value=200.0)
    assert content["trend"] == "down"

    content = map_widget_content(hass, config, prev_value=100.0)
    assert content["trend"] == "flat"


def test_value_template_non_numeric_state():
    """Non-numeric value still renders other fields (icon/label)."""
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.app_state",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
            CONF_LABEL: "Status",
            CONF_ICON: "mdi:database",
        }
    )
    state = make_mock_state("running", entity_id="sensor.app_state")
    hass = _make_hass({"sensor.app_state": state})

    content = map_widget_content(hass, config)
    assert content is not None
    assert "value" not in content
    assert content.get("label") == "Status"
    assert content.get("icon") == "mdi:database"


def test_progress_template_clamps_and_requires_numeric():
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS})

    # In-range value
    state = make_mock_state("0.5", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    content = map_widget_content(hass, config)
    assert content is not None
    assert content["value"] == 0.5

    # Out-of-range clamped. Pinned to fraction so the clamp is what's under test;
    # left on auto, 2.5 would read as 2.5% instead.
    fraction = make_widget_config(
        **{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS, CONF_VALUE_SCALE: VALUE_SCALE_FRACTION}
    )
    state = make_mock_state("2.5", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    content = map_widget_content(hass, fraction)
    assert content["value"] == 1.0

    # Percent rescale still clamps above 100.
    percent = make_widget_config(
        **{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS, CONF_VALUE_SCALE: VALUE_SCALE_PERCENT}
    )
    state = make_mock_state("150", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    content = map_widget_content(hass, percent)
    assert content["value"] == 1.0

    # Non-numeric → None (skip)
    state = make_mock_state("playing", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    assert map_widget_content(hass, config) is None


def test_progress_auto_detects_percent_from_unit():
    """A properly tagged % sensor rescales even inside the ambiguous 0-1 band."""
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS})

    state = make_mock_state("65", {"unit_of_measurement": "%"}, entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    assert map_widget_content(hass, config)["value"] == 0.65

    state = make_mock_state("0.5", {"unit_of_measurement": "%"}, entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    assert map_widget_content(hass, config)["value"] == 0.005


def test_progress_auto_detects_percent_from_value_above_one():
    """No unit, but a fraction can never exceed 1.0, so 65 is a percent."""
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS})
    state = make_mock_state("65", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    assert map_widget_content(hass, config)["value"] == 0.65


def test_progress_auto_leaves_untagged_fraction_alone():
    """Regression guard: existing 0.0-1.0 users must not start reading as percents."""
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS})
    for raw, expected in (("0.5", 0.5), ("1", 1.0), ("0", 0.0)):
        state = make_mock_state(raw, entity_id="sensor.users")
        hass = _make_hass({"sensor.users": state})
        assert map_widget_content(hass, config)["value"] == expected


def test_progress_auto_tolerates_fraction_overshoot():
    """A ratio sensor overshooting 1.0 by rounding noise must still read as done.

    Rescaling it would collapse a finished bar to ~1% at the completion moment,
    where the old clamp-only code showed 100%.
    """
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS})
    for raw in ("1.0000001", "1.01", "1.05"):
        state = make_mock_state(raw, entity_id="sensor.users")
        hass = _make_hass({"sensor.users": state})
        assert map_widget_content(hass, config)["value"] == 1.0, raw

    # Clear of the tolerance band, so it is a percent again.
    state = make_mock_state("1.5", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    assert map_widget_content(hass, config)["value"] == 0.015


def test_progress_explicit_scale_overrides_auto_detect():
    state = make_mock_state("0.65", {"unit_of_measurement": "%"}, entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    # The % unit would make auto rescale; fraction says take it as-is.
    fraction = make_widget_config(
        **{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS, CONF_VALUE_SCALE: VALUE_SCALE_FRACTION}
    )
    assert map_widget_content(hass, fraction)["value"] == 0.65

    # And percent rescales a value auto would have left alone.
    percent = make_widget_config(
        **{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS, CONF_VALUE_SCALE: VALUE_SCALE_PERCENT}
    )
    state = make_mock_state("0.65", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    assert map_widget_content(hass, percent)["value"] == pytest.approx(0.0065)


def test_progress_auto_ignores_entity_unit_for_attribute_source():
    """The entity's % unit describes its state, not an arbitrary attribute.

    So an attribute value in the 0-1 band stays a fraction, and only the >1 rule applies.
    """
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS,
            CONF_VALUE_ATTRIBUTE: "ratio",
        }
    )
    state = make_mock_state("65", {"unit_of_measurement": "%", "ratio": 0.4}, entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    assert map_widget_content(hass, config)["value"] == 0.4

    state = make_mock_state("65", {"unit_of_measurement": "%", "ratio": 40}, entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    assert map_widget_content(hass, config)["value"] == 0.4


def test_gauge_template_min_max_required_and_clamped():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_GAUGE,
            CONF_MIN_VALUE: 0.0,
            CONF_MAX_VALUE: 100.0,
        }
    )
    state = make_mock_state("150", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)
    assert content is not None
    assert content["value"] == 100.0
    assert content["min_value"] == 0.0
    assert content["max_value"] == 100.0


def test_gauge_invalid_range_returns_none():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_GAUGE,
            CONF_MIN_VALUE: 100.0,
            CONF_MAX_VALUE: 0.0,
        }
    )
    state = make_mock_state("50", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    assert map_widget_content(hass, config) is None


def test_status_template_includes_severity():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STATUS,
            CONF_SEVERITY: "warning",
            CONF_LABEL: "Backup overdue",
        }
    )
    state = make_mock_state("on", entity_id="binary_sensor.backup")
    hass = _make_hass({"binary_sensor.backup": state})

    content = map_widget_content(hass, config)
    assert content is not None
    assert content["severity"] == "warning"
    assert content["label"] == "Backup overdue"
    # status template never emits a numeric value field
    assert "value" not in content


def test_status_template_unavailable_uses_static_fallback():
    """When entity is unavailable, status emits the static label/icon/severity."""
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STATUS,
            CONF_SEVERITY: "critical",
            CONF_LABEL: "Backup not running",
            CONF_ICON: "mdi:backup-restore",
        }
    )
    hass = _make_hass({})
    content = map_widget_content(hass, config)
    assert content is not None
    assert content["severity"] == "critical"
    assert content["label"] == "Backup not running"


def test_progress_unavailable_returns_none():
    """progress (numeric required) skips entirely when entity is unavailable."""
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS})
    hass = _make_hass({})
    assert map_widget_content(hass, config) is None


def test_value_attribute_override():
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "sensor.app",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
            CONF_VALUE_ATTRIBUTE: "count",
        }
    )
    state = make_mock_state(
        "running",
        attributes={"count": 7},
        entity_id="sensor.app",
    )
    hass = _make_hass({"sensor.app": state})

    content = map_widget_content(hass, config)
    assert content is not None
    assert content["value"] == 7.0


def test_stat_list_multiple_entities():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_STAT_ROWS: [
                {"label": "Users", "entity_id": "sensor.users"},
                {"label": "Active", "entity_id": "sensor.active", "unit": "online"},
                {
                    "label": "Idle",
                    "entity_id": "sensor.idle",
                    "value_attribute": "count",
                },
            ],
        }
    )
    states = {
        "sensor.users": make_mock_state("42", entity_id="sensor.users"),
        "sensor.active": make_mock_state("10", entity_id="sensor.active"),
        "sensor.idle": make_mock_state("running", attributes={"count": 3}, entity_id="sensor.idle"),
    }
    hass = _make_hass(states)

    content = map_widget_content(hass, config)
    assert content is not None
    assert content["stat_rows"] == [
        {"label": "Users", "value": "42"},
        {"label": "Active", "value": "10", "unit": "online"},
        {"label": "Idle", "value": "3"},
    ]


def test_stat_list_truncates_long_values():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_STAT_ROWS: [
                {"label": "x" * 50, "entity_id": "sensor.s"},
            ],
        }
    )
    state = make_mock_state("y" * 50, entity_id="sensor.s")
    hass = _make_hass({"sensor.s": state})

    content = map_widget_content(hass, config)
    assert content is not None
    row = content["stat_rows"][0]
    assert len(row["label"]) == 32  # WIDGET_STAT_LABEL_MAX
    assert len(row["value"]) == 32  # WIDGET_STAT_VALUE_MAX


def test_stat_list_skips_unavailable_rows():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_STAT_ROWS: [
                {"label": "Online", "entity_id": "sensor.online"},
                {"label": "Offline", "entity_id": "sensor.offline"},
            ],
        }
    )
    states = {
        "sensor.online": make_mock_state("42", entity_id="sensor.online"),
        # sensor.offline missing entirely
    }
    hass = _make_hass(states)

    content = map_widget_content(hass, config)
    assert content is not None
    assert len(content["stat_rows"]) == 1
    assert content["stat_rows"][0]["label"] == "Online"


def test_stat_list_caps_at_max_rows():
    rows = [{"label": f"Row {i}", "entity_id": f"sensor.s{i}"} for i in range(8)]
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST, CONF_STAT_ROWS: rows})
    states = {f"sensor.s{i}": make_mock_state(str(i), entity_id=f"sensor.s{i}") for i in range(8)}
    hass = _make_hass(states)

    content = map_widget_content(hass, config)
    assert content is not None
    assert len(content["stat_rows"]) == WIDGET_MAX_STAT_ROWS


def test_widget_name_from_config_falls_back_to_friendly_name():
    config = make_widget_config(**{"widget_name": ""})
    state = make_mock_state("42", attributes={"friendly_name": "Total Users"}, entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    # Different entity_id resolution to confirm fallback path
    config[CONF_ENTITY_ID] = "sensor.users"

    name = widget_name_from_config(config, hass)
    assert name == "Total Users"


def test_widget_name_from_config_uses_explicit_name():
    config = make_widget_config(**{"widget_name": "My Custom Widget"})
    name = widget_name_from_config(config, None)
    assert name == "My Custom Widget"


# --- Widget tap_action ---


def test_value_template_widget_tap_action_http():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
            CONF_TAP_ACTION_URL: "https://example.com",
            CONF_TAP_ACTION_FOREGROUND: True,
        }
    )
    state = make_mock_state("42", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)
    assert content["tap_action"] == {"url": "https://example.com", "foreground": True}


def test_widget_tap_action_custom_scheme():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
            CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
            CONF_TAP_ACTION_FOREGROUND: True,
        }
    )
    state = make_mock_state("42", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)
    assert content["tap_action"] == {
        "url": "homeassistant://navigate/lovelace/0",
        "foreground": True,
    }


def test_widget_tap_action_silent_webhook_injects_post():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
            CONF_TAP_ACTION_URL: "https://ha.local/api/services/script/foo",
            CONF_TAP_ACTION_FOREGROUND: False,
        }
    )
    state = make_mock_state("42", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)
    assert content["tap_action"] == {
        "url": "https://ha.local/api/services/script/foo",
        "foreground": False,
        "method": "POST",
    }


def test_widget_tap_action_omitted_when_empty():
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE})
    state = make_mock_state("42", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)
    assert "tap_action" not in content


def test_widget_tap_action_progress_template():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS,
            CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
        }
    )
    state = make_mock_state("0.5", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)
    assert content["tap_action"]["url"] == "homeassistant://navigate/lovelace/0"


def test_widget_tap_action_gauge_template():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_GAUGE,
            CONF_MIN_VALUE: 0.0,
            CONF_MAX_VALUE: 100.0,
            CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
        }
    )
    state = make_mock_state("42", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)
    assert content["tap_action"]["url"] == "homeassistant://navigate/lovelace/0"


# --- Widget url_action / secondary_url_action ---
#
# The server takes both slots on every widget template (unlike the activity side,
# which gates them on steps/alert), so these are wired with no template check.


def test_widget_url_actions_foreground_http():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
            CONF_URL: "https://example.com/primary",
            CONF_URL_FOREGROUND: True,
            CONF_URL_TITLE: "Open",
            CONF_SECONDARY_URL: "https://example.com/secondary",
            CONF_SECONDARY_URL_FOREGROUND: True,
            CONF_SECONDARY_URL_TITLE: "Details",
        }
    )
    state = make_mock_state("42", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)
    assert content["url_action"] == {
        "url": "https://example.com/primary",
        "foreground": True,
        "title": "Open",
    }
    assert content["secondary_url_action"] == {
        "url": "https://example.com/secondary",
        "foreground": True,
        "title": "Details",
    }
    # The contract check has teeth the mapper does not: URL/title caps, and the rule
    # that method/headers/body may only ride an http(s) URL.
    assert_valid_widget_content(content, WIDGET_TEMPLATE_VALUE)


def test_widget_url_actions_omitted_when_empty():
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE})
    state = make_mock_state("42", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)
    assert "url_action" not in content
    assert "secondary_url_action" not in content


def test_widget_url_actions_on_group_chrome_stat_list():
    """stat_list builds its chrome on a different path than the single-entity map."""
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_STAT_ROWS: [{"label": "Users", "entity_id": "sensor.users"}],
            CONF_URL: "https://example.com/primary",
            CONF_URL_FOREGROUND: True,
        }
    )
    state = make_mock_state("42", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)
    assert content["url_action"] == {"url": "https://example.com/primary", "foreground": True}


def test_widget_url_actions_on_static_status_fallback():
    """The unavailable-entity status fallback is a third content-building path."""
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STATUS,
            CONF_SEVERITY: "critical",
            CONF_LABEL: "Backup not running",
            CONF_URL: "https://example.com/primary",
            CONF_URL_FOREGROUND: True,
        }
    )
    hass = _make_hass({})

    content = map_widget_content(hass, config)
    assert content is not None
    assert content["url_action"] == {"url": "https://example.com/primary", "foreground": True}


def test_widget_tap_action_status_template():
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.alarm",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STATUS,
            CONF_SEVERITY: "info",
            CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
        }
    )
    state = make_mock_state("off", entity_id="binary_sensor.alarm")
    hass = _make_hass({"binary_sensor.alarm": state})

    content = map_widget_content(hass, config)
    assert content["tap_action"]["url"] == "homeassistant://navigate/lovelace/0"


def test_widget_tap_action_status_template_static_fallback():
    """When the bound entity is unavailable, tap_action still rides on the static status fallback."""
    config = make_widget_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.alarm",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STATUS,
            CONF_SEVERITY: "warning",
            CONF_LABEL: "Offline",
            CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
        }
    )
    hass = _make_hass({})  # entity not registered → unavailable

    content = map_widget_content(hass, config)
    assert content["tap_action"]["url"] == "homeassistant://navigate/lovelace/0"


def test_widget_tap_action_stat_list_template():
    rows = [{"label": "A", "entity_id": "sensor.a"}]
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_STAT_ROWS: rows,
            CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
        }
    )
    states = {"sensor.a": make_mock_state("1", entity_id="sensor.a")}
    hass = _make_hass(states)

    content = map_widget_content(hass, config)
    assert content["tap_action"]["url"] == "homeassistant://navigate/lovelace/0"


# ----- 1.6 templates -----


def _iso_in(**delta) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


def _trend_config(**overrides) -> dict:
    base = {CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_TREND, CONF_MIN_VALUE: None, CONF_MAX_VALUE: None}
    base.update(overrides)
    return make_widget_config(**base)


def test_trend_emits_value_and_points():
    config = _trend_config(**{CONF_UNIT: "W"})
    hass = _make_hass({"sensor.users": make_mock_state("120", entity_id="sensor.users")})

    content = map_widget_content(hass, config, points=[100.0, 110.0, 120.0])

    assert content["value"] == 120.0
    assert content["points"] == [100.0, 110.0, 120.0]
    # Optional bounds stay absent so the client auto-scales.
    assert "min_value" not in content
    assert "max_value" not in content
    assert_valid_widget_content(content, WIDGET_TEMPLATE_TREND)


def test_trend_needs_two_points():
    config = _trend_config()
    hass = _make_hass({"sensor.users": make_mock_state("120", entity_id="sensor.users")})

    assert map_widget_content(hass, config, points=[120.0]) is None
    assert map_widget_content(hass, config, points=None) is None


def test_trend_caps_points_at_48():
    config = _trend_config()
    hass = _make_hass({"sensor.users": make_mock_state("1", entity_id="sensor.users")})

    content = map_widget_content(hass, config, points=[float(i) for i in range(200)])

    assert len(content["points"]) == WIDGET_MAX_TREND_POINTS
    assert_valid_widget_content(content, WIDGET_TEMPLATE_TREND)


def test_trend_drops_both_bounds_when_inverted():
    config = _trend_config(**{CONF_MIN_VALUE: 50.0, CONF_MAX_VALUE: 10.0})
    hass = _make_hass({"sensor.users": make_mock_state("30", entity_id="sensor.users")})

    content = map_widget_content(hass, config, points=[10.0, 30.0])

    assert "min_value" not in content
    assert "max_value" not in content
    assert_valid_widget_content(content, WIDGET_TEMPLATE_TREND)


def test_trend_keeps_valid_bounds():
    config = _trend_config(**{CONF_MIN_VALUE: 0.0, CONF_MAX_VALUE: 100.0})
    hass = _make_hass({"sensor.users": make_mock_state("30", entity_id="sensor.users")})

    content = map_widget_content(hass, config, points=[10.0, 30.0])

    assert content["min_value"] == 0.0
    assert content["max_value"] == 100.0


def test_countdown_reads_state_as_timestamp():
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_COUNTDOWN, CONF_EXPIRED_TEXT: "Done"})
    state = make_mock_state(_iso_in(hours=3), entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)

    assert "end_date" in content
    assert content["expired_text"] == "Done"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_COUNTDOWN)


def test_countdown_uses_timer_finishes_at_by_default():
    config = make_widget_config(**{CONF_ENTITY_ID: "timer.laundry", CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_COUNTDOWN})
    state = make_mock_state("active", entity_id="timer.laundry", attributes={"finishes_at": _iso_in(minutes=45)})
    hass = _make_hass({"timer.laundry": state})

    content = map_widget_content(hass, config)

    assert "end_date" in content
    assert_valid_widget_content(content, WIDGET_TEMPLATE_COUNTDOWN)


def test_countdown_without_parseable_date_returns_none():
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_COUNTDOWN})
    hass = _make_hass({"sensor.users": make_mock_state("not a date", entity_id="sensor.users")})

    assert map_widget_content(hass, config) is None


def test_countdown_omits_out_of_bounds_date():
    """A date past the horizon is dropped rather than sent - one 422 kills the whole PATCH."""
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_COUNTDOWN})
    hass = _make_hass({"sensor.users": make_mock_state(_iso_in(days=800), entity_id="sensor.users")})

    assert map_widget_content(hass, config) is None


def test_countdown_drops_start_date_after_end():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_COUNTDOWN,
            CONF_START_DATE_ATTRIBUTE: "began",
            CONF_END_DATE_ATTRIBUTE: "ends",
        }
    )
    state = make_mock_state(
        "on",
        entity_id="sensor.users",
        attributes={"began": _iso_in(hours=9), "ends": _iso_in(hours=2)},
    )
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)

    assert "start_date" not in content
    assert_valid_widget_content(content, WIDGET_TEMPLATE_COUNTDOWN)


def test_progress_date_pair_without_value():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS,
            CONF_START_DATE_ATTRIBUTE: "began",
            CONF_END_DATE_ATTRIBUTE: "ends",
        }
    )
    state = make_mock_state(
        "running",
        entity_id="sensor.users",
        attributes={"began": _iso_in(hours=-1), "ends": _iso_in(hours=2)},
    )
    hass = _make_hass({"sensor.users": state})

    content = map_widget_content(hass, config)

    # A non-numeric state used to skip the push entirely; the window now carries it.
    # value is an explicit null, not an absent key - see the merge-patch test below.
    assert content["value"] is None
    assert content["start_date"] < content["end_date"]
    assert_valid_widget_content(content, WIDGET_TEMPLATE_PROGRESS)


def test_progress_nulls_stale_value_when_window_takes_over():
    """PATCH is an RFC 7396 merge: omitting value would preserve the stored fraction.

    Regression for a bar frozen at its last numeric reading. Pre-1.6 app builds read
    only `value`, so an omitted key left them rendering a stale percentage forever.
    Every released build decodes `value` as optional (`value ?? 0`), so the explicit
    null is safe and lands them on an empty bar instead.
    """
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS,
            CONF_START_DATE_ATTRIBUTE: "began",
            CONF_END_DATE_ATTRIBUTE: "ends",
        }
    )
    attrs = {"began": _iso_in(hours=-1), "ends": _iso_in(hours=2)}
    hass = _make_hass({"sensor.users": make_mock_state("0.4", entity_id="sensor.users", attributes=attrs)})
    numeric = map_widget_content(hass, config)
    assert numeric["value"] == 0.4

    # The entity goes non-numeric but stays available; the window still resolves.
    hass = _make_hass({"sensor.users": make_mock_state("running", entity_id="sensor.users", attributes=attrs)})
    cleared = map_widget_content(hass, config)

    assert "value" in cleared, "an absent key would preserve the stored 0.4 server-side"
    assert cleared["value"] is None
    assert_valid_widget_content(cleared, WIDGET_TEMPLATE_PROGRESS)


def test_group_mapper_table_covers_every_group_template():
    """Drift guard: a template added to WIDGET_GROUP_TEMPLATES needs a mapper."""
    assert set(_GROUP_MAPPERS) == set(WIDGET_GROUP_TEMPLATES)


def test_battery_maps_rows_and_charging():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_BATTERY,
            CONF_BATTERY_DEVICES: [
                {"name": "Phone", "entity_id": "sensor.phone", "charging_entity": "binary_sensor.phone_charging"},
                {"entity_id": "sensor.vacuum", "icon": "robot", "color": "green"},
            ],
        }
    )
    hass = _make_hass(
        {
            "sensor.phone": make_mock_state("64", entity_id="sensor.phone"),
            "binary_sensor.phone_charging": make_mock_state("on", entity_id="binary_sensor.phone_charging"),
            "sensor.vacuum": make_mock_state("18", entity_id="sensor.vacuum", attributes={"friendly_name": "Roomba"}),
        }
    )

    content = map_widget_content(hass, config)

    assert content["devices"][0] == {"name": "Phone", "level": 64.0, "charging": True}
    assert content["devices"][1] == {"name": "Roomba", "level": 18.0, "icon": "robot", "color": "green"}
    assert_valid_widget_content(content, WIDGET_TEMPLATE_BATTERY)


def test_battery_skips_unavailable_rows_and_clamps_level():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_BATTERY,
            CONF_BATTERY_DEVICES: [
                {"name": "Gone", "entity_id": "sensor.missing"},
                {"name": "Overfull", "entity_id": "sensor.odd"},
            ],
        }
    )
    hass = _make_hass({"sensor.odd": make_mock_state("140", entity_id="sensor.odd")})

    content = map_widget_content(hass, config)

    assert [d["name"] for d in content["devices"]] == ["Overfull"]
    assert content["devices"][0]["level"] == 100.0
    assert_valid_widget_content(content, WIDGET_TEMPLATE_BATTERY)


def test_battery_emits_sort_when_configured():
    """The fused HA dropdown value becomes the server's array of sort keys.

    A selector cannot express a key list, so the mapper assembles the wire shape
    the same way _subtitle_timer does. The server does the reordering, which is
    what makes this work on already-released apps: they render the stored array
    as-is and slice a per-family prefix out of it.
    """
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_BATTERY,
            CONF_BATTERY_DEVICES: [
                {"name": "Phone", "entity_id": "sensor.phone"},
                {"name": "Watch", "entity_id": "sensor.watch"},
            ],
            CONF_WIDGET_BATTERY_SORT: "level_asc",
        }
    )
    hass = _make_hass(
        {
            "sensor.phone": make_mock_state("64", entity_id="sensor.phone"),
            "sensor.watch": make_mock_state("18", entity_id="sensor.watch"),
        }
    )

    content = map_widget_content(hass, config)

    assert content["device_sort"] == [{"field": "level", "direction": "asc"}]
    # Row order is left exactly as configured - reordering is the server's job.
    assert [d["name"] for d in content["devices"]] == ["Phone", "Watch"]
    assert_valid_widget_content(content, WIDGET_TEMPLATE_BATTERY)


def test_battery_sort_keys_are_copied_per_call():
    """The emitted keys must not alias the module-level map.

    Mutating a shared dict would silently rewrite the mapping for every widget
    on the next call.
    """
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_BATTERY,
            CONF_BATTERY_DEVICES: [{"name": "Phone", "entity_id": "sensor.phone"}],
            CONF_WIDGET_BATTERY_SORT: "level_desc",
        }
    )
    hass = _make_hass({"sensor.phone": make_mock_state("64", entity_id="sensor.phone")})

    content = map_widget_content(hass, config)
    content["device_sort"][0]["direction"] = "asc"

    assert WIDGET_BATTERY_SORT_KEYS["level_desc"] == [{"field": "level", "direction": "desc"}]


def test_battery_omits_sort_when_unset():
    """Omitted, not empty - an empty array would still differ from an absent key.

    See _map_battery: merge-patch reads an absent key as "keep", and the setup
    POST replaces content wholesale, so unsetting the option clears it there.
    """
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_BATTERY,
            CONF_BATTERY_DEVICES: [{"name": "Phone", "entity_id": "sensor.phone"}],
        }
    )
    hass = _make_hass({"sensor.phone": make_mock_state("64", entity_id="sensor.phone")})

    content = map_widget_content(hass, config)

    assert "device_sort" not in content
    assert_valid_widget_content(content, WIDGET_TEMPLATE_BATTERY)


def test_battery_all_rows_unavailable_returns_none():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_BATTERY,
            CONF_BATTERY_DEVICES: [{"name": "Gone", "entity_id": "sensor.missing"}],
        }
    )
    assert map_widget_content(_make_hass({}), config) is None


def _schedule_state(count: int = 4, start_hour_offset: int = 0) -> MagicMock:
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=start_hour_offset)
    raw = [{"start": (base + timedelta(hours=i)).isoformat(), "value": 0.1 * i} for i in range(count)]
    return make_mock_state("0.2", entity_id="sensor.users", attributes={"raw_today": raw})


def test_schedule_maps_periods_with_bands():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_SCHEDULE,
            CONF_SCHEDULE_ATTRIBUTES: ["raw_today"],
            CONF_SCHEDULE_LOW_MAX: 0.1,
            CONF_SCHEDULE_HIGH_MIN: 0.25,
            CONF_UNIT: "PLN/kWh",
        }
    )
    hass = _make_hass({"sensor.users": _schedule_state()})

    content = map_widget_content(hass, config)

    assert [p["level"] for p in content["periods"]] == ["low", "low", "medium", "high"]
    assert content["unit"] == "PLN/kWh"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_SCHEDULE)


def test_schedule_omits_level_without_thresholds():
    config = make_widget_config(
        **{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_SCHEDULE, CONF_SCHEDULE_ATTRIBUTES: ["raw_today"]}
    )
    hass = _make_hass({"sensor.users": _schedule_state()})

    content = map_widget_content(hass, config)

    assert all("level" not in p for p in content["periods"])


def test_schedule_dedupes_and_sorts_overlapping_arrays():
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    today = [{"start": (base + timedelta(hours=i)).isoformat(), "value": i} for i in range(3)]
    # Tomorrow's array repeats the last hour of today with a corrected price.
    tomorrow = [{"start": (base + timedelta(hours=2)).isoformat(), "value": 99}]
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_SCHEDULE,
            CONF_SCHEDULE_ATTRIBUTES: ["raw_today", "raw_tomorrow"],
        }
    )
    state = make_mock_state("1", entity_id="sensor.users", attributes={"raw_today": today, "raw_tomorrow": tomorrow})

    content = map_widget_content(_make_hass({"sensor.users": state}), config)

    assert len(content["periods"]) == 3
    assert content["periods"][-1]["value"] == 99
    assert_valid_widget_content(content, WIDGET_TEMPLATE_SCHEDULE)


def test_schedule_truncates_to_cap_keeping_current_period():
    config = make_widget_config(
        **{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_SCHEDULE, CONF_SCHEDULE_ATTRIBUTES: ["raw_today"]}
    )
    # 60 hourly periods starting 10 h ago: the cap must drop history, not the future.
    hass = _make_hass({"sensor.users": _schedule_state(count=60, start_hour_offset=-10)})

    content = map_widget_content(hass, config)

    assert len(content["periods"]) == WIDGET_MAX_SCHEDULE_PERIODS
    first = datetime.fromisoformat(content["periods"][0]["start"])
    assert first <= datetime.now(UTC)
    assert_valid_widget_content(content, WIDGET_TEMPLATE_SCHEDULE)


def test_schedule_without_usable_periods_returns_none():
    config = make_widget_config(
        **{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_SCHEDULE, CONF_SCHEDULE_ATTRIBUTES: ["raw_today"]}
    )
    state = make_mock_state("1", entity_id="sensor.users", attributes={"raw_today": [{"start": "junk", "value": 1}]})

    assert map_widget_content(_make_hass({"sensor.users": state}), config) is None


def test_flow_assembles_slots():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_FLOW,
            CONF_UNIT: "W",
            CONF_FLOW_NODES: [
                {"slot": "input", "name": "Solar", "entity_id": "sensor.solar", "total_entity": "sensor.solar_today"},
                {"slot": "output", "entity_id": "sensor.house"},
                {"slot": "storage", "entity_id": "sensor.batt", "level_entity": "sensor.batt_soc"},
                {"slot": "exchange", "entity_id": "sensor.grid"},
            ],
        }
    )
    hass = _make_hass(
        {
            "sensor.solar": make_mock_state("3200", entity_id="sensor.solar"),
            "sensor.solar_today": make_mock_state("18.4", entity_id="sensor.solar_today"),
            "sensor.house": make_mock_state("1100", entity_id="sensor.house"),
            "sensor.batt": make_mock_state("-450", entity_id="sensor.batt"),
            "sensor.batt_soc": make_mock_state("72", entity_id="sensor.batt_soc"),
            "sensor.grid": make_mock_state("-1650", entity_id="sensor.grid"),
        }
    )

    content = map_widget_content(hass, config)

    flow = content["flow"]
    assert flow["inputs"] == [{"rate": 3200.0, "name": "Solar", "total": 18.4}]
    assert flow["storage"]["level"] == 72.0
    assert flow["exchange"]["rate"] == -1650.0
    assert content["unit"] == "W"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_FLOW)


def test_flow_caps_inputs_and_skips_unavailable():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_FLOW,
            CONF_FLOW_NODES: [{"slot": "input", "entity_id": f"sensor.in{i}"} for i in range(5)]
            + [{"slot": "output", "entity_id": "sensor.gone"}],
        }
    )
    hass = _make_hass({f"sensor.in{i}": make_mock_state(str(i * 100), entity_id=f"sensor.in{i}") for i in range(5)})

    content = map_widget_content(hass, config)

    assert len(content["flow"]["inputs"]) == WIDGET_MAX_FLOW_INPUTS
    assert "output" not in content["flow"]
    assert_valid_widget_content(content, WIDGET_TEMPLATE_FLOW)


def test_flow_drops_negative_total():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_FLOW,
            CONF_FLOW_NODES: [{"slot": "output", "entity_id": "sensor.house", "total_entity": "sensor.broken"}],
        }
    )
    hass = _make_hass(
        {
            "sensor.house": make_mock_state("900", entity_id="sensor.house"),
            "sensor.broken": make_mock_state("-3", entity_id="sensor.broken"),
        }
    )

    content = map_widget_content(hass, config)

    assert "total" not in content["flow"]["output"]
    assert_valid_widget_content(content, WIDGET_TEMPLATE_FLOW)


def test_flow_all_nodes_unavailable_returns_none():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_FLOW,
            CONF_FLOW_NODES: [{"slot": "output", "entity_id": "sensor.gone"}],
        }
    )
    assert map_widget_content(_make_hass({}), config) is None


def test_subtitle_timer_on_single_entity_template():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
            CONF_SUBTITLE_TIMER_ENTITY: "sensor.next_run",
            CONF_SUBTITLE_TIMER_STYLE: "relative",
        }
    )
    hass = _make_hass(
        {
            "sensor.users": make_mock_state("7", entity_id="sensor.users"),
            "sensor.next_run": make_mock_state(_iso_in(minutes=30), entity_id="sensor.next_run"),
        }
    )

    content = map_widget_content(hass, config)

    assert content["subtitle_timer"]["style"] == "relative"
    assert_valid_widget_content(content, WIDGET_TEMPLATE_VALUE)


def test_subtitle_timer_omitted_when_date_unusable():
    config = make_widget_config(
        **{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE, CONF_SUBTITLE_TIMER_ENTITY: "sensor.next_run"}
    )
    hass = _make_hass(
        {
            "sensor.users": make_mock_state("7", entity_id="sensor.users"),
            "sensor.next_run": make_mock_state("unknown", entity_id="sensor.next_run"),
        }
    )

    content = map_widget_content(hass, config)

    assert "subtitle_timer" not in content


def test_stat_row_timer_keeps_string_value_fallback():
    when = _iso_in(hours=4)
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_STAT_ROWS: [{"label": "Next", "entity_id": "sensor.next", "timer_style": "timer"}],
        }
    )
    hass = _make_hass({"sensor.next": make_mock_state(when, entity_id="sensor.next")})

    content = map_widget_content(hass, config)

    row = content["stat_rows"][0]
    assert row["timer"]["style"] == "timer"
    # The static string stays so older clients still render something.
    assert row["value"]
    assert_valid_widget_content(content, WIDGET_TEMPLATE_STAT_LIST)


def test_stat_row_timer_skipped_for_non_date_value():
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_STAT_ROWS: [{"label": "CPU", "entity_id": "sensor.cpu", "timer_style": "timer"}],
        }
    )
    hass = _make_hass({"sensor.cpu": make_mock_state("42", entity_id="sensor.cpu")})

    content = map_widget_content(hass, config)

    assert "timer" not in content["stat_rows"][0]
    assert_valid_widget_content(content, WIDGET_TEMPLATE_STAT_LIST)
