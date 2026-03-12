"""Tests for the PushWard content mapper."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.pushward.const import (
    CONF_ACCENT_COLOR,
    CONF_CURRENT_STEP_ATTR,
    CONF_ICON,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SEVERITY,
    CONF_TEMPLATE,
    CONF_TOTAL_STEPS,
)
from custom_components.pushward.content_mapper import (
    get_domain_defaults,
    map_completion_content,
    map_content,
    sanitize_slug,
)


def _make_state(state: str, attributes: dict | None = None) -> MagicMock:
    """Create a mock HA State object."""
    mock = MagicMock()
    mock.state = state
    mock.attributes = attributes or {}
    return mock


# --- sanitize_slug ---


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        ("sensor.washing_machine_status", "ha-sensor-washing-machine-status"),
        ("binary_sensor.front_door", "ha-binary-sensor-front-door"),
        ("switch.living_room_light", "ha-switch-living-room-light"),
        ("climate.hvac", "ha-climate-hvac"),
        ("vacuum.roborock", "ha-vacuum-roborock"),
        ("timer.tea", "ha-timer-tea"),
    ],
)
def test_sanitize_slug(entity_id: str, expected: str):
    assert sanitize_slug(entity_id) == expected


# --- map_content ---


def test_map_content_basic():
    state = _make_state("on", {"friendly_name": "Living Room Light"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
    }

    content = map_content(state, config)

    assert content["template"] == "generic"
    assert content["progress"] == 0.0
    assert content["state"] == "On"
    assert content["icon"] == "lightbulb.fill"
    assert content["subtitle"] == "Living Room Light"
    assert content["accent_color"] == "blue"


def test_map_content_with_progress():
    state = _make_state("running", {"friendly_name": "Washer", "progress": 75})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "washer",
        CONF_PROGRESS_ATTRIBUTE: "progress",
    }

    content = map_content(state, config)

    assert content["progress"] == 0.75


def test_map_content_clamps_progress():
    # Progress > 100 should clamp to 1.0
    state = _make_state("running", {"progress": 150})
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "gauge", CONF_PROGRESS_ATTRIBUTE: "progress"}

    content = map_content(state, config)
    assert content["progress"] == 1.0

    # Progress < 0 should clamp to 0.0
    state_neg = _make_state("running", {"progress": -20})
    content_neg = map_content(state_neg, config)
    assert content_neg["progress"] == 0.0


def test_map_content_with_remaining_time():
    state = _make_state("active", {"friendly_name": "Tea Timer", "remaining": 120})
    config = {
        CONF_TEMPLATE: "countdown",
        CONF_ICON: "timer",
        CONF_REMAINING_TIME_ATTR: "remaining",
    }

    content = map_content(state, config)

    assert content["remaining_time"] == 120


def test_map_content_with_accent_color():
    state = _make_state("heating", {"friendly_name": "HVAC"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "thermometer",
        CONF_ACCENT_COLOR: "red",
    }

    content = map_content(state, config)

    assert content["accent_color"] == "red"


# --- map_completion_content ---


def test_map_completion_content_no_last():
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "washer"}

    content = map_completion_content(config)

    assert content["progress"] == 1.0
    assert content["state"] == "Complete"
    assert content["icon"] == "checkmark.circle.fill"
    assert content["accent_color"] == "green"
    assert content["template"] == "generic"
    assert content["subtitle"] == ""


def test_map_completion_content_preserves_last():
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "lightbulb.fill"}
    last = {"progress": 0.75, "subtitle": "Living Room Lamp", "accent_color": "blue"}

    content = map_completion_content(config, last_content=last)

    assert content["progress"] == 0.75
    assert content["subtitle"] == "Living Room Lamp"
    assert content["state"] == "Complete"
    assert content["icon"] == "checkmark.circle.fill"
    assert content["accent_color"] == "green"


# --- get_domain_defaults ---


def test_get_domain_defaults_known():
    defaults = get_domain_defaults("binary_sensor")
    assert defaults["icon"] == "circle.fill"
    assert "on" in defaults["start_states"]
    assert "off" in defaults["end_states"]


def test_get_domain_defaults_climate():
    defaults = get_domain_defaults("climate")
    assert defaults["icon"] == "thermometer"
    assert "heating" in defaults["start_states"]
    assert "off" in defaults["end_states"]


def test_get_domain_defaults_unknown():
    defaults = get_domain_defaults("nonexistent_domain")
    assert defaults["icon"] == "questionmark.circle"
    assert defaults["start_states"] == []
    assert defaults["end_states"] == []


# --- countdown template ---


@patch("custom_components.pushward.content_mapper.time")
def test_map_content_countdown_with_remaining(mock_time):
    mock_time.time.return_value = 1000.0
    state = _make_state("active", {"friendly_name": "Tea Timer", "remaining": 120})
    config = {
        CONF_TEMPLATE: "countdown",
        CONF_ICON: "timer",
        CONF_REMAINING_TIME_ATTR: "remaining",
    }

    content = map_content(state, config)

    assert content["end_date"] == 1120
    assert content["remaining_time"] == 120


@patch("custom_components.pushward.content_mapper.time")
def test_map_content_countdown_no_remaining(mock_time):
    mock_time.time.return_value = 1000.0
    state = _make_state("active", {"friendly_name": "Timer"})
    config = {
        CONF_TEMPLATE: "countdown",
        CONF_ICON: "timer",
    }

    content = map_content(state, config)

    # No remaining_time_attribute → falls back to now (end_date = now + 0)
    assert content["end_date"] == 1000


@patch("custom_components.pushward.content_mapper.time")
def test_map_completion_content_countdown(mock_time):
    mock_time.time.return_value = 2000.0
    config = {CONF_TEMPLATE: "countdown", CONF_ICON: "timer"}

    content = map_completion_content(config)

    assert content["end_date"] == 2000
    assert content["state"] == "Complete"


# --- pipeline template ---


def test_map_content_pipeline_with_step_attribute():
    state = _make_state("running", {"friendly_name": "Build", "step": 3})
    config = {
        CONF_TEMPLATE: "pipeline",
        CONF_ICON: "hammer",
        CONF_TOTAL_STEPS: 5,
        CONF_CURRENT_STEP_ATTR: "step",
    }

    content = map_content(state, config)

    assert content["total_steps"] == 5
    assert content["current_step"] == 3
    # Auto-derived progress (no explicit progress_attribute)
    assert content["progress"] == pytest.approx(0.6)


def test_map_content_pipeline_clamps_current_step():
    state = _make_state("running", {"step": 99})
    config = {
        CONF_TEMPLATE: "pipeline",
        CONF_ICON: "hammer",
        CONF_TOTAL_STEPS: 4,
        CONF_CURRENT_STEP_ATTR: "step",
    }

    content = map_content(state, config)

    assert content["current_step"] == 4  # clamped to total_steps
    assert content["progress"] == 1.0


def test_map_content_pipeline_no_step_attribute():
    state = _make_state("running", {"friendly_name": "Deploy"})
    config = {
        CONF_TEMPLATE: "pipeline",
        CONF_ICON: "arrow.triangle.2.circlepath",
        CONF_TOTAL_STEPS: 3,
    }

    content = map_content(state, config)

    assert content["total_steps"] == 3
    assert content["current_step"] == 0
    assert content["progress"] == 0.0


def test_map_content_pipeline_explicit_progress_attribute():
    """When progress_attribute is set, auto-derive is skipped."""
    state = _make_state("running", {"step": 2, "pct": 80})
    config = {
        CONF_TEMPLATE: "pipeline",
        CONF_ICON: "hammer",
        CONF_TOTAL_STEPS: 4,
        CONF_CURRENT_STEP_ATTR: "step",
        CONF_PROGRESS_ATTRIBUTE: "pct",
    }

    content = map_content(state, config)

    assert content["total_steps"] == 4
    assert content["current_step"] == 2
    # progress comes from the explicit attribute (80/100), not auto-derived
    assert content["progress"] == 0.8


def test_map_completion_content_pipeline():
    config = {CONF_TEMPLATE: "pipeline", CONF_ICON: "hammer", CONF_TOTAL_STEPS: 5}

    content = map_completion_content(config)

    assert content["total_steps"] == 5
    assert content["current_step"] == 5
    assert content["progress"] == 1.0


# --- alert template ---


def test_map_content_alert_severity():
    state = _make_state("firing", {"friendly_name": "CPU Alert"})
    config = {
        CONF_TEMPLATE: "alert",
        CONF_ICON: "exclamationmark.triangle",
        CONF_SEVERITY: "critical",
    }

    content = map_content(state, config)

    assert content["severity"] == "critical"


def test_map_content_alert_default_severity():
    state = _make_state("firing", {"friendly_name": "Alert"})
    config = {
        CONF_TEMPLATE: "alert",
        CONF_ICON: "exclamationmark.triangle",
    }

    content = map_content(state, config)

    assert content["severity"] == "info"


def test_map_completion_content_alert():
    config = {CONF_TEMPLATE: "alert", CONF_ICON: "bell", CONF_SEVERITY: "warning"}

    content = map_completion_content(config)

    assert content["severity"] == "warning"
    assert content["state"] == "Complete"
