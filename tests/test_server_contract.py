"""Tests for the server-contract assertion helpers themselves.

A contract validator is only useful if it *rejects* violations — a lenient one
would silently bless server-invalid payloads and give every other test a false
sense of safety. These tests pin both directions: valid payloads pass, and each
documented rule rejects a concrete violating payload.
"""

from __future__ import annotations

import time

import pytest

from custom_components.pushward.const import LOG_LINE_TEXT_MAX

from .server_contract import (
    PushWardContractError,
    assert_valid_activity_content,
    assert_valid_priority,
    assert_valid_sound,
    assert_valid_widget_content,
)

# --- minimal valid payload factories ---------------------------------------


def valid_generic() -> dict:
    return {
        "template": "generic",
        "progress": 0.0,
        "state": "On",
        "icon": "mdi:washing-machine",
        "subtitle": "Washer",
        "accent_color": "blue",
    }


def valid_countdown() -> dict:
    now = int(time.time())
    return {
        "template": "countdown",
        "progress": 0.4,
        "state": "Running",
        "accent_color": "blue",
        "start_date": now,
        "end_date": now + 3600,
    }


def valid_steps() -> dict:
    return {
        "template": "steps",
        "progress": 0.33,
        "state": "Washing",
        "accent_color": "blue",
        "total_steps": 3,
        "current_step": 1,
    }


def valid_alert() -> dict:
    return {"template": "alert", "progress": 0.0, "state": "Triggered", "accent_color": "red", "severity": "critical"}


def valid_gauge() -> dict:
    return {
        "template": "gauge",
        "progress": 0.5,
        "state": "50",
        "accent_color": "blue",
        "value": 50.0,
        "min_value": 0.0,
        "max_value": 100.0,
    }


def valid_timeline() -> dict:
    return {"template": "timeline", "progress": 0.0, "state": "x", "accent_color": "blue", "value": {"CPU": 50.0}}


def valid_board() -> dict:
    return {
        "template": "board",
        "progress": 0.0,
        "state": "Status",
        "accent_color": "blue",
        "tiles": [{"label": "CPU", "value": "72", "unit": "%", "trend": "up"}],
    }


def valid_log() -> dict:
    return {
        "template": "log",
        "progress": 0.0,
        "state": "Log",
        "accent_color": "blue",
        "lines": [{"text": "Front door opened", "at": int(time.time()), "level": "info"}],
    }


def _mut(factory, **changes) -> dict:
    d = factory()
    d.update(changes)
    return d


def _rm(factory, *keys) -> dict:
    d = factory()
    for k in keys:
        d.pop(k, None)
    return d


# --- activity: valid payloads pass -----------------------------------------


@pytest.mark.parametrize(
    "factory",
    [valid_generic, valid_countdown, valid_steps, valid_alert, valid_gauge, valid_timeline, valid_board, valid_log],
    ids=["generic", "countdown", "steps", "alert", "gauge", "timeline", "board", "log"],
)
def test_valid_activity_payloads_pass(factory) -> None:
    assert_valid_activity_content(factory())


def test_valid_steps_weights_and_colors_pass() -> None:
    """Full-length weights + colors, with a blank entry deferring to accent_color."""
    assert_valid_activity_content(_mut(valid_steps, step_weights=[1, 2.5, 1], step_colors=["green", "", "#ff0000"]))


# --- activity: violations are rejected -------------------------------------

_ACTIVITY_INVALID = [
    pytest.param(lambda: _mut(valid_generic, progress=1.5), id="progress_above_1"),
    pytest.param(lambda: _mut(valid_generic, progress=-0.1), id="progress_below_0"),
    pytest.param(lambda: _mut(valid_generic, accent_color="chartreuse"), id="unknown_named_colour"),
    pytest.param(lambda: _mut(valid_generic, accent_color="#12"), id="bad_hex_colour"),
    pytest.param(lambda: _mut(valid_generic, state="x" * 257), id="state_too_long"),
    pytest.param(lambda: _mut(valid_generic, icon="m" * 129), id="icon_too_long"),
    pytest.param(lambda: _mut(valid_generic, remaining_time=-5), id="negative_remaining_time"),
    pytest.param(lambda: _mut(valid_generic, tap_action={"foreground": True}), id="tap_action_missing_url"),
    pytest.param(
        lambda: _mut(valid_generic, tap_action={"url": "homeassistant://x", "method": "POST"}),
        id="tap_method_on_custom_scheme",
    ),
    pytest.param(lambda: _rm(valid_countdown, "end_date"), id="countdown_missing_end_date"),
    pytest.param(lambda: _mut(valid_countdown, end_date=0), id="countdown_zero_end_date"),
    pytest.param(lambda: (lambda c: {**c, "start_date": c["end_date"] + 10})(valid_countdown()), id="start_after_end"),
    pytest.param(lambda: _mut(valid_countdown, warning_threshold=-1), id="negative_warning_threshold"),
    pytest.param(lambda: _mut(valid_countdown, warning_threshold=86401), id="warning_threshold_above_max"),
    pytest.param(lambda: _mut(valid_countdown, snooze_seconds=30), id="snooze_below_min"),
    pytest.param(lambda: _mut(valid_countdown, snooze_seconds=4000), id="snooze_above_max"),
    pytest.param(lambda: _mut(valid_countdown, end_date=int(time.time()) + 6 * 365 * 24 * 3600), id="end_date_too_far"),
    pytest.param(lambda: _mut(valid_countdown, end_date=True), id="end_date_bool"),
    pytest.param(lambda: _mut(valid_steps, current_step=5), id="current_step_exceeds_total"),
    pytest.param(lambda: _mut(valid_steps, current_step=-1), id="negative_current_step"),
    pytest.param(lambda: _mut(valid_steps, total_steps=0), id="zero_total_steps"),
    pytest.param(lambda: _mut(valid_steps, total_steps=65), id="total_steps_above_max"),
    pytest.param(lambda: _mut(valid_steps, step_rows=[1, 2]), id="step_rows_length_mismatch"),
    pytest.param(lambda: _mut(valid_steps, step_rows=[1, 2, 99]), id="step_rows_out_of_range"),
    pytest.param(lambda: _mut(valid_steps, step_labels=["a", "b"]), id="step_labels_length_mismatch"),
    pytest.param(lambda: _mut(valid_steps, step_labels=["x" * 33, "b", "c"]), id="step_label_too_long"),
    pytest.param(lambda: _mut(valid_steps, step_weights=[1, 2]), id="step_weights_length_mismatch"),
    pytest.param(lambda: _mut(valid_steps, step_weights=[1, 0, 2]), id="step_weights_zero_entry"),
    pytest.param(lambda: _mut(valid_steps, step_weights=[1, -2, 3]), id="step_weights_negative_entry"),
    pytest.param(lambda: _mut(valid_steps, step_weights=[1, float("inf"), 3]), id="step_weights_not_finite"),
    pytest.param(lambda: _mut(valid_steps, step_colors=["red", "blue"]), id="step_colors_length_mismatch"),
    pytest.param(lambda: _mut(valid_steps, step_colors=["red", "chartreuse", "blue"]), id="step_colors_unknown_name"),
    pytest.param(lambda: _mut(valid_steps, step_colors=["red", "#12", "blue"]), id="step_colors_bad_hex"),
    pytest.param(lambda: _mut(valid_alert, severity="meh"), id="alert_bad_severity"),
    pytest.param(lambda: _mut(valid_alert, fired_at=int(time.time()) + 99999), id="alert_fired_at_future"),
    pytest.param(lambda: _mut(valid_alert, fired_at=0), id="alert_fired_at_non_positive"),
    pytest.param(lambda: _rm(valid_gauge, "min_value"), id="gauge_missing_min"),
    pytest.param(lambda: _mut(valid_gauge, min_value=100.0, max_value=100.0), id="gauge_min_eq_max"),
    pytest.param(lambda: _mut(valid_gauge, value=500.0), id="gauge_value_out_of_range"),
    pytest.param(lambda: _mut(valid_gauge, value=float("inf")), id="gauge_value_not_finite"),
    pytest.param(lambda: _mut(valid_gauge, unit="x" * 33), id="gauge_unit_too_long"),
    pytest.param(lambda: _mut(valid_timeline, value=50.0), id="timeline_value_is_number"),
    pytest.param(lambda: _mut(valid_timeline, value={}), id="timeline_value_empty"),
    pytest.param(lambda: _mut(valid_timeline, value={"": 1.0}), id="timeline_value_empty_key"),
    pytest.param(lambda: _mut(valid_timeline, value={"x" * 33: 1.0}), id="timeline_value_key_too_long"),
    pytest.param(lambda: _mut(valid_timeline, value={"CPU": "hot"}), id="timeline_value_not_number"),
    pytest.param(lambda: _mut(valid_timeline, value={f"s{i}": 1.0 for i in range(11)}), id="timeline_too_many_series"),
    pytest.param(lambda: _mut(valid_timeline, units={"DISK": "GB"}), id="timeline_units_key_mismatch"),
    pytest.param(lambda: _mut(valid_timeline, scale="exponential"), id="timeline_bad_scale"),
    pytest.param(lambda: _mut(valid_timeline, decimals=11), id="timeline_decimals_out_of_range"),
    pytest.param(lambda: _mut(valid_timeline, decimals=1.5), id="timeline_decimals_float"),
    pytest.param(
        lambda: _mut(valid_timeline, thresholds=[{"value": float(i)} for i in range(6)]), id="too_many_thresh"
    ),
    pytest.param(lambda: _mut(valid_timeline, thresholds=[42]), id="threshold_not_dict"),
    pytest.param(lambda: _mut(valid_timeline, thresholds=[{"value": "x"}]), id="threshold_value_nan"),
    pytest.param(
        lambda: _mut(valid_timeline, thresholds=[{"value": 1.0, "color": "chartreuse"}]), id="thresh_bad_color"
    ),
    pytest.param(lambda: _mut(valid_timeline, thresholds=[{"value": 1.0, "label": "x" * 13}]), id="thresh_label_long"),
    pytest.param(
        lambda: _mut(valid_timeline, history={"CPU": [{"timestamp": int(time.time()) + 99999, "value": 1.0}]}),
        id="timeline_history_future_ts",
    ),
    pytest.param(
        lambda: _mut(valid_timeline, history={"CPU": [{"timestamp": 0, "value": 1.0}]}), id="timeline_history_bad_ts"
    ),
    pytest.param(
        lambda: _mut(valid_timeline, history={"CPU": [{"timestamp": int(time.time()), "value": "x"}]}),
        id="timeline_history_value_nan",
    ),
    pytest.param(lambda: _mut(valid_timeline, history={"CPU": [42]}), id="timeline_history_point_not_dict"),
    pytest.param(lambda: _mut(valid_generic, remaining_time=True), id="remaining_time_bool"),
    pytest.param(lambda: _mut(valid_generic, remaining_time=5.5), id="remaining_time_float"),
    pytest.param(lambda: _mut(valid_generic, progress=float("inf")), id="progress_not_finite"),
    pytest.param(lambda: _mut(valid_generic, subtitle="x" * 256), id="subtitle_too_long"),
    pytest.param(lambda: _mut(valid_generic, completion_message="x" * 1025), id="completion_message_too_long"),
    pytest.param(lambda: _mut(valid_generic, state=123), id="state_not_string"),
    pytest.param(lambda: _mut(valid_generic, accent_color=123), id="colour_not_string"),
    pytest.param(lambda: _mut(valid_generic, template="bogus"), id="unknown_template"),
    pytest.param(lambda: _mut(valid_generic, tap_action={"url": "https://x", "title": "t" * 65}), id="tap_title_long"),
    pytest.param(lambda: _mut(valid_generic, tap_action={"url": "https://x", "icon": "i" * 65}), id="tap_icon_long"),
    pytest.param(lambda: _mut(valid_generic, tap_action="homeassistant://x"), id="tap_action_not_dict"),
    pytest.param(lambda: ["not", "a", "dict"], id="content_not_dict"),
    # board violations
    pytest.param(lambda: _rm(valid_board, "tiles"), id="board_missing_tiles"),
    pytest.param(lambda: _mut(valid_board, tiles=[]), id="board_empty_tiles"),
    pytest.param(
        lambda: _mut(valid_board, tiles=[{"label": f"t{i}", "value": "1"} for i in range(5)]), id="board_too_many_tiles"
    ),
    pytest.param(lambda: _mut(valid_board, tiles=[{"value": "1"}]), id="board_tile_missing_label"),
    pytest.param(lambda: _mut(valid_board, tiles=[{"label": "x" * 33, "value": "1"}]), id="board_tile_label_too_long"),
    pytest.param(lambda: _mut(valid_board, tiles=[{"label": "L"}]), id="board_tile_missing_value"),
    pytest.param(lambda: _mut(valid_board, tiles=[{"label": "L", "value": 72}]), id="board_tile_value_not_string"),
    pytest.param(lambda: _mut(valid_board, tiles=[{"label": "L", "value": "x" * 17}]), id="board_tile_value_too_long"),
    pytest.param(
        lambda: _mut(valid_board, tiles=[{"label": "L", "value": "1", "unit": "x" * 9}]), id="board_tile_unit_too_long"
    ),
    pytest.param(
        lambda: _mut(valid_board, tiles=[{"label": "L", "value": "1", "icon": "x" * 129}]),
        id="board_tile_icon_too_long",
    ),
    pytest.param(
        lambda: _mut(valid_board, tiles=[{"label": "L", "value": "1", "color": "chartreuse"}]),
        id="board_tile_bad_color",
    ),
    pytest.param(
        lambda: _mut(valid_board, tiles=[{"label": "L", "value": "1", "trend": "sideways"}]), id="board_tile_bad_trend"
    ),
    pytest.param(
        lambda: _mut(valid_board, tiles=[{"label": "L", "value": "1", "url_action": {"foreground": True}}]),
        id="board_tile_url_action_missing_url",
    ),
    # log violations
    pytest.param(lambda: _rm(valid_log, "lines"), id="log_missing_lines"),
    pytest.param(lambda: _mut(valid_log, lines=[]), id="log_empty_lines"),
    pytest.param(lambda: _mut(valid_log, lines=[{"text": f"line {i}"} for i in range(21)]), id="log_too_many_lines"),
    pytest.param(lambda: _mut(valid_log, lines=[{"level": "info"}]), id="log_line_missing_text"),
    pytest.param(lambda: _mut(valid_log, lines=[{"text": "x" * (LOG_LINE_TEXT_MAX + 1)}]), id="log_line_text_too_long"),
    pytest.param(lambda: _mut(valid_log, lines=[{"text": "x", "level": "debug"}]), id="log_line_bad_level"),
    pytest.param(lambda: _mut(valid_log, lines=[{"text": "x", "at": 0}]), id="log_line_at_non_positive"),
    pytest.param(lambda: _mut(valid_log, lines=[{"text": "x", "at": 1.5}]), id="log_line_at_float"),
    pytest.param(lambda: _mut(valid_log, log_backlog=[{"text": "x"}]), id="log_backlog_must_not_be_sent"),
    # live_progress: until these landed, _assert_live_progress had never rejected
    # anything -- it could have returned unconditionally and the suite stayed green.
    pytest.param(lambda: _mut(valid_generic, live_progress=True), id="generic_lp_missing_end_date"),
    pytest.param(
        lambda: _mut(valid_generic, live_progress=True, end_date=int(time.time()) - 10), id="generic_lp_past_end"
    ),
    pytest.param(lambda: _mut(valid_generic, live_progress=1), id="lp_not_a_bool"),
    pytest.param(lambda: _mut(valid_countdown, live_progress=True), id="lp_on_unsupported_template"),
    pytest.param(
        lambda: _mut(valid_steps, live_progress=True, end_date=int(time.time()) + 600),
        id="steps_lp_missing_start_date",
    ),
    pytest.param(
        lambda: _mut(
            valid_steps, live_progress=True, start_date=int(time.time()) + 700, end_date=int(time.time()) + 600
        ),
        id="steps_lp_start_after_end",
    ),
    pytest.param(
        lambda: _mut(valid_steps, live_progress=True, start_date=int(time.time()) - 60, end_date=int(time.time()) - 10),
        id="steps_lp_past_end",
    ),
]


@pytest.mark.parametrize("builder", _ACTIVITY_INVALID)
def test_invalid_activity_payloads_rejected(builder) -> None:
    with pytest.raises(PushWardContractError):
        assert_valid_activity_content(builder())


def test_valid_live_progress_payloads_pass() -> None:
    now = int(time.time())
    assert_valid_activity_content(_mut(valid_generic, live_progress=True, end_date=now + 600))
    assert_valid_activity_content(_mut(valid_steps, live_progress=True, start_date=now, end_date=now + 600))
    # Explicitly off needs no window: that is how a mid-run update stops the animation.
    assert_valid_activity_content(_mut(valid_steps, live_progress=False))


# --- widgets: valid payloads pass ------------------------------------------


@pytest.mark.parametrize(
    "content,template",
    [
        ({"value": 42.0}, "value"),
        ({}, "value"),  # value is optional for the value template
        ({"value": 0.65}, "progress"),
        ({"value": 50.0, "min_value": 0.0, "max_value": 100.0}, "gauge"),
        ({"severity": "warning", "label": "Armed"}, "status"),
        ({"stat_rows": [{"label": "Temp", "value": "21.4", "unit": "°C"}]}, "stat_list"),
        ({"value": 1.0, "severity": None, "trend": None}, "value"),  # null == absent (no annotation)
    ],
    ids=["value", "value_empty", "progress", "gauge", "status", "stat_list", "null_severity_trend"],
)
def test_valid_widget_payloads_pass(content, template) -> None:
    assert_valid_widget_content(content, template)


# --- widgets: violations are rejected --------------------------------------

_WIDGET_INVALID = [
    pytest.param({}, "progress", id="progress_missing_value"),
    pytest.param({"value": 1.5}, "progress", id="progress_value_above_1"),
    pytest.param({"value": 50.0, "min_value": 0.0}, "gauge", id="gauge_missing_max"),
    pytest.param({"value": 50.0, "min_value": 100.0, "max_value": 0.0}, "gauge", id="gauge_min_gt_max"),
    pytest.param({"value": 500.0, "min_value": 0.0, "max_value": 100.0}, "gauge", id="gauge_value_out_of_range"),
    pytest.param({"stat_rows": []}, "stat_list", id="stat_list_empty"),
    pytest.param(
        {"stat_rows": [{"label": f"r{i}", "value": "1"} for i in range(7)]}, "stat_list", id="stat_list_too_many"
    ),
    pytest.param({"stat_rows": [{"label": "", "value": "1"}]}, "stat_list", id="stat_list_empty_label"),
    pytest.param({"stat_rows": [{"label": "x" * 33, "value": "1"}]}, "stat_list", id="stat_list_label_too_long"),
    pytest.param({"value": 1.0, "severity": "bogus"}, "value", id="bad_severity"),
    pytest.param({"value": 1.0, "trend": "sideways"}, "value", id="bad_trend"),
    pytest.param({"value": 1.0, "accent_color": "chartreuse"}, "value", id="bad_colour"),
    pytest.param({"value": -0.1}, "progress", id="progress_value_below_0"),
    pytest.param({"value": float("inf")}, "value", id="value_widget_not_finite"),
    pytest.param({"value": 1.0, "icon": "x" * 129}, "value", id="widget_icon_too_long"),
    pytest.param({"value": 1.0, "label": "x" * 257}, "value", id="widget_label_too_long"),
    pytest.param({"value": 1.0, "subtitle": "x" * 257}, "value", id="widget_subtitle_too_long"),
    pytest.param({"value": 1.0, "unit": "x" * 33}, "value", id="widget_unit_too_long"),
    pytest.param({"stat_rows": [{"label": "L", "value": ""}]}, "stat_list", id="stat_row_empty_value"),
    pytest.param({"stat_rows": [{"label": "L", "value": "x" * 33}]}, "stat_list", id="stat_row_value_too_long"),
    pytest.param({"stat_rows": [{"label": "L", "value": "1", "unit": "x" * 17}]}, "stat_list", id="stat_row_unit_long"),
    pytest.param({}, "bogus", id="widget_bad_template"),
    pytest.param(["x"], "value", id="widget_content_not_dict"),
]


@pytest.mark.parametrize("content,template", _WIDGET_INVALID)
def test_invalid_widget_payloads_rejected(content, template) -> None:
    with pytest.raises(PushWardContractError):
        assert_valid_widget_content(content, template)


# --- sound & priority ------------------------------------------------------


@pytest.mark.parametrize("sound", ["", None, "default", "chime", "success"])
def test_valid_sounds_pass(sound) -> None:
    assert_valid_sound(sound)


@pytest.mark.parametrize("sound", ["meow", "DEFAULT", "siren"])
def test_invalid_sounds_rejected(sound) -> None:
    with pytest.raises(PushWardContractError):
        assert_valid_sound(sound)


@pytest.mark.parametrize("priority", [0, 1, 5, 10])
def test_valid_priorities_pass(priority) -> None:
    assert_valid_priority(priority)


@pytest.mark.parametrize("priority", [-1, 11, 1.5, True, False])
def test_invalid_priorities_rejected(priority) -> None:
    with pytest.raises(PushWardContractError):
        assert_valid_priority(priority)


# --- boundary payloads that the relaxed/tightened caps must accept ----------


def test_max_length_completion_message_passes() -> None:
    """A 1024-char completion message is valid (regression: the cap was wrongly 512)."""
    assert_valid_activity_content(_mut(valid_countdown, completion_message="x" * 1024))


def test_max_total_steps_passes() -> None:
    """total_steps at the documented ceiling is accepted; one over is rejected."""
    assert_valid_activity_content(_mut(valid_steps, total_steps=64, current_step=64))


# --- targeted matches: a rejection must fire for the RIGHT reason -----------


def test_rejections_pin_the_offending_rule() -> None:
    with pytest.raises(PushWardContractError, match="total_steps must be"):
        assert_valid_activity_content(_mut(valid_steps, total_steps=65))
    with pytest.raises(PushWardContractError, match="warning_threshold"):
        assert_valid_activity_content(_mut(valid_countdown, warning_threshold=99999))
    with pytest.raises(PushWardContractError, match="snooze_seconds"):
        assert_valid_activity_content(_mut(valid_countdown, snooze_seconds=30))
    with pytest.raises(PushWardContractError, match="priority"):
        assert_valid_priority(True)
