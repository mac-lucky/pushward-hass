"""Shared test fixtures for PushWard integration tests."""

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.loader import DATA_CUSTOM_COMPONENTS

from custom_components.pushward.const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ACTIVITY_NAME,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_DECIMALS,
    CONF_END_STATES,
    CONF_ENDED_TTL,
    CONF_ENTITY_ID,
    CONF_HISTORY_PERIOD,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PRIORITY,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SCALE,
    CONF_SECONDARY_URL,
    CONF_SERIES,
    CONF_SEVERITY,
    CONF_SLUG,
    CONF_SMOOTHING,
    CONF_STALE_TTL,
    CONF_START_STATES,
    CONF_STATE_LABELS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_TEMPLATE,
    CONF_THRESHOLDS,
    CONF_TOTAL_STEPS,
    CONF_UNIT,
    CONF_UPDATE_INTERVAL,
    CONF_URL,
    CONF_VALUE_ATTRIBUTE,
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
        CONF_REMAINING_TIME_ATTR: "",
        CONF_SUBTITLE_ATTRIBUTE: "",
        CONF_STATE_LABELS: {},
        CONF_COMPLETION_MESSAGE: "",
        CONF_TOTAL_STEPS: 1,
        CONF_CURRENT_STEP_ATTR: "",
        CONF_SEVERITY: "info",
        CONF_VALUE_ATTRIBUTE: "",
        CONF_MIN_VALUE: 0.0,
        CONF_MAX_VALUE: 100.0,
        CONF_UNIT: "",
        CONF_ACCENT_COLOR: "",
        CONF_ACCENT_COLOR_ATTRIBUTE: "",
        CONF_URL: "",
        CONF_SECONDARY_URL: "",
        CONF_ENDED_TTL: None,
        CONF_STALE_TTL: None,
        CONF_SERIES: {},
        CONF_SCALE: "linear",
        CONF_DECIMALS: 1,
        CONF_SMOOTHING: False,
        CONF_THRESHOLDS: [],
        CONF_HISTORY_PERIOD: 0,
    }
    config.update(overrides)
    return config


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
