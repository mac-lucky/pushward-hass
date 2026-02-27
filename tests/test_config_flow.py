"""Tests for PushWard config flow and subentry flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.api import PushWardAuthError
from custom_components.pushward.config_flow import _validate_url
from custom_components.pushward.const import (
    CONF_ACCENT_COLOR,
    CONF_ACTIVITY_NAME,
    CONF_END_STATES,
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
    SUBENTRY_TYPE_ENTITY,
)

MOCK_SERVER_URL = "https://pushward.example.com"
MOCK_INTEGRATION_KEY = "test-key-123"


def _mock_entity_input(**overrides) -> dict:
    """Build entity form input (raw strings, as user would type)."""
    data = {
        CONF_ENTITY_ID: "binary_sensor.washer",
        CONF_SLUG: "ha-washer",
        CONF_ACTIVITY_NAME: "Washer",
        CONF_ICON: "circle.fill",
        CONF_PRIORITY: 1,
        CONF_TEMPLATE: "generic",
        CONF_START_STATES: "on",
        CONF_END_STATES: "off",
        CONF_UPDATE_INTERVAL: 5,
        CONF_PROGRESS_ATTRIBUTE: "",
        CONF_REMAINING_TIME_ATTR: "",
    }
    data.update(overrides)
    return data


def _entity_subentry_data(**overrides) -> ConfigSubentryData:
    """Build a ConfigSubentryData for pre-loading subentries."""
    data = {
        CONF_ENTITY_ID: "binary_sensor.washer",
        CONF_SLUG: "ha-washer",
        CONF_ACTIVITY_NAME: "Washer",
        CONF_ICON: "circle.fill",
        CONF_PRIORITY: 1,
        CONF_TEMPLATE: "generic",
        CONF_START_STATES: ["on"],
        CONF_END_STATES: ["off"],
        CONF_UPDATE_INTERVAL: 5,
        CONF_PROGRESS_ATTRIBUTE: "",
        CONF_REMAINING_TIME_ATTR: "",
        CONF_ACCENT_COLOR: "",
    }
    data.update(overrides)
    return ConfigSubentryData(
        data=data,
        subentry_type=SUBENTRY_TYPE_ENTITY,
        title=data[CONF_ACTIVITY_NAME],
        unique_id=data[CONF_ENTITY_ID],
    )


def _mock_entry(**kwargs) -> MockConfigEntry:
    """Build a MockConfigEntry with sensible defaults."""
    defaults = {
        "domain": DOMAIN,
        "title": "PushWard",
        "data": {
            CONF_SERVER_URL: MOCK_SERVER_URL,
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
        "version": 2,
        "unique_id": DOMAIN,
    }
    defaults.update(kwargs)
    return MockConfigEntry(**defaults)


@pytest.fixture
def mock_api_client():
    """Mock PushWardApiClient with successful validate_connection."""
    with patch(
        "custom_components.pushward.config_flow.PushWardApiClient",
    ) as mock_cls:
        instance = mock_cls.return_value
        instance.validate_connection = AsyncMock(return_value=True)
        yield instance


@pytest.fixture(autouse=True)
def mock_session():
    """Mock async_get_clientsession for all tests."""
    with patch(
        "custom_components.pushward.config_flow.async_get_clientsession",
        return_value=MagicMock(),
    ):
        yield


@pytest.fixture(autouse=True)
def mock_setup_entry():
    """Prevent actual setup when config entry is created during tests."""
    with patch(
        "custom_components.pushward.async_setup_entry",
        return_value=True,
    ):
        yield


# --- Config flow tests ---


async def test_user_step_success(
    hass: HomeAssistant,
    mock_api_client,
) -> None:
    """Test successful user setup step."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SERVER_URL: MOCK_SERVER_URL,
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "PushWard"
    assert result["data"] == {
        CONF_SERVER_URL: MOCK_SERVER_URL,
        CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
    }
    mock_api_client.validate_connection.assert_awaited_once()


async def test_user_step_invalid_auth(hass: HomeAssistant) -> None:
    """Test user step with invalid auth."""
    with patch(
        "custom_components.pushward.config_flow.PushWardApiClient",
    ) as mock_cls:
        instance = mock_cls.return_value
        instance.validate_connection = AsyncMock(side_effect=PushWardAuthError("bad key"))

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_SERVER_URL: MOCK_SERVER_URL,
                CONF_INTEGRATION_KEY: "bad-key",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_step_cannot_connect(hass: HomeAssistant) -> None:
    """Test user step with connection failure."""
    with patch(
        "custom_components.pushward.config_flow.PushWardApiClient",
    ) as mock_cls:
        instance = mock_cls.return_value
        instance.validate_connection = AsyncMock(side_effect=OSError("timeout"))

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_SERVER_URL: MOCK_SERVER_URL,
                CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_already_configured(
    hass: HomeAssistant,
    mock_api_client,
) -> None:
    """Test abort when already configured."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- Reconfigure flow tests ---


async def test_reconfigure_success(
    hass: HomeAssistant,
    mock_api_client,
) -> None:
    """Test successful reconfiguration of server URL and key."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    new_url = "https://new.pushward.example.com"
    new_key = "new-key-456"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SERVER_URL: new_url,
            CONF_INTEGRATION_KEY: new_key,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SERVER_URL] == new_url
    assert entry.data[CONF_INTEGRATION_KEY] == new_key


async def test_reconfigure_invalid_auth(hass: HomeAssistant) -> None:
    """Test reconfigure with invalid auth shows error."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.pushward.config_flow.PushWardApiClient",
    ) as mock_cls:
        instance = mock_cls.return_value
        instance.validate_connection = AsyncMock(side_effect=PushWardAuthError("bad key"))

        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_SERVER_URL: MOCK_SERVER_URL,
                CONF_INTEGRATION_KEY: "bad-key",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


# --- URL validation tests ---


@pytest.mark.parametrize(
    "url",
    [
        "https://pushward.example.com",
        "http://192.168.1.100:8080",
        "http://localhost:8080",
    ],
)
def test_validate_url_accepts_http_https(url: str) -> None:
    """Test that _validate_url accepts http and https URLs."""
    assert _validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://evil.example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "gopher://evil.example.com",
        "not-a-url",
    ],
)
def test_validate_url_rejects_non_http_schemes(url: str) -> None:
    """Test that _validate_url rejects non-http/https schemes."""
    with pytest.raises(vol.Invalid):
        _validate_url(url)


async def test_user_step_rejects_non_http_url(
    hass: HomeAssistant,
    mock_api_client,
) -> None:
    """Test that the config flow rejects non-http URL schemes."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SERVER_URL: "ftp://evil.example.com",
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SERVER_URL: "invalid_url"}


# --- Subentry flow tests (add entity) ---


async def test_subentry_add_entity(hass: HomeAssistant) -> None:
    """Test adding an entity through subentry flow."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_entity_input(),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Washer"

    subentries = list(entry.subentries.values())
    assert len(subentries) == 1
    assert subentries[0].data[CONF_ENTITY_ID] == "binary_sensor.washer"
    assert subentries[0].data[CONF_SLUG] == "ha-washer"
    assert subentries[0].data[CONF_START_STATES] == ["on"]
    assert subentries[0].data[CONF_END_STATES] == ["off"]


async def test_subentry_add_entity_sanitizes_slug(hass: HomeAssistant) -> None:
    """Test that user-provided slugs are sanitized."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_entity_input(**{CONF_SLUG: "My--Slug!@#$%"}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_SLUG] == "my-slug"


async def test_subentry_add_entity_empty_slug_auto_generates(hass: HomeAssistant) -> None:
    """Test that empty slug falls back to auto-generated slug."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_entity_input(**{CONF_SLUG: ""}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_SLUG] == "ha-binary-sensor-washer"


async def test_subentry_duplicate_entity_aborts(hass: HomeAssistant) -> None:
    """Test that adding the same entity twice is aborted."""
    entry = _mock_entry(subentries_data=[_entity_subentry_data()])
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_entity_input(),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- Subentry reconfigure flow tests ---


async def test_subentry_reconfigure(hass: HomeAssistant) -> None:
    """Test reconfiguring an existing entity subentry."""
    entry = _mock_entry(subentries_data=[_entity_subentry_data()])
    entry.add_to_hass(hass)

    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_entity_input(**{CONF_ACTIVITY_NAME: "My Washer", CONF_PRIORITY: 5}),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    subentry = entry.subentries[subentry_id]
    assert subentry.data[CONF_ACTIVITY_NAME] == "My Washer"
    assert subentry.data[CONF_PRIORITY] == 5
