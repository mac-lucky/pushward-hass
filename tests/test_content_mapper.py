"""Tests for the PushWard content mapper."""

from unittest.mock import patch

import pytest

from custom_components.pushward.const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SECONDARY_URL,
    CONF_SEVERITY,
    CONF_STATE_LABELS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_TEMPLATE,
    CONF_TOTAL_STEPS,
    CONF_URL,
)
from custom_components.pushward.content_mapper import (
    _add_url_deeplinks,
    get_domain_defaults,
    map_completion_content,
    map_content,
    sanitize_slug,
)

from .conftest import make_mock_state as _make_state

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
    # Progress 255 (max of 0-255 range) should be 1.0
    state = _make_state("running", {"progress": 255})
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "gauge", CONF_PROGRESS_ATTRIBUTE: "progress"}

    content = map_content(state, config)
    assert content["progress"] == 1.0

    # Progress < 0 should clamp to 0.0
    state_neg = _make_state("running", {"progress": -20})
    content_neg = map_content(state_neg, config)
    assert content_neg["progress"] == 0.0


def test_map_content_progress_brightness_scale():
    """Brightness (0-255) is auto-detected and scaled correctly."""
    # brightness=26 in HA ≈ 10% → should be ~0.102
    state = _make_state("on", {"friendly_name": "Lamp", "brightness": 26})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
        CONF_PROGRESS_ATTRIBUTE: "brightness",
    }

    content = map_content(state, config)

    assert content["progress"] == pytest.approx(26 / 255, abs=0.01)


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


# --- Feature 1: subtitle attribute ---


def test_map_content_subtitle_attribute():
    """subtitle_attribute reads from entity attribute instead of friendly_name."""
    state = _make_state("playing", {"friendly_name": "Speaker", "media_title": "My Song"})
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "play.fill", CONF_SUBTITLE_ATTRIBUTE: "media_title"}

    content = map_content(state, config)

    assert content["subtitle"] == "My Song"


def test_map_content_subtitle_attribute_fallback():
    """subtitle_attribute falls back to friendly_name when attribute is missing."""
    state = _make_state("playing", {"friendly_name": "Speaker"})
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "play.fill", CONF_SUBTITLE_ATTRIBUTE: "media_title"}

    content = map_content(state, config)

    assert content["subtitle"] == "Speaker"


def test_map_content_subtitle_attribute_empty():
    """No subtitle_attribute configured uses friendly_name."""
    state = _make_state("on", {"friendly_name": "Lamp"})
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "lightbulb.fill"}

    content = map_content(state, config)

    assert content["subtitle"] == "Lamp"


# --- Feature 2: state labels ---


def test_map_content_state_labels():
    """Custom state labels map state to display text."""
    state = _make_state("heating", {"friendly_name": "HVAC"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "thermometer",
        CONF_STATE_LABELS: {"heating": "Warming Up", "cooling": "Cooling Down"},
    }

    content = map_content(state, config)

    assert content["state"] == "Warming Up"


def test_map_content_state_labels_fallback():
    """Missing state label falls back to default formatting."""
    state = _make_state("idle", {"friendly_name": "HVAC"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "thermometer",
        CONF_STATE_LABELS: {"heating": "Warming Up"},
    }

    content = map_content(state, config)

    assert content["state"] == "Idle"


def test_map_content_state_labels_empty_dict():
    """Empty state_labels dict uses default formatting."""
    state = _make_state("running", {"friendly_name": "Washer"})
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "washer", CONF_STATE_LABELS: {}}

    content = map_content(state, config)

    assert content["state"] == "Running"


# --- Feature 4: completion message ---


def test_map_completion_content_custom_message():
    """Custom completion message replaces 'Complete'."""
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "washer", CONF_COMPLETION_MESSAGE: "Wash Done"}

    content = map_completion_content(config)

    assert content["state"] == "Wash Done"


def test_map_completion_content_default_message():
    """No completion message defaults to 'Complete'."""
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "washer"}

    content = map_completion_content(config)

    assert content["state"] == "Complete"


@patch("custom_components.pushward.content_mapper.time")
def test_map_content_countdown_completion_message(mock_time):
    """Countdown template includes completion_message when configured."""
    mock_time.time.return_value = 1000.0
    state = _make_state("active", {"friendly_name": "Timer", "remaining": 60})
    config = {
        CONF_TEMPLATE: "countdown",
        CONF_ICON: "timer",
        CONF_REMAINING_TIME_ATTR: "remaining",
        CONF_COMPLETION_MESSAGE: "Time's Up!",
    }

    content = map_content(state, config)

    assert content["completion_message"] == "Time's Up!"


# --- Feature 5: URL deep links ---


def test_map_content_with_urls():
    """URLs are included in content when configured."""
    state = _make_state("on", {"friendly_name": "Washer"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "washer",
        CONF_URL: "https://ha.local/lovelace/laundry",
        CONF_SECONDARY_URL: "https://ha.local/lovelace/overview",
    }

    content = map_content(state, config)

    assert content["url"] == "https://ha.local/lovelace/laundry"
    assert content["secondary_url"] == "https://ha.local/lovelace/overview"


def test_map_content_urls_omitted_when_empty():
    """Empty URLs are not included in content dict."""
    state = _make_state("on", {"friendly_name": "Washer"})
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "washer", CONF_URL: "", CONF_SECONDARY_URL: ""}

    content = map_content(state, config)

    assert "url" not in content
    assert "secondary_url" not in content


def test_map_completion_content_preserves_urls():
    """URLs persist through completion content."""
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "washer",
        CONF_URL: "https://ha.local/lovelace/laundry",
        CONF_SECONDARY_URL: "https://ha.local/lovelace/overview",
    }

    content = map_completion_content(config)

    assert content["url"] == "https://ha.local/lovelace/laundry"
    assert content["secondary_url"] == "https://ha.local/lovelace/overview"


# --- Feature 6: conditional icon/color via attributes ---


def test_map_content_icon_attribute():
    """icon_attribute overrides static icon."""
    state = _make_state("heating", {"friendly_name": "HVAC", "sf_symbol": "flame.fill"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "thermometer",
        CONF_ICON_ATTRIBUTE: "sf_symbol",
    }

    content = map_content(state, config)

    assert content["icon"] == "flame.fill"


def test_map_content_icon_attribute_fallback_to_static():
    """icon_attribute missing from entity falls back to static icon."""
    state = _make_state("heating", {"friendly_name": "HVAC"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "thermometer",
        CONF_ICON_ATTRIBUTE: "sf_symbol",
    }

    content = map_content(state, config)

    assert content["icon"] == "thermometer"


def test_map_content_icon_attribute_not_configured():
    """No icon_attribute uses static icon."""
    state = _make_state("on", {"friendly_name": "Lamp"})
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "lightbulb.fill"}

    content = map_content(state, config)

    assert content["icon"] == "lightbulb.fill"


def test_map_content_accent_color_attribute():
    """accent_color_attribute overrides static accent_color."""
    state = _make_state("heating", {"friendly_name": "HVAC", "activity_color": "orange"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "thermometer",
        CONF_ACCENT_COLOR: "red",
        CONF_ACCENT_COLOR_ATTRIBUTE: "activity_color",
    }

    content = map_content(state, config)

    assert content["accent_color"] == "orange"


def test_map_content_accent_color_attribute_rgb_tuple():
    """RGB tuple from HA attribute is converted to hex string."""
    state = _make_state("on", {"friendly_name": "Lamp", "rgb_color": (255, 167, 88)})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
        CONF_ACCENT_COLOR_ATTRIBUTE: "rgb_color",
    }

    content = map_content(state, config)

    assert content["accent_color"] == "#ffa758"


def test_map_content_accent_color_attribute_rgbw_tuple():
    """RGBW 4-tuple takes first 3 channels as RGB."""
    state = _make_state("on", {"friendly_name": "Lamp", "rgbw_color": (100, 200, 50, 128)})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
        CONF_ACCENT_COLOR_ATTRIBUTE: "rgbw_color",
    }

    content = map_content(state, config)

    assert content["accent_color"] == "#64c832"


def test_map_content_accent_color_attribute_xy_tuple():
    """XY color (2-tuple, both <= 1.0) is converted via CIE xy → RGB → hex."""
    state = _make_state("on", {"friendly_name": "Lamp", "xy_color": (0.3, 0.3)})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
        CONF_ACCENT_COLOR_ATTRIBUTE: "xy_color",
    }

    content = map_content(state, config)

    assert content["accent_color"].startswith("#")
    assert len(content["accent_color"]) == 7


def test_map_content_accent_color_attribute_hs_tuple():
    """HS color (hue 0-360, sat 0-100) is converted via HS → RGB → hex."""
    state = _make_state("on", {"friendly_name": "Lamp", "hs_color": (240.0, 100.0)})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
        CONF_ACCENT_COLOR_ATTRIBUTE: "hs_color",
    }

    content = map_content(state, config)

    # Hue 240 = blue, full saturation
    assert content["accent_color"] == "#0000ff"


def test_map_content_accent_color_attribute_kelvin():
    """color_temp_kelvin (int) is converted to approximate RGB hex."""
    state = _make_state("on", {"friendly_name": "Lamp", "color_temp_kelvin": 3000})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
        CONF_ACCENT_COLOR_ATTRIBUTE: "color_temp_kelvin",
    }

    content = map_content(state, config)

    assert content["accent_color"].startswith("#")
    assert len(content["accent_color"]) == 7


def test_map_content_accent_color_attribute_fallback_to_static():
    """accent_color_attribute missing falls back to static accent_color."""
    state = _make_state("heating", {"friendly_name": "HVAC"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "thermometer",
        CONF_ACCENT_COLOR: "red",
        CONF_ACCENT_COLOR_ATTRIBUTE: "activity_color",
    }

    content = map_content(state, config)

    assert content["accent_color"] == "red"


def test_map_content_accent_color_attribute_fallback_to_blue():
    """No static color and no attribute defaults to 'blue'."""
    state = _make_state("on", {"friendly_name": "Lamp"})
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "lightbulb.fill"}

    content = map_content(state, config)

    assert content["accent_color"] == "blue"


# --- Entity native icon auto-detection ---


def test_map_content_entity_icon_used_when_no_static_icon():
    """Entity's native icon attribute is used when no static icon is configured."""
    state = _make_state("on", {"friendly_name": "Thermostat", "icon": "mdi:thermometer"})
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config)

    assert content["icon"] == "mdi:thermometer"


def test_map_content_static_icon_overrides_entity_icon():
    """Static icon takes priority over entity's native icon."""
    state = _make_state("on", {"friendly_name": "Thermostat", "icon": "mdi:thermometer"})
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "flame.fill"}

    content = map_content(state, config)

    assert content["icon"] == "flame.fill"


def test_map_content_icon_attribute_overrides_entity_icon():
    """icon_attribute takes priority over entity's native icon."""
    state = _make_state(
        "on",
        {"friendly_name": "HVAC", "icon": "mdi:thermometer", "custom_icon": "mdi:fire"},
    )
    config = {CONF_TEMPLATE: "generic", CONF_ICON_ATTRIBUTE: "custom_icon"}

    content = map_content(state, config)

    assert content["icon"] == "mdi:fire"


def test_map_content_fallback_icon_when_no_icon_anywhere():
    """Falls back to questionmark.circle when no icon source is available."""
    state = _make_state("on", {"friendly_name": "Unknown Sensor"})
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config)

    assert content["icon"] == "questionmark.circle"


# --- _add_url_deeplinks helper ---


def test_add_url_deeplinks_both_urls():
    """Both URLs are added when present."""
    content: dict = {}
    _add_url_deeplinks(content, {CONF_URL: "https://a.com", CONF_SECONDARY_URL: "https://b.com"})
    assert content["url"] == "https://a.com"
    assert content["secondary_url"] == "https://b.com"


def test_add_url_deeplinks_one_empty():
    """Only non-empty URL is added."""
    content: dict = {}
    _add_url_deeplinks(content, {CONF_URL: "https://a.com", CONF_SECONDARY_URL: ""})
    assert content["url"] == "https://a.com"
    assert "secondary_url" not in content


def test_add_url_deeplinks_both_empty():
    """No URLs added when both are empty."""
    content: dict = {}
    _add_url_deeplinks(content, {CONF_URL: "", CONF_SECONDARY_URL: ""})
    assert "url" not in content
    assert "secondary_url" not in content
