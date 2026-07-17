"""Tests for the PushWard content mapper."""

import time
from unittest.mock import patch

import pytest
import voluptuous as vol

from custom_components.pushward.const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ALARM,
    CONF_BACKGROUND_COLOR,
    CONF_BACKGROUND_COLOR_ATTRIBUTE,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_CURRENT_STEP_ENTITY,
    CONF_DECIMALS,
    CONF_ENTITY_ID,
    CONF_FIRED_AT_ATTRIBUTE,
    CONF_FIRED_AT_ENTITY,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_LABEL,
    CONF_LIVE_PROGRESS,
    CONF_LOG_COLUMNS,
    CONF_LOG_LEVEL_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PRIMARY_SERIES,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ATTR,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SCALE,
    CONF_SECONDARY_URL,
    CONF_SECONDARY_URL_FOREGROUND,
    CONF_SECONDARY_URL_TITLE,
    CONF_SERIES,
    CONF_SERIES_ENTITIES,
    CONF_SEVERITY,
    CONF_SEVERITY_LABEL,
    CONF_SMOOTHING,
    CONF_STATE_LABELS,
    CONF_STEP_COLORS,
    CONF_STEP_LABELS,
    CONF_STEP_ROWS,
    CONF_STEP_WEIGHTS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_SUBTITLE_ENTITY,
    CONF_TAP_ACTION_FOREGROUND,
    CONF_TAP_ACTION_URL,
    CONF_TEMPLATE,
    CONF_TEXT_COLOR,
    CONF_THRESHOLDS,
    CONF_TILES,
    CONF_TOTAL_STEPS,
    CONF_UNIT,
    CONF_UNITS,
    CONF_URL,
    CONF_URL_TITLE,
    CONF_VALUE_ATTRIBUTE,
    CONF_VALUE_ENTITY,
    CONF_WARNING_THRESHOLD,
    MAX_SEVERITY_LABEL_LEN,
    TIMELINE_SERIES_LABEL_MAX,
    normalize_slug,
    validate_slug,
)
from custom_components.pushward.content_mapper import (
    _build_log_line,
    _get_timeline_units,
    _get_timeline_values,
    _timeline_recorder_sources,
    build_tap_action,
    get_domain_defaults,
    map_completion_content,
    map_content,
    sanitize_slug,
)

from .conftest import make_entity_config
from .conftest import make_mock_state as _make_state
from .server_contract import assert_valid_activity_content

# --- sanitize_slug ---


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        ("sensor.washing_machine_status", "ha-sensor-washing_machine_status"),
        ("binary_sensor.front_door", "ha-binary_sensor-front_door"),
        ("switch.living_room_light", "ha-switch-living_room_light"),
        ("climate.hvac", "ha-climate-hvac"),
        ("vacuum.roborock", "ha-vacuum-roborock"),
        ("timer.tea", "ha-timer-tea"),
    ],
)
def test_sanitize_slug(entity_id: str, expected: str):
    assert sanitize_slug(entity_id) == expected


# --- normalize_slug ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("my-slug", "my-slug"),
        ("My Custom Slug", "my-custom-slug"),
        ("sensor.washer_status", "sensor-washer_status"),
        ("UPPER_CASE.dots", "upper_case-dots"),
        ("a--b", "a-b"),
        ("---leading-trailing---", "leading-trailing"),
        ("special!@#chars$%^", "specialchars"),
        ("", ""),
        ("!!!", ""),
        # Leading hyphens/underscores stripped so first char is alphanumeric
        # (server pattern requires ^[a-zA-Z0-9]).
        ("_foo", "foo"),
        ("__bar__", "bar"),
        ("_-mix-_", "mix"),
    ],
)
def test_normalize_slug(raw: str, expected: str):
    assert normalize_slug(raw) == expected


def test_normalize_slug_truncates_to_server_max():
    """Server caps slugs at 128 chars; normalize_slug must not exceed that."""
    raw = "a" * 200
    assert len(normalize_slug(raw)) == 128
    # And the result must still validate against the server pattern.
    validate_slug(normalize_slug(raw))


# --- validate_slug ---


@pytest.mark.parametrize(
    "slug",
    [
        "ha-washer",
        "my-activity-slug",
        "a",
        "abc123",
        "a1b2c3",
    ],
)
def test_validate_slug_accepts_valid(slug: str):
    assert validate_slug(slug) == slug


@pytest.mark.parametrize(
    "slug",
    [
        "../admin",
        "../../other-path",
        "has spaces",
        "",
        "-leading-hyphen",
        "special!chars",
        "a/b",
    ],
)
def test_validate_slug_rejects_invalid(slug: str):
    with pytest.raises(vol.Invalid):
        validate_slug(slug)


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

    assert content["progress"] == round(26 / 255, 2)


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


# --- generic live_progress ---


def test_map_content_generic_live_progress_on():
    state = _make_state("on", {"friendly_name": "Dishwasher", "remaining": 3600}, entity_id="switch.dishwasher")
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "washer",
        CONF_REMAINING_TIME_ATTR: "remaining",
        CONF_LIVE_PROGRESS: True,
    }

    now = int(time.time())
    content = map_content(state, config)

    assert content["live_progress"] is True
    assert content["remaining_time"] == 3600
    # end_date = the mapper's own clock read + remaining, so it lands within a
    # second or two of now + 3600.
    assert abs(content["end_date"] - (now + 3600)) <= 2
    assert_valid_activity_content(content)


def test_map_content_generic_live_progress_off_omits_fields():
    state = _make_state("on", {"friendly_name": "Dishwasher", "remaining": 3600}, entity_id="switch.dishwasher")
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "washer",
        CONF_REMAINING_TIME_ATTR: "remaining",
        CONF_LIVE_PROGRESS: False,
    }

    content = map_content(state, config)

    assert "live_progress" not in content
    assert "end_date" not in content
    assert content["remaining_time"] == 3600
    assert_valid_activity_content(content)


def test_map_content_generic_live_progress_no_remaining_source_clears_it():
    state = _make_state("on", {"friendly_name": "Dishwasher"}, entity_id="switch.dishwasher")
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "washer", CONF_LIVE_PROGRESS: True}

    content = map_content(state, config)

    # No derivable end. Updates are merge-patches, so omitting the key would leave a
    # previously-armed animation running against a stale end_date; clear it instead.
    assert content["live_progress"] is False
    assert "end_date" not in content
    assert_valid_activity_content(content)


# --- steps live_progress ---


def _steps_live_config() -> dict:
    return {
        CONF_TEMPLATE: "steps",
        CONF_ICON: "washer",
        CONF_TOTAL_STEPS: 4,
        CONF_CURRENT_STEP_ATTR: "step",
        CONF_REMAINING_TIME_ATTR: "remaining",
        CONF_LIVE_PROGRESS: True,
    }


def _steps_state(step: int, remaining: int = 5400):
    return _make_state(
        "on",
        {"friendly_name": "Dishwasher", "step": step, "remaining": remaining},
        entity_id="switch.dishwasher",
    )


def test_map_content_steps_live_progress_on():
    now = int(time.time())
    content = map_content(_steps_state(2), _steps_live_config())

    assert content["live_progress"] is True
    assert content["current_step"] == 2
    # The step just started, so its window opens now and closes at now + remaining.
    assert abs(content["start_date"] - now) <= 2
    assert abs(content["end_date"] - (now + 5400)) <= 2
    assert_valid_activity_content(content)


def test_map_content_steps_live_progress_carries_start_date_within_a_step():
    """The bar must not snap back to empty on every push."""
    now = int(time.time())
    last = {"current_step": 2, "start_date": now - 1200, "end_date": now + 4200}

    content = map_content(_steps_state(2, remaining=4200), _steps_live_config(), last_content=last)

    assert content["start_date"] == now - 1200
    assert abs(content["end_date"] - (now + 4200)) <= 2
    assert_valid_activity_content(content)


def test_map_content_steps_live_progress_restamps_start_on_step_advance():
    now = int(time.time())
    last = {"current_step": 2, "start_date": now - 1200, "end_date": now + 4200}

    content = map_content(_steps_state(3, remaining=900), _steps_live_config(), last_content=last)

    assert content["current_step"] == 3
    assert abs(content["start_date"] - now) <= 2
    assert abs(content["end_date"] - (now + 900)) <= 2
    assert_valid_activity_content(content)


def test_map_content_steps_live_progress_off_omits_fields():
    config = _steps_live_config() | {CONF_LIVE_PROGRESS: False}

    content = map_content(_steps_state(2), config)

    assert "live_progress" not in content
    assert "start_date" not in content
    assert "end_date" not in content
    assert_valid_activity_content(content)


def test_map_content_steps_live_progress_no_remaining_source_clears_it():
    config = _steps_live_config()
    del config[CONF_REMAINING_TIME_ATTR]
    state = _make_state("on", {"friendly_name": "Dishwasher", "step": 2}, entity_id="switch.dishwasher")

    content = map_content(state, config)

    assert content["live_progress"] is False
    assert "end_date" not in content
    assert_valid_activity_content(content)


def test_map_content_steps_live_progress_past_end_clears_it():
    """A stalled step must stop animating, not keep filling toward a passed deadline.

    Merge-patch means an omitted key preserves the server's prior live_progress=true.
    """
    content = map_content(_steps_state(2, remaining=0), _steps_live_config())

    assert content["live_progress"] is False
    assert "end_date" not in content
    assert_valid_activity_content(content)


def test_map_content_steps_live_progress_pins_end_date_when_remaining_unchanged():
    """end_date = now + remaining drifts with the wall clock even when the deadline hasn't moved.

    The server treats an end_date change as structural: a high-priority push that skips
    coalescing. Pin the shipped deadline until the source genuinely re-estimates.
    """
    now = int(time.time())
    last = {
        "current_step": 2,
        "remaining_time": 5400,
        "start_date": now - 1200,
        "end_date": now - 1200 + 5400,
    }

    content = map_content(_steps_state(2, remaining=5400), _steps_live_config(), last_content=last)

    assert content["end_date"] == last["end_date"]
    assert content["start_date"] == last["start_date"]
    assert_valid_activity_content(content)


def test_map_content_steps_live_progress_moves_end_date_when_remaining_re_estimates():
    now = int(time.time())
    last = {"current_step": 2, "remaining_time": 5400, "start_date": now - 1200, "end_date": now + 4200}

    content = map_content(_steps_state(2, remaining=600), _steps_live_config(), last_content=last)

    assert abs(content["end_date"] - (now + 600)) <= 2
    assert content["start_date"] == last["start_date"]
    assert_valid_activity_content(content)


def test_map_content_steps_live_progress_resets_inverted_carried_start():
    """A backward clock step (NTP, restored snapshot) can invert the carried window.

    remaining_time differs from the new remaining, so the end_date pin does not
    apply and the recomputed end lands before the carried start.
    """
    now = int(time.time())
    last = {"current_step": 2, "remaining_time": 4200, "start_date": now + 600, "end_date": now + 4200}

    content = map_content(_steps_state(2, remaining=60), _steps_live_config(), last_content=last)

    assert abs(content["start_date"] - now) <= 2
    assert content["start_date"] < content["end_date"]
    assert_valid_activity_content(content)


def test_map_completion_content_steps_clears_live_progress():
    now = int(time.time())
    last = {"live_progress": True, "current_step": 4, "start_date": now - 60, "end_date": now + 60}

    content = map_completion_content(_steps_live_config(), last)

    # Merge-patch would otherwise leave the last step's ETA counting on the end card.
    assert content["live_progress"] is False
    assert content["current_step"] == 4
    # The flag is enough; re-sending the window would be noise.
    assert "start_date" not in content
    assert "end_date" not in content
    assert_valid_activity_content(content)


def test_map_completion_content_steps_clears_live_progress_when_last_frame_lost_the_key():
    """The last frame is not a reliable record of what the server still has armed.

    A frame whose remaining-time source vanished omits live_progress, so gating the
    clear on last_content would skip it and the completion card would keep counting.
    """
    content = map_completion_content(_steps_live_config(), {"current_step": 3, "progress": 0.75})

    assert content["live_progress"] is False
    assert_valid_activity_content(content)


def test_map_completion_content_steps_without_opt_in_stays_absent():
    config = _steps_live_config() | {CONF_LIVE_PROGRESS: False}

    content = map_completion_content(config, {"current_step": 3, "progress": 0.75})

    assert "live_progress" not in content
    assert_valid_activity_content(content)


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


def test_map_completion_content_generic_stops_live_progress():
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "washer"}
    last = {"template": "generic", "progress": 0.4, "live_progress": True, "end_date": int(time.time()) + 3600}

    content = map_completion_content(config, last_content=last)

    # Completion must switch interpolation off so the done card stops counting down.
    assert content["live_progress"] is False
    assert_valid_activity_content(content)


def test_map_completion_content_generic_without_live_progress_stays_absent():
    config = {CONF_TEMPLATE: "generic", CONF_ICON: "washer"}

    content = map_completion_content(config, last_content={"template": "generic", "progress": 0.5})

    assert "live_progress" not in content


# --- get_domain_defaults ---


def test_get_domain_defaults_known():
    defaults = get_domain_defaults("binary_sensor")
    assert defaults["icon"] == "mdi:toggle-switch-variant"
    assert "on" in defaults["start_states"]
    assert "off" in defaults["end_states"]


def test_get_domain_defaults_climate():
    defaults = get_domain_defaults("climate")
    assert defaults["icon"] == "mdi:thermostat"
    assert "heating" in defaults["start_states"]
    assert "off" in defaults["end_states"]


def test_get_domain_defaults_unknown():
    defaults = get_domain_defaults("nonexistent_domain")
    assert defaults["icon"] == "mdi:eye"
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


# --- steps template ---


def test_map_content_steps_with_step_attribute():
    state = _make_state("running", {"friendly_name": "Build", "step": 3})
    config = {
        CONF_TEMPLATE: "steps",
        CONF_ICON: "hammer",
        CONF_TOTAL_STEPS: 5,
        CONF_CURRENT_STEP_ATTR: "step",
    }

    content = map_content(state, config)

    assert content["total_steps"] == 5
    assert content["current_step"] == 3
    # Auto-derived progress (no explicit progress_attribute)
    assert content["progress"] == pytest.approx(0.6)


def test_map_content_steps_clamps_current_step():
    state = _make_state("running", {"step": 99})
    config = {
        CONF_TEMPLATE: "steps",
        CONF_ICON: "hammer",
        CONF_TOTAL_STEPS: 4,
        CONF_CURRENT_STEP_ATTR: "step",
    }

    content = map_content(state, config)

    assert content["current_step"] == 4  # clamped to total_steps
    assert content["progress"] == 1.0


def test_map_content_steps_no_step_attribute():
    state = _make_state("running", {"friendly_name": "Deploy"})
    config = {
        CONF_TEMPLATE: "steps",
        CONF_ICON: "arrow.triangle.2.circlepath",
        CONF_TOTAL_STEPS: 3,
    }

    content = map_content(state, config)

    assert content["total_steps"] == 3
    assert content["current_step"] == 0
    assert content["progress"] == 0.0


def test_map_content_steps_explicit_progress_attribute():
    """When progress_attribute is set, auto-derive is skipped."""
    state = _make_state("running", {"step": 2, "pct": 80})
    config = {
        CONF_TEMPLATE: "steps",
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


def test_map_completion_content_steps():
    config = {CONF_TEMPLATE: "steps", CONF_ICON: "hammer", CONF_TOTAL_STEPS: 5}

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


def test_map_content_alert_severity_label_override():
    state = _make_state("firing", {"friendly_name": "CPU Alert"})
    config = {
        CONF_TEMPLATE: "alert",
        CONF_ICON: "exclamationmark.triangle",
        CONF_SEVERITY: "critical",
        CONF_SEVERITY_LABEL: "Page 1",
    }

    live = map_content(state, config)
    completion = map_completion_content(config)

    # The custom badge text overrides the severity name on both the live and
    # resolved (completion) cards.
    assert live["severity_label"] == "Page 1"
    assert completion["severity_label"] == "Page 1"


def test_map_content_alert_no_severity_label_by_default():
    state = _make_state("firing", {"friendly_name": "CPU Alert"})
    config = {CONF_TEMPLATE: "alert", CONF_SEVERITY: "info"}

    content = map_content(state, config)

    assert "severity_label" not in content


def test_map_content_alert_severity_label_truncated_to_cap():
    """A pre-cap stored config with an over-long label must not 400 every push."""
    state = _make_state("firing", {"friendly_name": "CPU Alert"})
    config = {
        CONF_TEMPLATE: "alert",
        CONF_SEVERITY: "warning",
        CONF_SEVERITY_LABEL: "x" * (MAX_SEVERITY_LABEL_LEN + 10),
    }

    live = map_content(state, config)
    completion = map_completion_content(config)

    assert live["severity_label"] == "x" * MAX_SEVERITY_LABEL_LEN
    assert completion["severity_label"] == "x" * MAX_SEVERITY_LABEL_LEN


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
    """URLs are emitted as structured url_action / secondary_url_action."""
    state = _make_state("on", {"friendly_name": "Washer"})
    config = {
        CONF_TEMPLATE: "steps",
        CONF_ICON: "washer",
        CONF_TOTAL_STEPS: 3,
        CONF_URL: "https://ha.local/lovelace/laundry",
        CONF_SECONDARY_URL: "https://ha.local/lovelace/overview",
    }

    content = map_content(state, config)

    assert content["url_action"] == {"url": "https://ha.local/lovelace/laundry", "foreground": True}
    assert content["secondary_url_action"] == {"url": "https://ha.local/lovelace/overview", "foreground": True}
    # Legacy string fields are no longer emitted.
    assert "url" not in content
    assert "secondary_url" not in content


def test_map_content_urls_omitted_when_empty():
    """Empty URLs do not emit url_action / secondary_url_action."""
    state = _make_state("on", {"friendly_name": "Washer"})
    config = {
        CONF_TEMPLATE: "steps",
        CONF_ICON: "washer",
        CONF_TOTAL_STEPS: 3,
        CONF_URL: "",
        CONF_SECONDARY_URL: "",
    }

    content = map_content(state, config)

    assert "url_action" not in content
    assert "secondary_url_action" not in content
    assert "url" not in content
    assert "secondary_url" not in content


def test_map_completion_content_preserves_urls():
    """URLs persist through completion content as structured actions."""
    config = {
        CONF_TEMPLATE: "steps",
        CONF_ICON: "washer",
        CONF_TOTAL_STEPS: 3,
        CONF_URL: "https://ha.local/lovelace/laundry",
        CONF_SECONDARY_URL: "https://ha.local/lovelace/overview",
    }

    content = map_completion_content(config)

    assert content["url_action"] == {"url": "https://ha.local/lovelace/laundry", "foreground": True}
    assert content["secondary_url_action"] == {"url": "https://ha.local/lovelace/overview", "foreground": True}


def test_map_content_tap_action_universal():
    """tap_action is emitted for every template, not just steps/alert."""
    state = _make_state("on", {"friendly_name": "Counter"})
    for template in ("generic", "countdown", "alert", "steps", "gauge", "timeline"):
        config = {
            CONF_TEMPLATE: template,
            CONF_TOTAL_STEPS: 3,
            CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
            CONF_TAP_ACTION_FOREGROUND: True,
        }
        content = map_content(state, config)
        assert content["tap_action"] == {
            "url": "homeassistant://navigate/lovelace/0",
            "foreground": True,
        }


def test_map_content_url_action_with_title():
    """url_action carries the user-configured title verbatim."""
    state = _make_state("on", {"friendly_name": "Washer"})
    config = {
        CONF_TEMPLATE: "steps",
        CONF_TOTAL_STEPS: 3,
        CONF_URL: "https://ha.local/lovelace/laundry",
        CONF_URL_TITLE: "Open Dashboard",
    }
    content = map_content(state, config)
    assert content["url_action"] == {
        "url": "https://ha.local/lovelace/laundry",
        "foreground": True,
        "title": "Open Dashboard",
    }


def test_map_content_silent_webhook_auto_method():
    """foreground=False on http(s) URL auto-injects method=POST."""
    state = _make_state("on", {"friendly_name": "Washer"})
    config = {
        CONF_TEMPLATE: "steps",
        CONF_TOTAL_STEPS: 3,
        CONF_SECONDARY_URL: "https://ha.local/api/services/script/foo",
        CONF_SECONDARY_URL_FOREGROUND: False,
        CONF_SECONDARY_URL_TITLE: "Run",
    }
    content = map_content(state, config)
    assert content["secondary_url_action"] == {
        "url": "https://ha.local/api/services/script/foo",
        "foreground": False,
        "method": "POST",
        "title": "Run",
    }


def test_map_content_custom_scheme_no_method_injection():
    """Custom scheme URLs never get method injected, even with foreground=False."""
    state = _make_state("on", {"friendly_name": "Washer"})
    config = {
        CONF_TEMPLATE: "alert",
        CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
        CONF_TAP_ACTION_FOREGROUND: False,
    }
    content = map_content(state, config)
    assert content["tap_action"] == {
        "url": "homeassistant://navigate/lovelace/0",
        "foreground": False,
    }
    assert "method" not in content["tap_action"]


def test_map_content_url_action_skipped_on_non_steps_alert():
    """url_action / secondary_url_action are emitted only for steps/alert."""
    state = _make_state("on", {"friendly_name": "Foo"})
    for template in ("generic", "countdown", "gauge", "timeline"):
        config = {
            CONF_TEMPLATE: template,
            CONF_URL: "https://ha.local",
            CONF_SECONDARY_URL: "https://ha.local/secondary",
        }
        content = map_content(state, config)
        assert "url_action" not in content, template
        assert "secondary_url_action" not in content, template


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


def test_map_content_accent_color_attribute_string_garbage_falls_back():
    """Non-color string from attribute must not be sent verbatim to the server."""
    state = _make_state("on", {"friendly_name": "Lamp", "effect": "breathe"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
        CONF_ACCENT_COLOR_ATTRIBUTE: "effect",
    }

    content = map_content(state, config)

    assert content["accent_color"] == "blue"


def test_map_content_accent_color_attribute_stringified_tuple_falls_back():
    """Attribute stored as a string like '(27.0, 19.2)' must fall back, not leak."""
    state = _make_state("on", {"friendly_name": "Lamp", "raw_color": "(27.001, 19.243)"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
        CONF_ACCENT_COLOR_ATTRIBUTE: "raw_color",
    }

    content = map_content(state, config)

    assert content["accent_color"] == "blue"


def test_map_content_accent_color_attribute_dict_falls_back():
    """Dict-valued attribute falls back instead of emitting str(dict)."""
    state = _make_state("on", {"friendly_name": "Lamp", "color": {"r": 255, "g": 0, "b": 0}})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
        CONF_ACCENT_COLOR_ATTRIBUTE: "color",
    }

    content = map_content(state, config)

    assert content["accent_color"] == "blue"


def test_map_content_accent_color_attribute_hex_string_passthrough():
    """Hex string attribute is passed through unchanged."""
    state = _make_state("on", {"friendly_name": "Lamp", "color_hex": "#a1b2c3"})
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "lightbulb.fill",
        CONF_ACCENT_COLOR_ATTRIBUTE: "color_hex",
    }

    content = map_content(state, config)

    assert content["accent_color"] == "#a1b2c3"


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


def test_map_content_registry_icon_used_when_no_other_icon():
    """Entity registry icon is used when no static/attribute/state icon exists."""
    state = _make_state("on", {"friendly_name": "Thermostat"}, entity_id="climate.thermostat")
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config, registry_icon="mdi:thermometer")

    assert content["icon"] == "mdi:thermometer"


def test_map_content_state_icon_overrides_registry_icon():
    """State attribute icon takes priority over entity registry icon."""
    state = _make_state("on", {"friendly_name": "T", "icon": "mdi:fire"}, entity_id="climate.thermostat")
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config, registry_icon="mdi:thermometer")

    assert content["icon"] == "mdi:fire"


def test_map_content_device_class_icon():
    """Device class icon is resolved from DEVICE_CLASS_ICONS table."""
    state = _make_state(
        "on",
        {"friendly_name": "Temp Sensor", "device_class": "temperature"},
        entity_id="sensor.temp",
    )
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config)

    assert content["icon"] == "mdi:thermometer"


def test_map_content_device_class_icon_binary_sensor():
    """Binary sensor device class resolves to correct MDI icon."""
    state = _make_state(
        "on",
        {"friendly_name": "Front Door", "device_class": "door"},
        entity_id="binary_sensor.front_door",
    )
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config)

    assert content["icon"] == "mdi:door-open"


def test_map_content_registry_icon_overrides_device_class():
    """Registry icon takes priority over device class icon."""
    state = _make_state(
        "on",
        {"friendly_name": "Temp", "device_class": "temperature"},
        entity_id="sensor.temp",
    )
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config, registry_icon="mdi:home-thermometer")

    assert content["icon"] == "mdi:home-thermometer"


def test_map_content_number_domain_falls_back_to_sensor_icons():
    """Number domain shares sensor device-class icons."""
    state = _make_state(
        "on",
        {"friendly_name": "Target Temp", "device_class": "temperature"},
        entity_id="number.target_temp",
    )
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config)

    assert content["icon"] == "mdi:thermometer"


def test_map_content_unknown_device_class_falls_to_domain():
    """Unknown device class with known domain falls back to domain default."""
    state = _make_state(
        "on",
        {"friendly_name": "Sensor", "device_class": "nonexistent"},
        entity_id="sensor.weird",
    )
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config)

    assert content["icon"] == "mdi:eye"


def test_map_content_domain_default_icon_when_no_device_class():
    """Falls back to domain default icon when no device class is set."""
    state = _make_state("on", {"friendly_name": "HVAC"}, entity_id="climate.hvac")
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config)

    assert content["icon"] == "mdi:thermostat"


def test_map_content_fallback_icon_when_no_icon_anywhere():
    """Falls back to mdi:eye for unknown domains with no icon source."""
    state = _make_state("on", {"friendly_name": "Unknown Thing"}, entity_id="custom.thing")
    config = {CONF_TEMPLATE: "generic"}

    content = map_content(state, config)

    assert content["icon"] == "mdi:eye"


# --- build_tap_action helper ---


def test_build_tap_action_empty_url_returns_none():
    """Empty URL produces no action — caller skips the key."""
    assert build_tap_action("", True) is None
    assert build_tap_action("   ", True) is None


def test_build_tap_action_http_foreground():
    """http(s) URL + foreground → bare open action, no method injection."""
    assert build_tap_action("https://example.com", True) == {
        "url": "https://example.com",
        "foreground": True,
    }


def test_build_tap_action_http_silent_injects_method_post():
    """http(s) URL + foreground=False → method=POST so iOS treats it as a webhook."""
    assert build_tap_action("https://example.com/hook", False) == {
        "url": "https://example.com/hook",
        "foreground": False,
        "method": "POST",
    }


def test_build_tap_action_custom_scheme_no_method():
    """Custom scheme + foreground=False → no method injection (iOS just opens the app)."""
    assert build_tap_action("homeassistant://navigate/lovelace/0", False) == {
        "url": "homeassistant://navigate/lovelace/0",
        "foreground": False,
    }


def test_build_tap_action_includes_title_when_set():
    """Title is included verbatim when non-empty; stripped of surrounding whitespace."""
    assert build_tap_action("https://example.com", True, "  Open  ") == {
        "url": "https://example.com",
        "foreground": True,
        "title": "Open",
    }


def test_build_tap_action_empty_title_skipped():
    """Empty title is omitted entirely from the action dict."""
    action = build_tap_action("https://example.com", True, "")
    assert "title" not in action


# --- gauge template ---


def test_map_content_gauge_with_value_attribute():
    """Gauge template reads value from entity attribute."""
    state = _make_state("22.5", {"friendly_name": "Thermometer", "temperature": 22.5})
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_ICON: "thermometer",
        CONF_VALUE_ATTRIBUTE: "temperature",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 50.0,
        CONF_UNIT: "\u00b0C",
    }

    content = map_content(state, config)

    assert content["value"] == 22.5
    assert content["min_value"] == 0.0
    assert content["max_value"] == 50.0
    assert content["unit"] == "\u00b0C"
    assert content["progress"] == pytest.approx(0.45)


def test_map_content_gauge_brightness_rescaled():
    """Brightness (0-255) is rescaled to 0-100 for gauge."""
    state = _make_state("on", {"friendly_name": "Lamp", "brightness": 138})
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_ICON: "lightbulb.fill",
        CONF_VALUE_ATTRIBUTE: "brightness",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
    }

    content = map_content(state, config)

    # round(138/255 * 100) = 54
    assert content["value"] == 54
    assert content["progress"] == 0.54


def test_map_content_gauge_reads_state_when_no_attribute():
    """Gauge template falls back to entity state as float value."""
    state = _make_state("75.0", {"friendly_name": "Battery"})
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_ICON: "battery.50",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
    }

    content = map_content(state, config)

    assert content["value"] == 75.0
    assert content["progress"] == 0.75


def test_map_content_gauge_clamps_value():
    """Gauge value is clamped to min/max range."""
    state = _make_state("150", {"friendly_name": "Sensor"})
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_ICON: "gauge",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
    }

    content = map_content(state, config)

    assert content["value"] == 100.0
    assert content["progress"] == 1.0


def test_map_content_gauge_unit_omitted_when_empty():
    """Unit field is not included in content when empty."""
    state = _make_state("50", {"friendly_name": "Sensor"})
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_ICON: "gauge",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
        CONF_UNIT: "",
    }

    content = map_content(state, config)

    assert "unit" not in content


def test_map_content_gauge_negative_range():
    """Gauge with range crossing zero works correctly."""
    state = _make_state("-5", {"friendly_name": "Outdoor Temp"})
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_ICON: "thermometer",
        CONF_MIN_VALUE: -20.0,
        CONF_MAX_VALUE: 40.0,
    }

    content = map_content(state, config)

    assert content["value"] == -5.0
    assert content["progress"] == pytest.approx(0.25)  # (-5 - -20) / (40 - -20) = 15/60


def test_map_content_gauge_equal_min_max():
    """Degenerate gauge (min == max) doesn't crash and sets progress 1.0."""
    state = _make_state("50", {"friendly_name": "Sensor"})
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_ICON: "gauge",
        CONF_MIN_VALUE: 50.0,
        CONF_MAX_VALUE: 50.0,
    }

    content = map_content(state, config)

    assert content["value"] == 50.0
    assert content["progress"] == 1.0


def test_map_completion_content_gauge():
    """Gauge completion content sets value to max."""
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_ICON: "gauge",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
        CONF_UNIT: "%",
    }

    content = map_completion_content(config)

    assert content["value"] == 100.0
    assert content["min_value"] == 0.0
    assert content["max_value"] == 100.0
    assert content["unit"] == "%"
    assert content["progress"] == 1.0


def test_map_completion_content_gauge_preserves_last_value():
    """Gauge completion preserves the final live value (e.g. blinds at 50%)."""
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_ICON: "gauge",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
        CONF_UNIT: "%",
    }
    last = {"value": 50.0, "progress": 0.5, "subtitle": "Half"}

    content = map_completion_content(config, last_content=last)

    assert content["value"] == 50.0
    assert content["progress"] == 0.5
    assert content["subtitle"] == "Half"
    assert content["max_value"] == 100.0
    assert content["unit"] == "%"


# --- timeline template ---


def test_map_content_timeline_single_series_from_state():
    """Timeline with no series config uses entity state as single series."""
    state = _make_state("22.5", {"friendly_name": "Living Room Temp"})
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:thermometer",
        CONF_UNIT: "\u00b0C",
    }

    content = map_content(state, config)

    assert content["template"] == "timeline"
    assert content["value"] == {"Living Room Temp": 22.5}
    assert content["unit"] == "\u00b0C"
    assert content["progress"] == 0.0


def test_map_content_timeline_single_series_from_attribute():
    """Timeline with value_attribute reads from attribute."""
    state = _make_state("on", {"friendly_name": "Thermostat", "current_temperature": 21.0})
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:thermostat",
        CONF_VALUE_ATTRIBUTE: "current_temperature",
    }

    content = map_content(state, config)

    assert content["value"] == {"Thermostat": 21.0}


def test_map_content_timeline_multi_series():
    """Timeline with series config maps multiple attributes to labeled series."""
    state = _make_state(
        "heating",
        {
            "friendly_name": "HVAC",
            "current_temperature": 20.5,
            "target_temperature": 22.0,
        },
    )
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:thermostat",
        CONF_SERIES: {"current_temperature": "Current", "target_temperature": "Target"},
        CONF_UNIT: "\u00b0C",
    }

    content = map_content(state, config)

    assert content["value"] == {"Current": 20.5, "Target": 22.0}
    assert content["unit"] == "\u00b0C"


def test_map_content_timeline_series_map_label_clamped():
    """An over-length series-map label is clamped to the cap as a value-map key.

    Guards a stored config that predates the config-flow cap: without the clamp the value
    key would exceed TIMELINE_SERIES_LABEL_MAX and the server would 400 the whole push.
    """
    long_label = "z" * (TIMELINE_SERIES_LABEL_MAX + 8)
    state = _make_state("on", {"friendly_name": "Sensor", "temperature": 42.0})
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES: {"temperature": long_label},
    }

    content = map_content(state, config)

    assert list(content["value"]) == [long_label[:TIMELINE_SERIES_LABEL_MAX]]
    assert all(len(key) <= TIMELINE_SERIES_LABEL_MAX for key in content["value"])
    assert_valid_activity_content(content, where="timeline")


def test_map_content_timeline_primary_series_single_fallback():
    """Single-series timelines mark the tracked entity's series as primary."""
    state = _make_state("22.5", {"friendly_name": "Living Room Temp"})
    config = {CONF_TEMPLATE: "timeline", CONF_ICON: "mdi:thermometer"}

    content = map_content(state, config)

    assert content["primary_series"] == "Living Room Temp"


def test_map_content_timeline_primary_series_first_attribute_series():
    """With attribute series configured, the first label is primary."""
    state = _make_state(
        "heating",
        {"friendly_name": "HVAC", "current_temperature": 20.5, "target_temperature": 22.0},
    )
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:thermostat",
        CONF_SERIES: {"current_temperature": "Current", "target_temperature": "Target"},
    }

    content = map_content(state, config)

    assert content["primary_series"] == "Current"


def test_map_content_timeline_primary_series_explicit_override():
    """An explicit primary series config wins over the auto pick."""
    state = _make_state(
        "heating",
        {"friendly_name": "HVAC", "current_temperature": 20.5, "target_temperature": 22.0},
    )
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:thermostat",
        CONF_SERIES: {"current_temperature": "Current", "target_temperature": "Target"},
        CONF_PRIMARY_SERIES: "Target",
    }

    content = map_content(state, config)

    assert content["primary_series"] == "Target"


def test_map_content_timeline_primary_series_first_series_entity():
    """With only series entities configured, the first label is primary."""
    anchor = _make_state("42", {"friendly_name": "Air Monitor"})
    aqi = _make_state("42", {}, "sensor.aqi")
    temp = _make_state("21.5", {}, "sensor.temp")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:air-filter",
        CONF_SERIES_ENTITIES: [
            {CONF_LABEL: "Air Quality", CONF_ENTITY_ID: "sensor.aqi"},
            {CONF_LABEL: "Temperature", CONF_ENTITY_ID: "sensor.temp"},
        ],
    }

    content = map_content(
        anchor,
        config,
        hass=_FakeHass({"sensor.aqi": aqi, "sensor.temp": temp}),
    )

    assert content["primary_series"] == "Air Quality"


def test_map_content_timeline_primary_series_long_label_omitted():
    """Labels over the server's 32-char key cap are not sent as primary."""
    long_name = "x" * 33
    state = _make_state("22.5", {"friendly_name": long_name})
    config = {CONF_TEMPLATE: "timeline", CONF_ICON: "mdi:thermometer"}

    content = map_content(state, config)

    assert "primary_series" not in content


def test_map_content_timeline_multi_series_partial():
    """Multi-series skips missing attributes gracefully."""
    state = _make_state("heating", {"friendly_name": "HVAC", "current_temperature": 20.5})
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:thermostat",
        CONF_SERIES: {"current_temperature": "Current", "missing_attr": "Missing"},
    }

    content = map_content(state, config)

    assert content["value"] == {"Current": 20.5}


def test_map_content_timeline_brightness_single_series():
    """Brightness (0-255) is rescaled to 0-100 for single-series timeline."""
    state = _make_state("on", {"friendly_name": "Lamp", "brightness": 138})
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:lightbulb",
        CONF_VALUE_ATTRIBUTE: "brightness",
    }

    content = map_content(state, config)

    # round(138/255 * 100) = 54
    assert content["value"] == {"Lamp": 54}


def test_map_content_timeline_brightness_multi_series():
    """Brightness (0-255) is rescaled to 0-100 in multi-series timeline."""
    state = _make_state(
        "on",
        {
            "friendly_name": "Lamp",
            "brightness": 255,
            "color_temp": 350,
        },
    )
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:lightbulb",
        CONF_SERIES: {"brightness": "Brightness", "color_temp": "Color Temp"},
    }

    content = map_content(state, config)

    # brightness 255 → 100, color_temp unchanged (not in _ATTRS_0_255)
    assert content["value"] == {"Brightness": 100, "Color Temp": 350}


def test_map_content_timeline_non_numeric_state():
    """Timeline with non-numeric state produces no value."""
    state = _make_state("heating", {"friendly_name": "HVAC"})
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:thermostat",
    }

    content = map_content(state, config)

    assert "value" not in content
    assert content["progress"] == 0.0


def test_map_content_timeline_scale_logarithmic():
    """Logarithmic scale is included in content."""
    state = _make_state("42.0", {"friendly_name": "Sensor"})
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:chart-line",
        CONF_SCALE: "logarithmic",
    }

    content = map_content(state, config)

    assert content["scale"] == "logarithmic"


def test_map_content_timeline_scale_linear_omitted():
    """Linear scale (default) is omitted from content to save payload."""
    state = _make_state("42.0", {"friendly_name": "Sensor"})
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:chart-line",
        CONF_SCALE: "linear",
    }

    content = map_content(state, config)

    assert "scale" not in content


def test_map_content_timeline_defaults_omitted():
    """Default decimals (1) and smoothing (False) are omitted from payload."""
    state = _make_state("42.0", {"friendly_name": "Sensor"})
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:chart-line",
        CONF_DECIMALS: 1,
        CONF_SMOOTHING: False,
    }

    content = map_content(state, config)

    assert "decimals" not in content
    assert "smoothing" not in content


def test_map_content_timeline_all_options():
    """Timeline includes all optional fields when configured."""
    state = _make_state("22.5", {"friendly_name": "Room Temp"})
    thresholds = [{"value": 25.0, "color": "red", "label": "Hot"}]
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:thermometer",
        CONF_UNIT: "\u00b0C",
        CONF_SCALE: "logarithmic",
        CONF_DECIMALS: 2,
        CONF_SMOOTHING: True,
        CONF_THRESHOLDS: thresholds,
    }

    content = map_content(state, config)

    assert content["unit"] == "\u00b0C"
    assert content["scale"] == "logarithmic"
    assert content["decimals"] == 2
    assert content["smoothing"] is True
    assert content["thresholds"] == thresholds


def test_map_content_timeline_unit_omitted_when_empty():
    """Unit field is not included when empty."""
    state = _make_state("42.0", {"friendly_name": "Sensor"})
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_ICON: "mdi:chart-line",
        CONF_UNIT: "",
    }

    content = map_content(state, config)

    assert "unit" not in content


def test_map_completion_content_timeline():
    """Timeline completion preserves last values and display settings."""
    last_content = {
        "value": {"Temp": 22.5},
        "unit": "\u00b0C",
        "scale": "logarithmic",
        "decimals": 2,
        "smoothing": True,
        "thresholds": [{"value": 25.0, "color": "red"}],
    }
    config = {CONF_TEMPLATE: "timeline", CONF_ICON: "mdi:thermometer"}

    content = map_completion_content(config, last_content=last_content)

    assert content["value"] == {"Temp": 22.5}
    assert content["unit"] == "\u00b0C"
    assert content["scale"] == "logarithmic"
    assert content["decimals"] == 2
    assert content["smoothing"] is True
    assert content["thresholds"] == [{"value": 25.0, "color": "red"}]
    assert content["state"] == "Complete"
    assert content["icon"] == "checkmark.circle.fill"


def test_map_completion_content_timeline_no_last_content():
    """Timeline completion without last content doesn't crash."""
    config = {CONF_TEMPLATE: "timeline", CONF_ICON: "mdi:thermometer"}

    content = map_completion_content(config)

    assert "value" not in content
    assert content["state"] == "Complete"


# --- New field tests ---

# Countdown: warning_threshold


@patch("custom_components.pushward.content_mapper.time")
def test_countdown_emits_warning_threshold(mock_time):
    mock_time.time.return_value = 1000.0
    state = _make_state("active", {"remaining": 120})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "countdown",
            CONF_REMAINING_TIME_ATTR: "remaining",
            CONF_WARNING_THRESHOLD: 30,
        }
    )

    content = map_content(state, config)

    assert content["warning_threshold"] == 30


@patch("custom_components.pushward.content_mapper.time")
def test_countdown_omits_warning_threshold_when_not_configured(mock_time):
    mock_time.time.return_value = 1000.0
    state = _make_state("active", {"remaining": 120})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "countdown",
            CONF_REMAINING_TIME_ATTR: "remaining",
            CONF_WARNING_THRESHOLD: None,
        }
    )

    content = map_content(state, config)

    assert "warning_threshold" not in content


# Countdown: alarm


@patch("custom_components.pushward.content_mapper.time")
def test_countdown_emits_alarm_true_when_configured(mock_time):
    mock_time.time.return_value = 1000.0
    state = _make_state("active", {"remaining": 60})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "countdown",
            CONF_REMAINING_TIME_ATTR: "remaining",
            CONF_ALARM: True,
        }
    )

    content = map_content(state, config)

    assert content["alarm"] is True


@patch("custom_components.pushward.content_mapper.time")
def test_countdown_omits_alarm_when_false(mock_time):
    mock_time.time.return_value = 1000.0
    state = _make_state("active", {"remaining": 60})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "countdown",
            CONF_REMAINING_TIME_ATTR: "remaining",
            CONF_ALARM: False,
        }
    )

    content = map_content(state, config)

    assert "alarm" not in content


# Countdown: start_date


@patch("custom_components.pushward.content_mapper.time")
def test_countdown_emits_start_date_when_remaining_present(mock_time):
    mock_time.time.return_value = 5000.0
    state = _make_state("active", {"remaining": 120})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "countdown",
            CONF_REMAINING_TIME_ATTR: "remaining",
        }
    )

    content = map_content(state, config)

    assert content["start_date"] == 5000


@patch("custom_components.pushward.content_mapper.time")
def test_countdown_omits_start_date_when_no_remaining_time_attr(mock_time):
    mock_time.time.return_value = 5000.0
    state = _make_state("active", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "countdown",
            CONF_REMAINING_TIME_ATTR: "",
        }
    )

    content = map_content(state, config)

    assert "start_date" not in content


# Steps: step_labels


def test_steps_emits_step_labels_as_ordered_list():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_LABELS: {"1": "Init", "2": "Build", "3": "Deploy"},
        }
    )

    content = map_content(state, config)

    assert content["step_labels"] == ["Init", "Build", "Deploy"]


def test_steps_step_labels_fills_missing_indices_with_empty_string():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_LABELS: {"1": "Init", "3": "Deploy"},
        }
    )

    content = map_content(state, config)

    assert content["step_labels"] == ["Init", "", "Deploy"]


def test_steps_omits_step_labels_when_all_empty():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_LABELS: {},
        }
    )

    content = map_content(state, config)

    assert "step_labels" not in content


def test_steps_omits_step_labels_when_all_values_empty():
    """A non-empty dict can still yield only empty slots (blank values, out-of-range keys)."""
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_LABELS: {"1": "", "2": "", "9": "Ghost"},
        }
    )

    content = map_content(state, config)

    assert "step_labels" not in content


# Steps: step_rows


def test_steps_emits_step_rows_matching_total():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_ROWS: [1, 2, 3],
        }
    )

    content = map_content(state, config)

    assert content["step_rows"] == [1, 2, 3]


def test_steps_step_rows_clamps_to_one_ten():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_ROWS: [0, 5, 99],
        }
    )

    content = map_content(state, config)

    assert content["step_rows"] == [1, 5, 10]


def test_steps_step_rows_omitted_when_length_mismatch():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_ROWS: [1, 2],
        }
    )

    content = map_content(state, config)

    assert "step_rows" not in content


# Steps: step_weights


def test_steps_emits_step_weights_matching_total():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_WEIGHTS: [1, 2.5, 1],
        }
    )

    content = map_content(state, config)

    assert content["step_weights"] == [1.0, 2.5, 1.0]


def test_steps_step_weights_omitted_when_length_mismatch():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_WEIGHTS: [1, 2],
        }
    )

    content = map_content(state, config)

    assert "step_weights" not in content


def test_steps_step_weights_omitted_when_entry_not_positive():
    """The server rejects a non-positive weight, so drop the array instead of 400ing."""
    for weights in ([1, 0, 2], [1, -3, 2], [1, float("inf"), 2], [1, float("nan"), 2], [1, "x", 2], [True, True, True]):
        state = _make_state("running", {})
        config = make_entity_config(
            **{
                CONF_TEMPLATE: "steps",
                CONF_TOTAL_STEPS: 3,
                CONF_STEP_WEIGHTS: weights,
            }
        )

        content = map_content(state, config)

        assert "step_weights" not in content, weights


# Steps: step_colors


def test_steps_emits_step_colors_matching_total():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_COLORS: ["green", "#ff0000", "blue"],
        }
    )

    content = map_content(state, config)

    assert content["step_colors"] == ["green", "#ff0000", "blue"]


def test_steps_step_colors_keeps_empty_entry():
    """An empty entry is legal and means "use accent_color", so it must hold its slot."""
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_COLORS: ["green", "", "red"],
        }
    )

    content = map_content(state, config)

    assert content["step_colors"] == ["green", "", "red"]


def test_steps_step_colors_normalizes_invalid_entry_to_accent():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_COLORS: ["green", "chartreuse", "red"],
        }
    )

    content = map_content(state, config)

    assert content["step_colors"] == ["green", "", "red"]


def test_steps_omits_step_colors_when_all_resolve_empty():
    """All-empty colors say nothing the accent doesn't; the list must be omitted."""
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_COLORS: ["", "chartreuse", ""],
        }
    )

    content = map_content(state, config)

    assert "step_colors" not in content


def test_steps_step_colors_omitted_when_length_mismatch():
    state = _make_state("running", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "steps",
            CONF_TOTAL_STEPS: 3,
            CONF_STEP_COLORS: ["green", "red"],
        }
    )

    content = map_content(state, config)

    assert "step_colors" not in content


# Alert: fired_at


def test_alert_emits_fired_at_from_attribute():
    state = _make_state("firing", {"triggered_at": 1700000000})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "alert",
            CONF_FIRED_AT_ATTRIBUTE: "triggered_at",
        }
    )

    content = map_content(state, config)

    assert content["fired_at"] == 1700000000


def test_alert_omits_fired_at_when_attribute_missing():
    state = _make_state("firing", {})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "alert",
            CONF_FIRED_AT_ATTRIBUTE: "missing_attr",
        }
    )

    content = map_content(state, config)

    assert "fired_at" not in content


def test_alert_omits_fired_at_when_unparseable():
    state = _make_state("firing", {"triggered_at": "not-a-number"})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "alert",
            CONF_FIRED_AT_ATTRIBUTE: "triggered_at",
        }
    )

    content = map_content(state, config)

    assert "fired_at" not in content


def test_alert_fired_at_coerces_float_to_int():
    state = _make_state("firing", {"triggered_at": 1700000000.5})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "alert",
            CONF_FIRED_AT_ATTRIBUTE: "triggered_at",
        }
    )

    content = map_content(state, config)

    assert content["fired_at"] == 1700000000


# Timeline: units


def test_timeline_emits_units_dict():
    state = _make_state(
        "22.5",
        {"friendly_name": "Thermostat", "current_temperature": 22.5, "target_temperature": 21.0},
    )
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "timeline",
            CONF_SERIES: {"current_temperature": "Current", "target_temperature": "Target"},
            CONF_UNITS: {"Current": "°C", "Target": "°C"},
        }
    )

    content = map_content(state, config)

    assert content["units"] == {"Current": "°C", "Target": "°C"}


def test_timeline_omits_units_when_empty():
    state = _make_state("22.5", {"friendly_name": "Sensor"})
    config = make_entity_config(
        **{
            CONF_TEMPLATE: "timeline",
            CONF_UNITS: {},
        }
    )

    content = map_content(state, config)

    assert "units" not in content


def test_timeline_completion_carries_units():
    last_content = {
        "value": {"Temp": 22.5},
        "units": {"Temp": "°C"},
        "template": "timeline",
    }
    config = make_entity_config(**{CONF_TEMPLATE: "timeline"})

    content = map_completion_content(config, last_content=last_content)

    assert content["units"] == {"Temp": "°C"}


# Common colors: background_color and text_color


def test_common_background_color_static():
    state = _make_state("on", {"friendly_name": "Lamp"})
    config = make_entity_config(**{CONF_BACKGROUND_COLOR: "#1a2b3c"})

    content = map_content(state, config)

    assert content["background_color"] == "#1a2b3c"


def test_common_background_color_from_attribute():
    state = _make_state("on", {"friendly_name": "Lamp", "bg": "#ff0000"})
    config = make_entity_config(**{CONF_BACKGROUND_COLOR_ATTRIBUTE: "bg"})

    content = map_content(state, config)

    assert content["background_color"] == "#ff0000"


def test_common_text_color_static():
    state = _make_state("on", {"friendly_name": "Lamp"})
    config = make_entity_config(**{CONF_TEXT_COLOR: "#ffffff"})

    content = map_content(state, config)

    assert content["text_color"] == "#ffffff"


def test_common_omits_colors_when_not_set():
    state = _make_state("on", {"friendly_name": "Lamp"})
    config = make_entity_config(**{CONF_BACKGROUND_COLOR: "", CONF_TEXT_COLOR: ""})

    content = map_content(state, config)

    assert "background_color" not in content
    assert "text_color" not in content


@pytest.mark.parametrize("template", ["generic", "countdown"])
def test_completion_carries_background_and_text_color_all_templates(template):
    last_content = {
        "template": template,
        "background_color": "#fff",
        "text_color": "red",
        "progress": 0.5,
        "subtitle": "test",
    }
    config = make_entity_config(**{CONF_TEMPLATE: template})

    content = map_completion_content(config, last_content=last_content)

    assert content["background_color"] == "#fff"
    assert content["text_color"] == "red"


# --- Companion source entities ---------------------------------------------


class _FakeStates:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


class _FakeHass:
    """Minimal hass stub exposing only states.get for companion resolution."""

    def __init__(self, mapping):
        self.states = _FakeStates(mapping)


def test_companion_remaining_time_from_state():
    """A separate sensor's state supplies remaining seconds (LG washer pattern)."""
    primary = _make_state("on", {"friendly_name": "Pralka"}, "sensor.pralka_stan")
    time_sensor = _make_state("1500", {}, "sensor.pralka_pozostaly_czas")
    config = {
        CONF_TEMPLATE: "generic",
        CONF_ICON: "washer",
        CONF_REMAINING_TIME_ENTITY: "sensor.pralka_pozostaly_czas",
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.pralka_pozostaly_czas": time_sensor}))

    assert content["remaining_time"] == 1500


def test_companion_remaining_time_clock_string():
    """An 'H:MM:SS' duration string is parsed to seconds."""
    primary = _make_state("on", {}, "sensor.pralka_stan")
    time_sensor = _make_state("0:25:00", {}, "sensor.pralka_pozostaly_czas")
    config = {CONF_TEMPLATE: "countdown", CONF_REMAINING_TIME_ENTITY: "sensor.pralka_pozostaly_czas"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.pralka_pozostaly_czas": time_sensor}))

    assert content["remaining_time"] == 25 * 60


def test_companion_remaining_time_duration_unit_minutes():
    """A duration sensor in minutes is converted to seconds."""
    primary = _make_state("on", {}, "sensor.washer")
    time_sensor = _make_state("25", {"device_class": "duration", "unit_of_measurement": "min"}, "sensor.washer_time")
    config = {CONF_TEMPLATE: "generic", CONF_REMAINING_TIME_ENTITY: "sensor.washer_time"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_time": time_sensor}))

    assert content["remaining_time"] == 25 * 60


@patch("custom_components.pushward.content_mapper.time")
def test_companion_remaining_time_timestamp_anchors_end_date(mock_time):
    """A timestamp finish-time sensor maps end_date to the absolute epoch."""
    mock_time.time.return_value = 1000.0
    primary = _make_state("on", {}, "sensor.washer")
    finish = _make_state(
        "1970-01-01T00:25:00+00:00",
        {"device_class": "timestamp"},
        "sensor.washer_finish",
    )
    config = {CONF_TEMPLATE: "countdown", CONF_REMAINING_TIME_ENTITY: "sensor.washer_finish"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_finish": finish}))

    assert content["end_date"] == 1500  # absolute epoch from the timestamp
    assert content["remaining_time"] == 500  # 1500 - now(1000)


def test_companion_attribute_overrides_state():
    """When both companion entity and attribute are set, read the companion's attribute."""
    primary = _make_state("on", {}, "sensor.washer")
    companion = _make_state("ignored", {"remaining": 300}, "sensor.washer_extra")
    config = {
        CONF_TEMPLATE: "countdown",
        CONF_REMAINING_TIME_ENTITY: "sensor.washer_extra",
        CONF_REMAINING_TIME_ATTR: "remaining",
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_extra": companion}))

    assert content["remaining_time"] == 300


def test_companion_unavailable_falls_back_to_default():
    """A configured-but-unavailable companion yields no value, not the primary's."""
    primary = _make_state("on", {"remaining": 999}, "sensor.washer")
    companion = _make_state("unavailable", {}, "sensor.washer_time")
    config = {
        CONF_TEMPLATE: "generic",
        CONF_REMAINING_TIME_ENTITY: "sensor.washer_time",
        CONF_REMAINING_TIME_ATTR: "remaining",
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_time": companion}))

    assert "remaining_time" not in content


def test_companion_subtitle_from_state():
    """Subtitle can come from a separate entity's state."""
    primary = _make_state("on", {"friendly_name": "Pralka"}, "sensor.pralka_stan")
    info = _make_state("Cottons 40°C", {}, "sensor.pralka_program")
    config = {CONF_TEMPLATE: "generic", CONF_SUBTITLE_ENTITY: "sensor.pralka_program"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.pralka_program": info}))

    assert content["subtitle"] == "Cottons 40°C"


def test_companion_gauge_value_from_state():
    """Gauge value can come from a separate numeric entity's state."""
    primary = _make_state("on", {}, "climate.living")
    temp = _make_state("21.5", {}, "sensor.living_temp")
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
        CONF_VALUE_ENTITY: "sensor.living_temp",
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.living_temp": temp}))

    assert content["value"] == 21.5


def test_companion_progress_from_state():
    """Progress can come from a separate 0-100 entity's state."""
    primary = _make_state("on", {}, "vacuum.robot")
    pct = _make_state("75", {}, "sensor.robot_progress")
    config = {CONF_TEMPLATE: "generic", CONF_PROGRESS_ENTITY: "sensor.robot_progress"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.robot_progress": pct}))

    assert content["progress"] == 0.75


def test_companion_current_step_from_state():
    """Steps current_step can come from a separate entity's state."""
    primary = _make_state("on", {}, "sensor.dishwasher")
    step = _make_state("2", {}, "sensor.dishwasher_phase")
    config = {
        CONF_TEMPLATE: "steps",
        CONF_TOTAL_STEPS: 3,
        CONF_CURRENT_STEP_ENTITY: "sensor.dishwasher_phase",
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.dishwasher_phase": step}))

    assert content["current_step"] == 2


def test_companion_fired_at_from_timestamp_entity():
    """Alert fired_at can come from a separate timestamp entity (ISO state)."""
    primary = _make_state("on", {}, "binary_sensor.alarm")
    fired = _make_state("1970-01-01T00:16:40+00:00", {"device_class": "timestamp"}, "sensor.alarm_time")
    config = {CONF_TEMPLATE: "alert", CONF_FIRED_AT_ENTITY: "sensor.alarm_time"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.alarm_time": fired}))

    assert content["fired_at"] == 1000


def test_no_companion_preserves_legacy_attribute_behavior():
    """Without a companion entity, values still read from the primary's attributes."""
    state = _make_state("active", {"friendly_name": "Tea Timer", "remaining": 120})
    config = {CONF_TEMPLATE: "countdown", CONF_REMAINING_TIME_ATTR: "remaining"}

    # No hass passed at all — legacy path.
    content = map_content(state, config)

    assert content["remaining_time"] == 120


def test_companion_configured_but_no_hass_yields_no_value():
    """A companion is configured but hass is unavailable → fail safe to default, not the primary."""
    primary = _make_state("on", {"remaining": 999}, "sensor.washer")
    config = {
        CONF_TEMPLATE: "generic",
        CONF_REMAINING_TIME_ENTITY: "sensor.washer_time",
        CONF_REMAINING_TIME_ATTR: "remaining",
    }

    # hass omitted: must NOT silently read the primary's "remaining" attribute.
    content = map_content(primary, config)

    assert "remaining_time" not in content


def test_companion_remaining_time_clock_mmss():
    """A 2-part 'MM:SS' string parses as minutes:seconds."""
    primary = _make_state("on", {}, "sensor.washer")
    time_sensor = _make_state("25:00", {}, "sensor.washer_time")
    config = {CONF_TEMPLATE: "countdown", CONF_REMAINING_TIME_ENTITY: "sensor.washer_time"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_time": time_sensor}))

    assert content["remaining_time"] == 25 * 60


def test_companion_remaining_time_invalid_clock_dropped():
    """A non-clock string containing ':' yields no remaining_time (no crash)."""
    primary = _make_state("on", {}, "sensor.washer")
    time_sensor = _make_state("ab:cd", {}, "sensor.washer_time")
    config = {CONF_TEMPLATE: "generic", CONF_REMAINING_TIME_ENTITY: "sensor.washer_time"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_time": time_sensor}))

    assert "remaining_time" not in content


def test_companion_remaining_time_negative_clamped():
    """A negative duration string clamps to zero rather than going negative."""
    primary = _make_state("on", {}, "sensor.washer")
    time_sensor = _make_state("-0:10", {}, "sensor.washer_time")
    config = {CONF_TEMPLATE: "generic", CONF_REMAINING_TIME_ENTITY: "sensor.washer_time"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_time": time_sensor}))

    assert content["remaining_time"] == 0


def test_companion_remaining_time_duration_unit_hours():
    """A duration sensor in hours is converted to seconds."""
    primary = _make_state("on", {}, "sensor.washer")
    time_sensor = _make_state("2", {"device_class": "duration", "unit_of_measurement": "h"}, "sensor.washer_time")
    config = {CONF_TEMPLATE: "generic", CONF_REMAINING_TIME_ENTITY: "sensor.washer_time"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_time": time_sensor}))

    assert content["remaining_time"] == 2 * 3600


def test_companion_remaining_time_duration_no_unit_is_seconds():
    """A duration sensor without a unit falls back to raw seconds."""
    primary = _make_state("on", {}, "sensor.washer")
    time_sensor = _make_state("90", {"device_class": "duration"}, "sensor.washer_time")
    config = {CONF_TEMPLATE: "generic", CONF_REMAINING_TIME_ENTITY: "sensor.washer_time"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_time": time_sensor}))

    assert content["remaining_time"] == 90


def test_companion_remaining_time_unknown_unit_is_raw_seconds():
    """An unsupported duration unit is treated as raw seconds, not dropped."""
    primary = _make_state("on", {}, "sensor.washer")
    time_sensor = _make_state(
        "10", {"device_class": "duration", "unit_of_measurement": "fortnights"}, "sensor.washer_time"
    )
    config = {CONF_TEMPLATE: "generic", CONF_REMAINING_TIME_ENTITY: "sensor.washer_time"}

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_time": time_sensor}))

    assert content["remaining_time"] == 10


def test_companion_attribute_missing_falls_back_to_default():
    """Companion entity present but the requested attribute is absent → no value."""
    primary = _make_state("on", {"remaining": 999}, "sensor.washer")
    companion = _make_state("on", {}, "sensor.washer_extra")  # no 'remaining' attr
    config = {
        CONF_TEMPLATE: "generic",
        CONF_REMAINING_TIME_ENTITY: "sensor.washer_extra",
        CONF_REMAINING_TIME_ATTR: "remaining",
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.washer_extra": companion}))

    assert "remaining_time" not in content


def test_companion_timeline_value_from_entity():
    """Timeline single-series value can come from a separate entity, labeled by the primary."""
    primary = _make_state("on", {"friendly_name": "Robot"}, "vacuum.robot")
    metric = _make_state("42", {}, "sensor.robot_metric")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_VALUE_ENTITY: "sensor.robot_metric",
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.robot_metric": metric}))

    assert content["value"] == {"Robot": 42.0}


# --- timeline series entities (multi-entity) -------------------------------


def test_timeline_series_entities_multi():
    """Each series entity becomes its own labelled line, read from that entity's state."""
    anchor = _make_state("on", {"friendly_name": "Air"}, "binary_sensor.air")
    bedroom = _make_state("12.5", {}, "sensor.bedroom_pm25")
    office = _make_state("8.0", {}, "sensor.office_pm25")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES_ENTITIES: [
            {CONF_LABEL: "Bedroom", CONF_ENTITY_ID: "sensor.bedroom_pm25"},
            {CONF_LABEL: "Office", CONF_ENTITY_ID: "sensor.office_pm25"},
        ],
    }

    content = map_content(
        anchor,
        config,
        hass=_FakeHass({"sensor.bedroom_pm25": bedroom, "sensor.office_pm25": office}),
    )

    assert content["value"] == {"Bedroom": 12.5, "Office": 8.0}
    assert_valid_activity_content(content)


def test_timeline_series_entity_attribute_and_rescale():
    """A series entity can read an attribute, with 0-255 attrs rescaled to 0-100."""
    anchor = _make_state("on", {"friendly_name": "Room"}, "binary_sensor.room")
    lamp = _make_state("on", {"brightness": 255}, "light.lamp")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES_ENTITIES: [{CONF_LABEL: "Lamp", CONF_ENTITY_ID: "light.lamp", "attribute": "brightness"}],
    }

    content = map_content(anchor, config, hass=_FakeHass({"light.lamp": lamp}))

    assert content["value"] == {"Lamp": 100}


def test_timeline_series_entities_combine_with_attribute_series():
    """CONF_SERIES (attribute map) and series entities coexist on one timeline."""
    anchor = _make_state("heating", {"friendly_name": "HVAC", "current_temperature": 20.5}, "climate.hvac")
    outdoor = _make_state("11.0", {}, "sensor.outdoor")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES: {"current_temperature": "Indoor"},
        CONF_SERIES_ENTITIES: [{CONF_LABEL: "Outdoor", CONF_ENTITY_ID: "sensor.outdoor"}],
    }

    content = map_content(anchor, config, hass=_FakeHass({"sensor.outdoor": outdoor}))

    assert content["value"] == {"Indoor": 20.5, "Outdoor": 11.0}


def test_timeline_series_entity_unavailable_key_omitted():
    """An unavailable series entity drops only its own key (RFC-7396 keeps last value)."""
    anchor = _make_state("on", {"friendly_name": "Air"}, "binary_sensor.air")
    bedroom = _make_state("12.5", {}, "sensor.bedroom_pm25")
    office = _make_state("unavailable", {}, "sensor.office_pm25")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES_ENTITIES: [
            {CONF_LABEL: "Bedroom", CONF_ENTITY_ID: "sensor.bedroom_pm25"},
            {CONF_LABEL: "Office", CONF_ENTITY_ID: "sensor.office_pm25"},
        ],
    }

    content = map_content(
        anchor,
        config,
        hass=_FakeHass({"sensor.bedroom_pm25": bedroom, "sensor.office_pm25": office}),
    )

    assert content["value"] == {"Bedroom": 12.5}
    assert "Office" not in content["value"]


def test_timeline_series_entity_non_numeric_skipped():
    """A non-numeric series entity value is skipped, not coerced to garbage."""
    anchor = _make_state("on", {"friendly_name": "Air"}, "binary_sensor.air")
    text = _make_state("open", {}, "binary_sensor.door")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES_ENTITIES: [{CONF_LABEL: "Door", CONF_ENTITY_ID: "binary_sensor.door"}],
    }

    content = map_content(anchor, config, hass=_FakeHass({"binary_sensor.door": text}))

    assert "value" not in content


def test_timeline_single_series_regression_without_series_entities():
    """With no series map and no series entities, the single-series fallback is unchanged."""
    state = _make_state("22.5", {"friendly_name": "Living Room Temp"})
    config = {CONF_TEMPLATE: "timeline"}

    content = map_content(state, config)

    assert content["value"] == {"Living Room Temp": 22.5}


# --- _get_timeline_units ----------------------------------------------------


def test_timeline_units_auto_default_from_uom():
    """A state-sourced series entity's unit auto-defaults from its unit_of_measurement."""
    anchor = _make_state("on", {"friendly_name": "Air"}, "binary_sensor.air")
    bedroom = _make_state("12.5", {"unit_of_measurement": "ppm"}, "sensor.bedroom_pm25")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES_ENTITIES: [{CONF_LABEL: "Bedroom", CONF_ENTITY_ID: "sensor.bedroom_pm25"}],
    }
    hass = _FakeHass({"sensor.bedroom_pm25": bedroom})

    values = _get_timeline_values(anchor, config, hass)
    units = _get_timeline_units(config, values, hass)

    assert units == {"Bedroom": "ppm"}


def test_timeline_units_explicit_overrides_auto():
    """An explicit CONF_UNITS entry overrides the auto-defaulted unit."""
    anchor = _make_state("on", {"friendly_name": "Air"}, "binary_sensor.air")
    bedroom = _make_state("12.5", {"unit_of_measurement": "ppm"}, "sensor.bedroom_pm25")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES_ENTITIES: [{CONF_LABEL: "Bedroom", CONF_ENTITY_ID: "sensor.bedroom_pm25"}],
        CONF_UNITS: {"Bedroom": "PM"},
    }
    hass = _FakeHass({"sensor.bedroom_pm25": bedroom})

    values = _get_timeline_values(anchor, config, hass)
    units = _get_timeline_units(config, values, hass)

    assert units == {"Bedroom": "PM"}


def test_timeline_units_filtered_to_value_keys():
    """Units for labels not present in values are dropped (server: units keys subset of value keys)."""
    anchor = _make_state("on", {"friendly_name": "Air"}, "binary_sensor.air")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES: {"current_temperature": "Indoor"},
        CONF_UNITS: {"Indoor": "°C", "Ghost": "°C"},
    }

    values = _get_timeline_values(anchor, config)
    units = _get_timeline_units(config, values, None)

    assert "Ghost" not in units


def test_timeline_units_attribute_series_no_auto_unit():
    """An attribute-sourced series entity contributes no auto unit (attributes have no uom)."""
    anchor = _make_state("on", {"friendly_name": "Room"}, "binary_sensor.room")
    lamp = _make_state("on", {"brightness": 128, "unit_of_measurement": "lx"}, "light.lamp")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES_ENTITIES: [{CONF_LABEL: "Lamp", CONF_ENTITY_ID: "light.lamp", "attribute": "brightness"}],
    }
    hass = _FakeHass({"light.lamp": lamp})

    values = _get_timeline_values(anchor, config, hass)
    units = _get_timeline_units(config, values, hass)

    assert units == {}


# --- _timeline_recorder_sources --------------------------------------------


def test_recorder_sources_state_series_only():
    """Only state-sourced series entities map to a recorder source; attribute ones don't."""
    state = _make_state("on", {"friendly_name": "Air"}, "binary_sensor.air")
    config = {
        CONF_TEMPLATE: "timeline",
        CONF_SERIES_ENTITIES: [
            {CONF_LABEL: "Bedroom", CONF_ENTITY_ID: "sensor.bedroom_pm25"},
            {CONF_LABEL: "Lamp", CONF_ENTITY_ID: "light.lamp", "attribute": "brightness"},
        ],
    }

    sources = _timeline_recorder_sources(state, config)

    assert sources == {"Bedroom": "sensor.bedroom_pm25"}


def test_recorder_sources_single_series_value_entity():
    """The single-series fallback maps the friendly-name label to the value entity."""
    state = _make_state("on", {"friendly_name": "Meter"}, "sensor.meter")
    config = {CONF_TEMPLATE: "timeline", CONF_VALUE_ENTITY: "sensor.power_raw"}

    sources = _timeline_recorder_sources(state, config)

    assert sources == {"Meter": "sensor.power_raw"}


def test_recorder_sources_single_series_tracked_entity():
    """With no value entity, the single-series fallback maps to the tracked entity itself."""
    state = _make_state("22.5", {"friendly_name": "Room Temp"}, "sensor.room_temp")
    config = {CONF_TEMPLATE: "timeline"}

    sources = _timeline_recorder_sources(state, config)

    assert sources == {"Room Temp": "sensor.room_temp"}


def test_recorder_sources_excludes_attribute_single_series():
    """A value_attribute single-series has no recorder source (attributes are stripped)."""
    state = _make_state("on", {"friendly_name": "Lamp"}, "light.lamp")
    config = {CONF_TEMPLATE: "timeline", CONF_VALUE_ATTRIBUTE: "brightness"}

    assert _timeline_recorder_sources(state, config) == {}


def test_recorder_sources_excludes_attribute_map_single():
    """When CONF_SERIES (attribute map) is set, the single-series fallback is not added."""
    state = _make_state("heating", {"friendly_name": "HVAC"}, "climate.hvac")
    config = {CONF_TEMPLATE: "timeline", CONF_SERIES: {"current_temperature": "Indoor"}}

    assert _timeline_recorder_sources(state, config) == {}


def test_companion_gauge_value_attribute_rescaled():
    """A companion gauge value read from a 0-255 attribute is rescaled to 0-100."""
    primary = _make_state("on", {}, "light.lamp")
    companion = _make_state("on", {"brightness": 128}, "light.other")
    config = {
        CONF_TEMPLATE: "gauge",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
        CONF_VALUE_ENTITY: "light.other",
        CONF_VALUE_ATTRIBUTE: "brightness",
    }

    content = map_content(primary, config, hass=_FakeHass({"light.other": companion}))

    assert content["value"] == 50  # round(128 / 255 * 100)


def test_steps_progress_entity_suppresses_autoderive():
    """A configured progress source overrides the steps auto-derived progress."""
    primary = _make_state("on", {"step": 2}, "sensor.dishwasher")
    pct = _make_state("30", {}, "sensor.dishwasher_progress")
    config = {
        CONF_TEMPLATE: "steps",
        CONF_TOTAL_STEPS: 4,
        CONF_CURRENT_STEP_ATTR: "step",  # auto-derive would yield 2/4 = 0.5
        CONF_PROGRESS_ENTITY: "sensor.dishwasher_progress",
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.dishwasher_progress": pct}))

    assert content["progress"] == 0.3  # from the entity, NOT the 0.5 auto-derive


# --- board template -------------------------------------------------------


def test_map_content_board_tiles():
    """Board reads each tile's bound entity into a {label, value, unit} tile."""
    primary = _make_state("on", {"friendly_name": "Home"}, "binary_sensor.home_status")
    cpu = _make_state("72", {}, "sensor.cpu")
    door = _make_state("open", {}, "binary_sensor.door")
    config = {
        CONF_TEMPLATE: "board",
        CONF_TILES: [
            {CONF_LABEL: "CPU", CONF_ENTITY_ID: "sensor.cpu", CONF_UNIT: "%"},
            {CONF_LABEL: "Door", CONF_ENTITY_ID: "binary_sensor.door"},
        ],
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.cpu": cpu, "binary_sensor.door": door}))

    assert content["template"] == "board"
    assert content["progress"] == 0.0
    assert content["tiles"][0]["label"] == "CPU"
    assert content["tiles"][0]["value"] == "72"
    assert content["tiles"][0]["unit"] == "%"
    assert content["tiles"][1]["label"] == "Door"
    assert content["tiles"][1]["value"] == "open"
    assert_valid_activity_content(content)


def test_map_content_board_reads_tile_attribute():
    """A tile with value_attribute reads that attribute, not the state."""
    primary = _make_state("on", {}, "binary_sensor.home_status")
    climate = _make_state("heat", {"temperature": 21.5}, "climate.living")
    config = {
        CONF_TEMPLATE: "board",
        CONF_TILES: [{CONF_LABEL: "Set", CONF_ENTITY_ID: "climate.living", CONF_VALUE_ATTRIBUTE: "temperature"}],
    }

    content = map_content(primary, config, hass=_FakeHass({"climate.living": climate}))

    assert content["tiles"][0]["value"] == "21.5"


def test_map_content_board_skips_unavailable_tile():
    """A tile whose entity is unavailable/unknown is skipped."""
    primary = _make_state("on", {}, "binary_sensor.home_status")
    cpu = _make_state("72", {}, "sensor.cpu")
    dead = _make_state("unavailable", {}, "sensor.dead")
    config = {
        CONF_TEMPLATE: "board",
        CONF_TILES: [
            {CONF_LABEL: "CPU", CONF_ENTITY_ID: "sensor.cpu"},
            {CONF_LABEL: "Dead", CONF_ENTITY_ID: "sensor.dead"},
        ],
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.cpu": cpu, "sensor.dead": dead}))

    assert len(content["tiles"]) == 1
    assert content["tiles"][0]["label"] == "CPU"


def test_map_content_board_truncates_value_to_cap():
    """A long tile value is truncated to the 16-char server cap."""
    primary = _make_state("on", {}, "binary_sensor.home_status")
    long_sensor = _make_state("x" * 40, {}, "sensor.long")
    config = {
        CONF_TEMPLATE: "board",
        CONF_TILES: [{CONF_LABEL: "Long", CONF_ENTITY_ID: "sensor.long"}],
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.long": long_sensor}))

    assert len(content["tiles"][0]["value"]) == 16
    assert_valid_activity_content(content)


def test_map_content_board_emits_tile_color_and_url_action():
    """A configured per-tile color and url become tile content (color + url_action)."""
    primary = _make_state("on", {}, "binary_sensor.home_status")
    cpu = _make_state("72", {}, "sensor.cpu")
    config = {
        CONF_TEMPLATE: "board",
        CONF_TILES: [
            {
                CONF_LABEL: "CPU",
                CONF_ENTITY_ID: "sensor.cpu",
                "color": "red",
                "url_action": "https://ha.local/cpu",
            }
        ],
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.cpu": cpu}))

    tile = content["tiles"][0]
    assert tile["color"] == "red"
    assert tile["url_action"] == {"url": "https://ha.local/cpu", "foreground": True}
    assert_valid_activity_content(content)


def test_map_content_board_omits_unset_color_and_url_action():
    """A tile without color/url emits neither key (no empty color, no null action)."""
    primary = _make_state("on", {}, "binary_sensor.home_status")
    cpu = _make_state("72", {}, "sensor.cpu")
    config = {
        CONF_TEMPLATE: "board",
        CONF_TILES: [{CONF_LABEL: "CPU", CONF_ENTITY_ID: "sensor.cpu"}],
    }

    content = map_content(primary, config, hass=_FakeHass({"sensor.cpu": cpu}))

    tile = content["tiles"][0]
    assert "color" not in tile
    assert "url_action" not in tile


def test_map_completion_content_board_carries_tiles():
    """Board completion carries the last rendered tiles (server requires ≥1)."""
    config = {CONF_TEMPLATE: "board"}
    last = {"template": "board", "tiles": [{"label": "CPU", "value": "72"}], "progress": 0.0}

    content = map_completion_content(config, last_content=last)

    assert content["tiles"] == [{"label": "CPU", "value": "72"}]
    assert_valid_activity_content(content)


# --- log template ---------------------------------------------------------


def test_map_content_log_single_line():
    """Log maps the current state into one line; progress is 0."""
    state = _make_state("open", {"friendly_name": "Front Door"}, "binary_sensor.front_door")
    config = {CONF_TEMPLATE: "log"}

    content = map_content(state, config)

    assert content["template"] == "log"
    assert content["progress"] == 0.0
    assert len(content["lines"]) == 1
    assert content["lines"][0]["text"] == "Open"


def test_map_content_log_uses_state_label():
    """A custom state label is used as the log line text."""
    state = _make_state("heat_pump", {}, "climate.living")
    config = {CONF_TEMPLATE: "log", CONF_STATE_LABELS: {"heat_pump": "Heat Pump On"}}

    content = map_content(state, config)

    assert content["lines"][0]["text"] == "Heat Pump On"


def test_build_log_line_level_from_attribute():
    """The line level comes from CONF_LOG_LEVEL_ATTRIBUTE when valid."""
    state = _make_state("triggered", {"sev": "warn"}, "sensor.event")
    config = {CONF_TEMPLATE: "log", CONF_LOG_LEVEL_ATTRIBUTE: "sev"}

    line = _build_log_line(state, config)

    assert line["text"] == "Triggered"
    assert line["level"] == "warn"


def test_build_log_line_invalid_level_omitted():
    """An attribute value outside info/warn/error is dropped, not sent."""
    state = _make_state("x", {"sev": "debug"}, "sensor.event")
    config = {CONF_TEMPLATE: "log", CONF_LOG_LEVEL_ATTRIBUTE: "sev"}

    line = _build_log_line(state, config)

    assert "level" not in line


def test_build_log_line_at_from_last_updated():
    """A real last_updated datetime becomes the line's at epoch."""
    import datetime as _dt

    state = _make_state("x", {}, "sensor.event")
    state.last_updated = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)

    line = _build_log_line(state, {CONF_TEMPLATE: "log"})

    assert line["at"] == int(state.last_updated.timestamp())


def test_map_completion_content_log_carries_lines():
    """Log completion carries the last rendered lines (server requires ≥1)."""
    config = {CONF_TEMPLATE: "log"}
    last = {"template": "log", "lines": [{"text": "Door opened"}], "progress": 0.0}

    content = map_completion_content(config, last_content=last)

    assert content["lines"] == [{"text": "Door opened"}]
    assert_valid_activity_content(content)


# --- log_columns ----------------------------------------------------------


def test_build_log_line_tracked_attribute_column():
    """A bare-attribute column reads an attribute of the tracked entity, with a unit suffix."""
    state = _make_state("on", {"color_temp_kelvin": 4000}, "light.lamp")
    config = {CONF_TEMPLATE: "log", CONF_LOG_COLUMNS: [{"attribute": "color_temp_kelvin", CONF_UNIT: "K"}]}

    line = _build_log_line(state, config)

    assert line["text"] == "On · 4000K"


def test_build_log_line_other_entity_state_and_attribute_columns():
    """An entity-state column and an entity-attribute column resolve via hass."""
    state = _make_state("on", {}, "light.lamp")
    door = _make_state("open", {}, "binary_sensor.door")
    temp = _make_state("21.5", {"temperature": 21.5}, "sensor.temp")
    config = {
        CONF_TEMPLATE: "log",
        CONF_LOG_COLUMNS: [
            {CONF_ENTITY_ID: "binary_sensor.door"},
            {CONF_ENTITY_ID: "sensor.temp", "attribute": "temperature"},
        ],
    }

    line = _build_log_line(state, config, _FakeHass({"binary_sensor.door": door, "sensor.temp": temp}))

    assert line["text"] == "On · open · 21.5"


def test_build_log_line_labeled_column():
    """A column with a label renders 'Label: value'."""
    state = _make_state("on", {}, "light.lamp")
    door = _make_state("Open", {}, "binary_sensor.door")
    config = {CONF_TEMPLATE: "log", CONF_LOG_COLUMNS: [{CONF_LABEL: "Door", CONF_ENTITY_ID: "binary_sensor.door"}]}

    line = _build_log_line(state, config, _FakeHass({"binary_sensor.door": door}))

    assert line["text"] == "On · Door: Open"


def test_build_log_line_off_state_falls_back_to_bare_label():
    """When every column resolves empty (lamp off → no brightness), text is just the state label."""
    state = _make_state("off", {}, "light.lamp")
    config = {CONF_TEMPLATE: "log", CONF_LOG_COLUMNS: [{"attribute": "brightness"}]}

    line = _build_log_line(state, config)

    assert line["text"] == "Off"


def test_build_log_line_skips_missing_and_unavailable_columns():
    """A missing attribute and an unavailable entity column are skipped; valid columns remain."""
    state = _make_state("on", {"brightness": 153}, "light.lamp")
    dead = _make_state("unavailable", {}, "sensor.dead")
    config = {
        CONF_TEMPLATE: "log",
        CONF_LOG_COLUMNS: [
            {CONF_ENTITY_ID: "sensor.dead"},
            {"attribute": "missing_attr"},
            {"attribute": "brightness"},
        ],
    }

    line = _build_log_line(state, config, _FakeHass({"sensor.dead": dead}))

    assert line["text"] == "On · 153"


def test_build_log_line_entity_column_skipped_without_hass():
    """Without hass, entity columns can't be read and are skipped; attribute columns still resolve."""
    state = _make_state("on", {"brightness": 153}, "light.lamp")
    config = {
        CONF_TEMPLATE: "log",
        CONF_LOG_COLUMNS: [{CONF_ENTITY_ID: "binary_sensor.door"}, {"attribute": "brightness"}],
    }

    line = _build_log_line(state, config)

    assert line["text"] == "On · 153"


def test_map_content_log_composes_columns():
    """map_content passes hass through so the log line's text carries the columns."""
    state = _make_state("on", {"color_temp_kelvin": 4000}, "light.lamp")
    cpu = _make_state("72", {}, "sensor.cpu")
    config = {
        CONF_TEMPLATE: "log",
        CONF_LOG_COLUMNS: [{"attribute": "color_temp_kelvin", CONF_UNIT: "K"}, {CONF_ENTITY_ID: "sensor.cpu"}],
    }

    content = map_content(state, config, hass=_FakeHass({"sensor.cpu": cpu}))

    assert content["lines"][0]["text"] == "On · 4000K · 72"
    assert_valid_activity_content(content)
