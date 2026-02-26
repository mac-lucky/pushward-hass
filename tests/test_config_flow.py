"""Tests for PushWard config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.api import PushWardAuthError
from custom_components.pushward.config_flow import _validate_url
from custom_components.pushward.const import (
    CONF_ACTIVITY_NAME,
    CONF_END_STATES,
    CONF_ENTITIES,
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_INTEGRATION_KEY,
    CONF_PRIORITY,
    CONF_SERVER_URL,
    CONF_SLUG,
    CONF_START_STATES,
    CONF_TEMPLATE,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
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
    }
    data.update(overrides)
    return data


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
    assert result["options"] == {CONF_ENTITIES: []}
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
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={
            CONF_SERVER_URL: MOCK_SERVER_URL,
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_menu(hass: HomeAssistant) -> None:
    """Test options flow shows menu with 3 options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={
            CONF_SERVER_URL: MOCK_SERVER_URL,
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
        options={CONF_ENTITIES: []},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["add_entity", "edit_entity", "remove_entity"]


async def test_add_entity(hass: HomeAssistant) -> None:
    """Test adding an entity through options flow."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={
            CONF_SERVER_URL: MOCK_SERVER_URL,
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
        options={CONF_ENTITIES: []},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "add_entity"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_entity"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_mock_entity_input(),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entities = result["data"][CONF_ENTITIES]
    assert len(entities) == 1
    assert entities[0][CONF_ENTITY_ID] == "binary_sensor.washer"
    assert entities[0][CONF_SLUG] == "ha-washer"
    # _parse_entity_input converts CSV strings to lists
    assert entities[0][CONF_START_STATES] == ["on"]
    assert entities[0][CONF_END_STATES] == ["off"]


async def test_remove_entity(hass: HomeAssistant) -> None:
    """Test removing an entity through options flow."""
    entity_config = {
        CONF_ENTITY_ID: "binary_sensor.washer",
        CONF_SLUG: "ha-washer",
        CONF_ACTIVITY_NAME: "Washer",
        CONF_ICON: "circle.fill",
        CONF_PRIORITY: 1,
        CONF_TEMPLATE: "generic",
        CONF_START_STATES: ["on"],
        CONF_END_STATES: ["off"],
        CONF_UPDATE_INTERVAL: 5,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={
            CONF_SERVER_URL: MOCK_SERVER_URL,
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
        options={CONF_ENTITIES: [entity_config]},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "remove_entity"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "remove_entity"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_ENTITY_ID: ["binary_sensor.washer"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENTITIES] == []


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
    with pytest.raises(vol.Invalid):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_SERVER_URL: "ftp://evil.example.com",
                CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
            },
        )


# --- Slug sanitization tests ---


async def test_add_entity_sanitizes_slug(hass: HomeAssistant) -> None:
    """Test that user-provided slugs are sanitized to safe characters."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={
            CONF_SERVER_URL: MOCK_SERVER_URL,
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
        options={CONF_ENTITIES: []},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "add_entity"},
    )

    # Slug with uppercase and special chars should be sanitized
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_mock_entity_input(**{CONF_SLUG: "My--Slug!@#$%"}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entities = result["data"][CONF_ENTITIES]
    # Uppercase → lowercase, special chars stripped, double hyphens collapsed
    assert entities[0][CONF_SLUG] == "my-slug"


async def test_add_entity_empty_slug_auto_generates(hass: HomeAssistant) -> None:
    """Test that empty slug falls back to auto-generated slug."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={
            CONF_SERVER_URL: MOCK_SERVER_URL,
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
        options={CONF_ENTITIES: []},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "add_entity"},
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_mock_entity_input(**{CONF_SLUG: ""}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entities = result["data"][CONF_ENTITIES]
    # Empty slug → auto-generated from entity_id "binary_sensor.washer"
    assert entities[0][CONF_SLUG] == "ha-binary-sensor-washer"
