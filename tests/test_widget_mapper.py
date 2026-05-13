"""Tests for the widget mapper."""

from unittest.mock import MagicMock

from custom_components.pushward.const import (
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_LABEL,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_SEVERITY,
    CONF_STAT_ROWS,
    CONF_TAP_ACTION_FOREGROUND,
    CONF_TAP_ACTION_URL,
    CONF_UNIT,
    CONF_VALUE_ATTRIBUTE,
    CONF_WIDGET_TEMPLATE,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_PROGRESS,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_STATUS,
    WIDGET_TEMPLATE_VALUE,
)
from custom_components.pushward.widget_mapper import (
    map_widget_content,
    widget_name_from_config,
)
from tests.conftest import make_mock_state, make_widget_config


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

    # Out-of-range clamped
    state = make_mock_state("2.5", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    content = map_widget_content(hass, config)
    assert content["value"] == 1.0

    # Non-numeric → None (skip)
    state = make_mock_state("playing", entity_id="sensor.users")
    hass = _make_hass({"sensor.users": state})
    assert map_widget_content(hass, config) is None


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


def test_stat_list_caps_at_4_rows():
    rows = [{"label": f"Row {i}", "entity_id": f"sensor.s{i}"} for i in range(6)]
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST, CONF_STAT_ROWS: rows})
    states = {f"sensor.s{i}": make_mock_state(str(i), entity_id=f"sensor.s{i}") for i in range(6)}
    hass = _make_hass(states)

    content = map_widget_content(hass, config)
    assert content is not None
    assert len(content["stat_rows"]) == 4


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
