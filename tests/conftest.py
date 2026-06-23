"""Shared test fixtures for PushWard integration tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.loader import DATA_CUSTOM_COMPONENTS

from custom_components.pushward.activity_manager import ActivityManager
from custom_components.pushward.api import PushWardApiClient
from custom_components.pushward.const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ACTIVITY_NAME,
    CONF_ALARM,
    CONF_BACKGROUND_COLOR,
    CONF_BACKGROUND_COLOR_ATTRIBUTE,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_CURRENT_STEP_ENTITY,
    CONF_DECIMALS,
    CONF_END_STATES,
    CONF_ENDED_TTL,
    CONF_ENTITY_ID,
    CONF_FIRED_AT_ATTRIBUTE,
    CONF_FIRED_AT_ENTITY,
    CONF_HISTORY_PERIOD,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_LABEL,
    CONF_LABEL_ATTRIBUTE,
    CONF_LOG_LEVEL_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PRIORITY,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ATTR,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SCALE,
    CONF_SECONDARY_URL,
    CONF_SECONDARY_URL_FOREGROUND,
    CONF_SECONDARY_URL_TITLE,
    CONF_SERIES,
    CONF_SEVERITY,
    CONF_SLUG,
    CONF_SMOOTHING,
    CONF_SOUND,
    CONF_STALE_TTL,
    CONF_START_STATES,
    CONF_STAT_ROWS,
    CONF_STATE_LABELS,
    CONF_STEP_LABELS,
    CONF_STEP_ROWS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_SUBTITLE_ENTITY,
    CONF_TAP_ACTION_FOREGROUND,
    CONF_TAP_ACTION_URL,
    CONF_TEMPLATE,
    CONF_TEXT_COLOR,
    CONF_TEXT_COLOR_ATTRIBUTE,
    CONF_THRESHOLDS,
    CONF_TILES,
    CONF_TOTAL_STEPS,
    CONF_UNIT,
    CONF_UNITS,
    CONF_UPDATE_INTERVAL,
    CONF_URL,
    CONF_URL_FOREGROUND,
    CONF_URL_TITLE,
    CONF_VALUE_ATTRIBUTE,
    CONF_VALUE_ENTITY,
    CONF_WARNING_THRESHOLD,
    CONF_WIDGET_NAME,
    CONF_WIDGET_POLL_INTERVAL,
    CONF_WIDGET_TEMPLATE,
    CONF_WIDGET_TRIGGER_MODE,
    DEFAULT_TAP_ACTION_FOREGROUND,
    DEFAULT_WIDGET_POLL_INTERVAL,
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TRIGGER_EVENT,
)


@pytest.fixture(autouse=True)
def enable_custom_integrations(hass: HomeAssistant) -> None:
    """Enable custom integrations defined in the test dir."""
    hass.data.pop(DATA_CUSTOM_COMPONENTS)


def make_entity_config(**overrides) -> dict:
    """Build a test entity configuration with sensible defaults."""
    config = {
        CONF_ENTITY_ID: "binary_sensor.washer",
        CONF_SLUG: "ha-washer",
        CONF_ACTIVITY_NAME: "Washer",
        CONF_ICON: "washer",
        CONF_ICON_ATTRIBUTE: "",
        CONF_PRIORITY: 1,
        CONF_TEMPLATE: "generic",
        CONF_START_STATES: ["on"],
        CONF_END_STATES: ["off"],
        CONF_UPDATE_INTERVAL: 5,
        CONF_PROGRESS_ATTRIBUTE: "",
        CONF_PROGRESS_ENTITY: "",
        CONF_REMAINING_TIME_ATTR: "",
        CONF_REMAINING_TIME_ENTITY: "",
        CONF_SUBTITLE_ATTRIBUTE: "",
        CONF_SUBTITLE_ENTITY: "",
        CONF_STATE_LABELS: {},
        CONF_COMPLETION_MESSAGE: "",
        CONF_TOTAL_STEPS: 1,
        CONF_CURRENT_STEP_ATTR: "",
        CONF_CURRENT_STEP_ENTITY: "",
        CONF_SEVERITY: "info",
        CONF_VALUE_ATTRIBUTE: "",
        CONF_VALUE_ENTITY: "",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
        CONF_UNIT: "",
        CONF_ACCENT_COLOR: "",
        CONF_ACCENT_COLOR_ATTRIBUTE: "",
        CONF_URL: "",
        CONF_URL_FOREGROUND: DEFAULT_TAP_ACTION_FOREGROUND,
        CONF_URL_TITLE: "",
        CONF_SECONDARY_URL: "",
        CONF_SECONDARY_URL_FOREGROUND: DEFAULT_TAP_ACTION_FOREGROUND,
        CONF_SECONDARY_URL_TITLE: "",
        CONF_TAP_ACTION_URL: "",
        CONF_TAP_ACTION_FOREGROUND: DEFAULT_TAP_ACTION_FOREGROUND,
        CONF_ENDED_TTL: None,
        CONF_STALE_TTL: None,
        CONF_SERIES: {},
        CONF_SCALE: "linear",
        CONF_DECIMALS: 1,
        CONF_SMOOTHING: False,
        CONF_THRESHOLDS: [],
        CONF_HISTORY_PERIOD: 0,
        CONF_SOUND: "",
        CONF_WARNING_THRESHOLD: None,
        CONF_ALARM: False,
        CONF_STEP_LABELS: {},
        CONF_STEP_ROWS: [],
        CONF_FIRED_AT_ATTRIBUTE: "",
        CONF_FIRED_AT_ENTITY: "",
        CONF_UNITS: {},
        CONF_BACKGROUND_COLOR: "",
        CONF_BACKGROUND_COLOR_ATTRIBUTE: "",
        CONF_TEXT_COLOR: "",
        CONF_TEXT_COLOR_ATTRIBUTE: "",
        CONF_TILES: [],
        CONF_LOG_LEVEL_ATTRIBUTE: "",
    }
    config.update(overrides)
    return config


def make_widget_config(**overrides) -> dict:
    """Build a test widget configuration with sensible defaults."""
    config = {
        CONF_ENTITY_ID: "sensor.users",
        CONF_SLUG: "ha-users",
        CONF_WIDGET_NAME: "Users",
        CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
        CONF_WIDGET_TRIGGER_MODE: WIDGET_TRIGGER_EVENT,
        CONF_WIDGET_POLL_INTERVAL: DEFAULT_WIDGET_POLL_INTERVAL,
        CONF_VALUE_ATTRIBUTE: "",
        CONF_UNIT: "",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
        CONF_SEVERITY: "",
        CONF_STAT_ROWS: [],
        CONF_LABEL: "",
        CONF_LABEL_ATTRIBUTE: "",
        CONF_SUBTITLE_ATTRIBUTE: "",
        CONF_ICON: "",
        CONF_ICON_ATTRIBUTE: "",
        CONF_ACCENT_COLOR: "",
        CONF_ACCENT_COLOR_ATTRIBUTE: "",
        CONF_BACKGROUND_COLOR: "",
        CONF_TEXT_COLOR: "",
        CONF_TAP_ACTION_URL: "",
        CONF_TAP_ACTION_FOREGROUND: DEFAULT_TAP_ACTION_FOREGROUND,
    }
    config.update(overrides)
    return config


def make_mock_response(
    status: int, *, text: str = "", headers: dict | None = None, json_body: dict | None = None
) -> AsyncMock:
    """Build a mock aiohttp response with the given status + body."""
    resp = AsyncMock()
    resp.status = status
    resp.ok = 200 <= status < 300
    resp.text = AsyncMock(return_value=text)
    resp.json = AsyncMock(return_value=json_body if json_body is not None else {})
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(), history=(), status=status, message=text
        )
    return resp


def make_usage_payload(**overrides) -> dict:
    """Build a representative free-tier ``GET /auth/me`` usage payload.

    For a premium-shaped payload use ``make_premium_usage_payload``.
    """
    payload = {
        "id": "user-123",
        "nickname": "Test",
        "activity_count": 2,
        "subscribed": False,
        "quota_period_month": 202606,
        "notifications_used": 137,
        "notifications_limit": 500,
        "live_activity_updates_used": 40,
        "live_activity_updates_limit": 250,
        "widget_updates_used": 8,
        "widget_updates_limit": 50,
        "emails_used": 3,
        "emails_limit": 500,
        "quota_resets_at": "2026-07-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def make_premium_usage_payload(**overrides) -> dict:
    """Build a representative premium ``GET /auth/me`` usage payload.

    Premium drops the uncapped Live Activity / widget limits and adds the
    daily-notification fields; the notifications counter is the daily cap.
    """
    payload = make_usage_payload(
        subscribed=True,
        notifications_used=12,
        notifications_limit=5000,
        notifications_used_month=420,
        quota_resets_day_at="2026-06-15T00:00:00Z",
    )
    payload.pop("live_activity_updates_limit", None)
    payload.pop("widget_updates_limit", None)
    payload.update(overrides)
    return payload


def make_mock_session(*responses: AsyncMock) -> AsyncMock:
    """Build a mock aiohttp.ClientSession returning `responses` in order."""
    session = AsyncMock(spec=aiohttp.ClientSession)
    ctx_managers = []
    for resp in responses:
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx_managers.append(cm)
    session.request = MagicMock(side_effect=ctx_managers)
    session.get = MagicMock()
    return session


def make_api_client(session: AsyncMock) -> PushWardApiClient:
    """Build a PushWardApiClient backed by the given mock session."""
    return PushWardApiClient(session, "https://api.example.com", "test-key")


def make_mock_state(state: str, attributes: dict | None = None, entity_id: str | None = None) -> MagicMock:
    """Create a mock HA State object."""
    mock = MagicMock()
    mock.state = state
    mock.attributes = attributes or {}
    if entity_id is not None:
        mock.entity_id = entity_id
        mock.domain = entity_id.split(".")[0] if "." in entity_id else ""
    else:
        mock.domain = ""
    return mock


def make_activity_api() -> AsyncMock:
    """Mock PushWard API client exposing the activity CRUD coroutines."""
    api = AsyncMock()
    api.create_activity = AsyncMock()
    api.update_activity = AsyncMock()
    api.delete_activity = AsyncMock()
    return api


def make_widget_api() -> AsyncMock:
    """Mock PushWard API client exposing the widget CRUD coroutines."""
    api = AsyncMock()
    api.create_widget = AsyncMock()
    api.patch_widget = AsyncMock()
    return api


def make_mock_entry(entry_id: str = "test_entry") -> MagicMock:
    """Mock ConfigEntry with the attributes the managers touch."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.async_start_reauth = MagicMock()
    return entry


def activity_updates(api: AsyncMock, wire_state: str) -> list[dict]:
    """Return the content dicts sent to ``update_activity`` for one wire state.

    ``update_activity(slug, wire_state, content, ...)`` — positional arg 1 is the
    ONGOING/ENDED wire state, arg 2 the content payload.
    """
    return [
        call.args[2]
        for call in api.update_activity.call_args_list
        if len(call.args) >= 3 and call.args[1] == wire_state
    ]


async def bump_state(
    manager: ActivityManager,
    hass: HomeAssistant,
    tracked_entity_id: str,
    set_entity_id: str,
    value: str,
    attrs: dict,
) -> None:
    """Force a throttled update through the real subscription path.

    Resetting the tracked entity's ``last_sent_at`` makes the cooldown read as
    elapsed, so a state change on ``set_entity_id`` (the tracked entity itself or a
    companion) sends immediately instead of arming a timer.
    """
    manager._tracked[tracked_entity_id].last_sent_at = 0.0
    hass.states.async_set(set_entity_id, value, attrs)
    await hass.async_block_till_done()


async def end_activity_via_state(
    manager: ActivityManager,
    hass: HomeAssistant,
    entity_id: str,
    end_state: str,
    attrs: dict,
) -> None:
    """Drive an entity into an end state and run the two-phase end deterministically.

    Goes through the real ``_async_on_state_change`` → ``_schedule_end`` path with
    ``END_DELAY_SECONDS`` collapsed to 0 so the ENDED push lands without a 5 s wait.
    """
    with patch("custom_components.pushward.activity_manager.END_DELAY_SECONDS", 0):
        hass.states.async_set(entity_id, end_state, attrs)
        await hass.async_block_till_done()
        end_task = manager._tracked[entity_id].end_task
        if end_task is not None:
            await end_task
        await hass.async_block_till_done()
