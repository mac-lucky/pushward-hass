"""Shared test fixtures for PushWard integration tests."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.loader import DATA_CUSTOM_COMPONENTS

from custom_components.pushward.const import (
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ACTIVITY_NAME,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_END_STATES,
    CONF_ENDED_TTL,
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_PRIORITY,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SECONDARY_URL,
    CONF_SEVERITY,
    CONF_SLUG,
    CONF_STALE_TTL,
    CONF_START_STATES,
    CONF_STATE_LABELS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_TEMPLATE,
    CONF_TOTAL_STEPS,
    CONF_UPDATE_INTERVAL,
    CONF_URL,
)


@pytest.fixture(autouse=True)
def enable_custom_integrations(hass: HomeAssistant) -> None:
    """Enable custom integrations defined in the test dir."""
    hass.data.pop(DATA_CUSTOM_COMPONENTS)


@pytest.fixture
def mock_api():
    """Create a mock PushWard API client."""
    with patch("custom_components.pushward.api.PushWardApiClient") as mock_cls:
        client = mock_cls.return_value
        client.validate_connection = AsyncMock(return_value=True)
        client.create_activity = AsyncMock()
        client.update_activity = AsyncMock()
        client.delete_activity = AsyncMock()
        yield client


@pytest.fixture
def sample_entity_config():
    """Return a sample entity configuration dict."""
    return {
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
        CONF_ACCENT_COLOR: "",
        CONF_ACCENT_COLOR_ATTRIBUTE: "",
        CONF_URL: "",
        CONF_SECONDARY_URL: "",
        CONF_ENDED_TTL: None,
        CONF_STALE_TTL: None,
    }
