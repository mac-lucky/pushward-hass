"""Shared test fixtures for PushWard integration tests."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.pushward.const import (
    CONF_ACCENT_COLOR,
    CONF_ACTIVITY_NAME,
    CONF_END_STATES,
    CONF_ENTITIES,
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_INTEGRATION_KEY,
    CONF_PRIORITY,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SERVER_URL,
    CONF_SLUG,
    CONF_START_STATES,
    CONF_TEMPLATE,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)


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
        CONF_PRIORITY: 1,
        CONF_TEMPLATE: "generic",
        CONF_START_STATES: ["on"],
        CONF_END_STATES: ["off"],
        CONF_UPDATE_INTERVAL: 5,
        CONF_PROGRESS_ATTRIBUTE: "",
        CONF_REMAINING_TIME_ATTR: "",
        CONF_ACCENT_COLOR: "",
    }
