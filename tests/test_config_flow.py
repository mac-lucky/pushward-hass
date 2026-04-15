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
from custom_components.pushward.config_flow import (
    _hex_to_rgb,
    _parse_csv,
    _parse_state_labels,
    _parse_thresholds,
    _rgb_to_hex,
    _serialize_thresholds,
    _validate_integration_key,
)
from custom_components.pushward.const import (
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ACTIVITY_NAME,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_DECIMALS,
    CONF_END_STATES,
    CONF_ENTITY_ID,
    CONF_HISTORY_PERIOD,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_INTEGRATION_KEY,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PRIORITY,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SCALE,
    CONF_SECONDARY_URL,
    CONF_SERIES,
    CONF_SERVER_URL,
    CONF_SEVERITY,
    CONF_SLUG,
    CONF_SMOOTHING,
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
    DEFAULT_SERVER_URL,
    DOMAIN,
    SUBENTRY_TYPE_ENTITY,
    validate_url,
)

from .conftest import make_entity_config

MOCK_INTEGRATION_KEY = "test-key-123"


def _mock_core_input(**overrides) -> dict:
    """Build step-1 (entity + template) form input."""
    data = {
        CONF_ENTITY_ID: "binary_sensor.washer",
        CONF_TEMPLATE: "generic",
    }
    data.update(overrides)
    return data


def _mock_details_input(template: str = "generic", **overrides) -> dict:
    """Build step-2 (details) form input based on template."""
    data: dict = {}

    # States (list format from SelectSelector)
    data[CONF_START_STATES] = ["on"]
    data[CONF_END_STATES] = ["off"]

    # Template-specific fields
    if template in ("generic", "steps"):
        data[CONF_PROGRESS_ATTRIBUTE] = ""
    if template in ("generic", "countdown"):
        data[CONF_REMAINING_TIME_ATTR] = ""
    if template == "steps":
        data[CONF_TOTAL_STEPS] = 1
        data[CONF_CURRENT_STEP_ATTR] = ""
    if template == "alert":
        data[CONF_SEVERITY] = "info"
    if template == "gauge":
        data[CONF_VALUE_ATTRIBUTE] = ""
        data[CONF_MIN_VALUE] = 0.0
        data[CONF_MAX_VALUE] = 100.0
        data[CONF_UNIT] = ""
    if template == "timeline":
        data[CONF_SERIES] = ""
        data[CONF_VALUE_ATTRIBUTE] = ""
        data[CONF_UNIT] = ""
        data[CONF_SCALE] = "linear"
        data[CONF_DECIMALS] = 1
        data[CONF_SMOOTHING] = False
        data[CONF_THRESHOLDS] = ""
        data[CONF_HISTORY_PERIOD] = 0

    # Identity fields
    data[CONF_SLUG] = "ha-washer"
    data[CONF_ACTIVITY_NAME] = "Washer"
    data[CONF_ICON] = "circle.fill"
    data[CONF_ICON_ATTRIBUTE] = ""
    data[CONF_PRIORITY] = 1
    data[CONF_UPDATE_INTERVAL] = 5

    # Common optional fields
    data[CONF_SUBTITLE_ATTRIBUTE] = ""
    data[CONF_STATE_LABELS] = ""
    if template == "countdown":
        data[CONF_COMPLETION_MESSAGE] = ""
    data[CONF_ACCENT_COLOR_ATTRIBUTE] = ""
    if template in ("steps", "alert"):
        data[CONF_URL] = ""
        data[CONF_SECONDARY_URL] = ""

    data.update(overrides)
    return data


def _entity_subentry_data(**overrides) -> ConfigSubentryData:
    """Build a ConfigSubentryData for pre-loading subentries."""
    data = make_entity_config(**{CONF_ICON: "circle.fill", **overrides})
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
            CONF_SERVER_URL: DEFAULT_SERVER_URL,
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
        "version": 2,
        "unique_id": DOMAIN,
    }
    defaults.update(kwargs)
    return MockConfigEntry(**defaults)


async def _add_entity_subentry(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    core_overrides: dict | None = None,
    details_overrides: dict | None = None,
    template: str = "generic",
) -> dict:
    """Run both steps of the entity subentry add flow."""
    core = _mock_core_input(**{CONF_TEMPLATE: template, **(core_overrides or {})})
    details = _mock_details_input(template, **(details_overrides or {}))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    # Step 1 → Step 2
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=core,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "details"

    # Step 2 → Create
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=details,
    )
    return result


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
            CONF_INTEGRATION_KEY: MOCK_INTEGRATION_KEY,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "PushWard"
    assert result["data"] == {
        CONF_SERVER_URL: DEFAULT_SERVER_URL,
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
    """Test successful reconfiguration of integration key."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    new_key = "new-key-456"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_INTEGRATION_KEY: new_key,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SERVER_URL] == DEFAULT_SERVER_URL
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
                CONF_INTEGRATION_KEY: "bad-key",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


# --- Reauth flow tests ---


async def test_reauth_success(
    hass: HomeAssistant,
    mock_api_client,
) -> None:
    """Test successful reauthentication with a new key."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    new_key = "new-valid-key-789"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_INTEGRATION_KEY: new_key},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_INTEGRATION_KEY] == new_key
    assert entry.data[CONF_SERVER_URL] == DEFAULT_SERVER_URL


async def test_reauth_invalid_key(hass: HomeAssistant) -> None:
    """Test reauth with an invalid key shows error."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.pushward.config_flow.PushWardApiClient",
    ) as mock_cls:
        instance = mock_cls.return_value
        instance.validate_connection = AsyncMock(side_effect=PushWardAuthError("bad key"))

        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_INTEGRATION_KEY: "still-bad-key"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_cannot_connect(hass: HomeAssistant) -> None:
    """Test reauth when server is unreachable shows error."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.pushward.config_flow.PushWardApiClient",
    ) as mock_cls:
        instance = mock_cls.return_value
        instance.validate_connection = AsyncMock(side_effect=OSError("timeout"))

        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_INTEGRATION_KEY: "some-key"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


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
    """Test that validate_url accepts http and https URLs."""
    assert validate_url(url) == url


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
    """Test that validate_url rejects non-http/https schemes."""
    with pytest.raises(vol.Invalid):
        validate_url(url)


# --- _validate_integration_key helper tests ---


async def test_validate_integration_key_success(hass: HomeAssistant, mock_api_client) -> None:
    """Successful validation returns empty errors dict."""
    errors = await _validate_integration_key(hass, "valid-key", "test")
    assert errors == {}


async def test_validate_integration_key_auth_error(hass: HomeAssistant) -> None:
    """Auth error returns invalid_auth."""
    with patch(
        "custom_components.pushward.config_flow.PushWardApiClient",
    ) as mock_cls:
        mock_cls.return_value.validate_connection = AsyncMock(side_effect=PushWardAuthError("bad key"))
        errors = await _validate_integration_key(hass, "bad-key", "test")
    assert errors == {"base": "invalid_auth"}


async def test_validate_integration_key_unexpected_error(hass: HomeAssistant) -> None:
    """Unexpected error returns cannot_connect."""
    with patch(
        "custom_components.pushward.config_flow.PushWardApiClient",
    ) as mock_cls:
        mock_cls.return_value.validate_connection = AsyncMock(side_effect=OSError("timeout"))
        errors = await _validate_integration_key(hass, "some-key", "test")
    assert errors == {"base": "cannot_connect"}


# --- Subentry flow tests (add entity — two-step) ---


async def test_subentry_add_entity(hass: HomeAssistant) -> None:
    """Test adding an entity through the two-step subentry flow."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(hass, entry)
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

    result = await _add_entity_subentry(hass, entry, details_overrides={CONF_SLUG: "My--Slug!@#$%"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_SLUG] == "my-slug"


async def test_subentry_add_entity_empty_slug_auto_generates(hass: HomeAssistant) -> None:
    """Test that empty slug falls back to auto-generated slug."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(hass, entry, details_overrides={CONF_SLUG: ""})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_SLUG] == "ha-binary_sensor-washer"


async def test_subentry_add_steps_entity(hass: HomeAssistant) -> None:
    """Test adding a steps entity persists total_steps and current_step_attribute."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(
        hass,
        entry,
        template="steps",
        details_overrides={CONF_TOTAL_STEPS: 5, CONF_CURRENT_STEP_ATTR: "step"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_TEMPLATE] == "steps"
    assert subentries[0].data[CONF_TOTAL_STEPS] == 5
    assert subentries[0].data[CONF_CURRENT_STEP_ATTR] == "step"


async def test_subentry_add_alert_entity(hass: HomeAssistant) -> None:
    """Test adding an alert entity persists severity."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(
        hass,
        entry,
        template="alert",
        details_overrides={CONF_SEVERITY: "critical"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_TEMPLATE] == "alert"
    assert subentries[0].data[CONF_SEVERITY] == "critical"


async def test_subentry_add_countdown_entity(hass: HomeAssistant) -> None:
    """Test adding a countdown entity — no steps/alert fields shown."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(
        hass,
        entry,
        template="countdown",
        details_overrides={CONF_REMAINING_TIME_ATTR: "remaining"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_TEMPLATE] == "countdown"
    assert subentries[0].data[CONF_REMAINING_TIME_ATTR] == "remaining"
    # Steps/alert fields should get defaults (not in the form)
    assert subentries[0].data[CONF_TOTAL_STEPS] == 1
    assert subentries[0].data[CONF_SEVERITY] == "info"


async def test_subentry_generic_hides_steps_alert_fields(hass: HomeAssistant) -> None:
    """Test that generic template doesn't include steps/alert fields in step 2."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    # Step 1
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "generic"}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "details"

    # Check which fields are in the schema
    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert CONF_PROGRESS_ATTRIBUTE in schema_keys
    assert CONF_REMAINING_TIME_ATTR in schema_keys
    assert CONF_TOTAL_STEPS not in schema_keys
    assert CONF_CURRENT_STEP_ATTR not in schema_keys
    assert CONF_SEVERITY not in schema_keys


async def test_subentry_steps_shows_steps_fields(hass: HomeAssistant) -> None:
    """Test that steps template shows total_steps and current_step_attribute."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "steps"}),
    )
    assert result["step_id"] == "details"

    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert CONF_TOTAL_STEPS in schema_keys
    assert CONF_CURRENT_STEP_ATTR in schema_keys
    assert CONF_PROGRESS_ATTRIBUTE in schema_keys
    assert CONF_REMAINING_TIME_ATTR not in schema_keys
    assert CONF_SEVERITY not in schema_keys


async def test_subentry_alert_shows_severity(hass: HomeAssistant) -> None:
    """Test that alert template shows severity but not steps fields."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "alert"}),
    )
    assert result["step_id"] == "details"

    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert CONF_SEVERITY in schema_keys
    assert CONF_TOTAL_STEPS not in schema_keys
    assert CONF_CURRENT_STEP_ATTR not in schema_keys
    assert CONF_PROGRESS_ATTRIBUTE not in schema_keys
    assert CONF_REMAINING_TIME_ATTR not in schema_keys


async def test_subentry_countdown_shows_remaining_time(hass: HomeAssistant) -> None:
    """Test that countdown template shows remaining_time but not steps/alert fields."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "countdown"}),
    )
    assert result["step_id"] == "details"

    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert CONF_REMAINING_TIME_ATTR in schema_keys
    assert CONF_PROGRESS_ATTRIBUTE not in schema_keys
    assert CONF_TOTAL_STEPS not in schema_keys
    assert CONF_SEVERITY not in schema_keys


async def test_subentry_add_gauge_entity(hass: HomeAssistant) -> None:
    """Test adding a gauge entity persists min/max/unit/value_attribute."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(
        hass,
        entry,
        template="gauge",
        details_overrides={
            CONF_VALUE_ATTRIBUTE: "temperature",
            CONF_MIN_VALUE: -10.0,
            CONF_MAX_VALUE: 50.0,
            CONF_UNIT: "\u00b0C",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_TEMPLATE] == "gauge"
    assert subentries[0].data[CONF_MIN_VALUE] == -10.0
    assert subentries[0].data[CONF_MAX_VALUE] == 50.0
    assert subentries[0].data[CONF_UNIT] == "\u00b0C"
    assert subentries[0].data[CONF_VALUE_ATTRIBUTE] == "temperature"


async def test_subentry_gauge_rejects_min_gte_max(hass: HomeAssistant) -> None:
    """Test that gauge rejects min_value >= max_value and re-shows the form."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "gauge"}),
    )
    assert result["step_id"] == "details"

    # Submit with min >= max
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_details_input("gauge", **{CONF_MIN_VALUE: 100.0, CONF_MAX_VALUE: 50.0}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "details"
    assert CONF_MIN_VALUE in result["errors"]


async def test_subentry_gauge_shows_gauge_fields(hass: HomeAssistant) -> None:
    """Test that gauge template shows value/min/max/unit but not steps/alert fields."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "gauge"}),
    )
    assert result["step_id"] == "details"

    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert CONF_VALUE_ATTRIBUTE in schema_keys
    assert CONF_MIN_VALUE in schema_keys
    assert CONF_MAX_VALUE in schema_keys
    assert CONF_UNIT in schema_keys
    # Should NOT show other template fields
    assert CONF_TOTAL_STEPS not in schema_keys
    assert CONF_CURRENT_STEP_ATTR not in schema_keys
    assert CONF_SEVERITY not in schema_keys
    assert CONF_PROGRESS_ATTRIBUTE not in schema_keys
    assert CONF_REMAINING_TIME_ATTR not in schema_keys


async def test_subentry_duplicate_entity_aborts(hass: HomeAssistant) -> None:
    """Test that adding the same entity twice is aborted."""
    entry = _mock_entry(subentries_data=[_entity_subentry_data()])
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- Subentry reconfigure flow tests (two-step) ---


async def test_subentry_reconfigure(hass: HomeAssistant) -> None:
    """Test reconfiguring an existing entity subentry (two-step)."""
    entry = _mock_entry(subentries_data=[_entity_subentry_data()])
    entry.add_to_hass(hass)

    subentry_id = next(iter(entry.subentries))

    # Step 1: entity + template
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "details"

    # Step 2: all details including name and priority changes
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_details_input("generic", **{CONF_ACTIVITY_NAME: "My Washer", CONF_PRIORITY: 5}),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    subentry = entry.subentries[subentry_id]
    assert subentry.data[CONF_ACTIVITY_NAME] == "My Washer"
    assert subentry.data[CONF_PRIORITY] == 5


# --- Template auto-suggestion tests ---


async def test_subentry_suggests_gauge_for_measurement_sensor(hass: HomeAssistant) -> None:
    """Submitting generic for a measurement sensor re-shows step 1 with gauge suggested."""
    entry = _mock_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(
        "sensor.temperature",
        "22.5",
        {"device_class": "temperature", "state_class": "measurement", "friendly_name": "Temp"},
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    # Submit step 1 with default generic
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_ENTITY_ID: "sensor.temperature", CONF_TEMPLATE: "generic"},
    )
    # Should re-show step 1 with gauge suggested
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    # Accept suggestion
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_ENTITY_ID: "sensor.temperature", CONF_TEMPLATE: "gauge"},
    )
    assert result["step_id"] == "details"


async def test_subentry_suggests_gauge_for_light(hass: HomeAssistant) -> None:
    """Light domain auto-suggests gauge template."""
    entry = _mock_entry()
    entry.add_to_hass(hass)
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Lamp", "brightness": 128})

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_ENTITY_ID: "light.lamp", CONF_TEMPLATE: "generic"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_subentry_suggests_countdown_for_timer(hass: HomeAssistant) -> None:
    """Timer domain auto-suggests countdown template."""
    entry = _mock_entry()
    entry.add_to_hass(hass)
    hass.states.async_set("timer.tea", "idle", {"friendly_name": "Tea Timer"})

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_ENTITY_ID: "timer.tea", CONF_TEMPLATE: "generic"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_subentry_no_suggestion_when_non_generic(hass: HomeAssistant) -> None:
    """User explicitly picking a non-generic template skips suggestion."""
    entry = _mock_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(
        "sensor.temperature",
        "22.5",
        {"device_class": "temperature", "state_class": "measurement"},
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    # User explicitly picks alert — should go straight to details
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_ENTITY_ID: "sensor.temperature", CONF_TEMPLATE: "alert"},
    )
    assert result["step_id"] == "details"


async def test_subentry_no_suggestion_for_binary_sensor(hass: HomeAssistant) -> None:
    """binary_sensor has no better suggestion — goes straight to details."""
    entry = _mock_entry()
    entry.add_to_hass(hass)
    hass.states.async_set("binary_sensor.door", "off", {"friendly_name": "Door"})

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_ENTITY_ID: "binary_sensor.door", CONF_TEMPLATE: "generic"},
    )
    assert result["step_id"] == "details"


async def test_subentry_suggestion_only_offered_once(hass: HomeAssistant) -> None:
    """User overrides suggestion back to generic — proceeds without loop."""
    entry = _mock_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(
        "sensor.temp",
        "22.5",
        {"device_class": "temperature", "state_class": "measurement"},
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    # First submit: generic → re-show with gauge
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_ENTITY_ID: "sensor.temp", CONF_TEMPLATE: "generic"},
    )
    assert result["step_id"] == "user"

    # Second submit: override back to generic → should proceed (no infinite loop)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_ENTITY_ID: "sensor.temp", CONF_TEMPLATE: "generic"},
    )
    assert result["step_id"] == "details"


# --- State labels parsing ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("heating=Warming Up, cooling=Cooling Down", {"heating": "Warming Up", "cooling": "Cooling Down"}),
        ("on=Active", {"on": "Active"}),
        ("", {}),
        ("bad format, no equals", {}),
        ("a=1, =empty_key, c=3", {"a": "1", "c": "3"}),
    ],
)
def test_parse_state_labels(raw: str, expected: dict) -> None:
    """Test _parse_state_labels parses various inputs correctly."""
    assert _parse_state_labels(raw) == expected


@pytest.mark.parametrize(
    ("hex_color", "expected"),
    [
        ("#ff8040", [255, 128, 64]),
        ("#000000", [0, 0, 0]),
        ("#ffffff", [255, 255, 255]),
        ("", None),
        ("#xyz", None),
        ("ff8040", None),
        ("#ff80", None),
    ],
)
def test_hex_to_rgb(hex_color: str, expected: list[int] | None) -> None:
    """Test _hex_to_rgb converts hex strings to RGB lists."""
    assert _hex_to_rgb(hex_color) == expected


@pytest.mark.parametrize(
    ("rgb", "expected"),
    [
        ([255, 128, 64], "#ff8040"),
        ([0, 0, 0], "#000000"),
        (None, ""),
        ([1, 2], ""),
        ("not a list", ""),
    ],
)
def test_rgb_to_hex(rgb: list[int] | None, expected: str) -> None:
    """Test _rgb_to_hex converts RGB lists to hex strings."""
    assert _rgb_to_hex(rgb) == expected


@pytest.mark.parametrize(
    ("csv_str", "expected"),
    [
        ("a, b, c", ["a", "b", "c"]),
        ("", []),
        ("  one  ,  two  ", ["one", "two"]),
        (",,,", []),
        ("single", ["single"]),
    ],
)
def test_parse_csv(csv_str: str, expected: list[str]) -> None:
    """Test _parse_csv splits and strips comma-separated values."""
    assert _parse_csv(csv_str) == expected


# --- New field tests (two-step) ---


async def test_subentry_add_entity_with_state_labels(hass: HomeAssistant) -> None:
    """State labels are parsed from CSV and stored as dict."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(
        hass, entry, details_overrides={CONF_STATE_LABELS: "heating=Warming Up, idle=Standby"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_STATE_LABELS] == {"heating": "Warming Up", "idle": "Standby"}


async def test_subentry_add_entity_with_subtitle_attribute(hass: HomeAssistant) -> None:
    """Subtitle attribute is stored in subentry data."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(hass, entry, details_overrides={CONF_SUBTITLE_ATTRIBUTE: "media_title"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_SUBTITLE_ATTRIBUTE] == "media_title"


async def test_subentry_add_entity_with_completion_message(hass: HomeAssistant) -> None:
    """Completion message is stored in subentry data (countdown template only)."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(
        hass,
        entry,
        template="countdown",
        details_overrides={CONF_COMPLETION_MESSAGE: "Wash Done"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_COMPLETION_MESSAGE] == "Wash Done"


async def test_subentry_completion_message_only_for_countdown(hass: HomeAssistant) -> None:
    """completion_message field appears in schema only for countdown template."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    for template, expected in [
        ("generic", False),
        ("countdown", True),
        ("steps", False),
        ("alert", False),
        ("gauge", False),
        ("timeline", False),
    ]:
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, SUBENTRY_TYPE_ENTITY),
            context={"source": config_entries.SOURCE_USER},
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input=_mock_core_input(**{CONF_TEMPLATE: template}),
        )
        assert result["step_id"] == "details"
        schema_keys = {str(k) for k in result["data_schema"].schema}
        assert (CONF_COMPLETION_MESSAGE in schema_keys) is expected, (
            f"template={template} expected completion_message={expected} but got keys={schema_keys}"
        )


async def test_subentry_add_entity_with_urls(hass: HomeAssistant) -> None:
    """URLs are validated and stored."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(
        hass,
        entry,
        template="alert",
        details_overrides={
            CONF_URL: "https://ha.local/lovelace/laundry",
            CONF_SECONDARY_URL: "https://ha.local/lovelace/overview",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_URL] == "https://ha.local/lovelace/laundry"
    assert subentries[0].data[CONF_SECONDARY_URL] == "https://ha.local/lovelace/overview"


async def test_subentry_add_entity_with_icon_attribute(hass: HomeAssistant) -> None:
    """Icon attribute is stored."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(hass, entry, details_overrides={CONF_ICON_ATTRIBUTE: "sf_symbol"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_ICON_ATTRIBUTE] == "sf_symbol"


async def test_subentry_reconfigure_clearing_attribute_selectors(hass: HomeAssistant) -> None:
    """Clearing attribute selectors during reconfigure saves empty strings.

    When a user presses X on an AttributeSelector, HA omits the key from
    the form submission. The old code used vol.Optional(default=old_value)
    which re-filled the old value. The fix uses suggested_value so clearing
    actually persists.
    """
    # Start with attribute selectors populated
    entry = _mock_entry(
        subentries_data=[
            _entity_subentry_data(
                icon_attribute="rgb_color",
                subtitle_attribute="xy_color",
                progress_attribute="brightness",
                accent_color_attribute="hs_color",
            )
        ]
    )
    entry.add_to_hass(hass)

    subentry_id = next(iter(entry.subentries))

    # Verify precondition: attributes are set
    assert entry.subentries[subentry_id].data[CONF_ICON_ATTRIBUTE] == "rgb_color"
    assert entry.subentries[subentry_id].data[CONF_SUBTITLE_ATTRIBUTE] == "xy_color"
    assert entry.subentries[subentry_id].data[CONF_PROGRESS_ATTRIBUTE] == "brightness"
    assert entry.subentries[subentry_id].data[CONF_ACCENT_COLOR_ATTRIBUTE] == "hs_color"

    # Step 1: reconfigure
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(),
    )
    assert result["step_id"] == "details"

    # Step 2: submit WITHOUT attribute selector keys (simulates clearing via X)
    details = _mock_details_input("generic")
    del details[CONF_ICON_ATTRIBUTE]
    del details[CONF_SUBTITLE_ATTRIBUTE]
    del details[CONF_PROGRESS_ATTRIBUTE]
    del details[CONF_ACCENT_COLOR_ATTRIBUTE]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=details,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    # All cleared selectors should be empty strings
    data = entry.subentries[subentry_id].data
    assert data[CONF_ICON_ATTRIBUTE] == ""
    assert data[CONF_SUBTITLE_ATTRIBUTE] == ""
    assert data[CONF_PROGRESS_ATTRIBUTE] == ""
    assert data[CONF_ACCENT_COLOR_ATTRIBUTE] == ""


async def test_subentry_reconfigure_clearing_remaining_time_attr(hass: HomeAssistant) -> None:
    """Clearing remaining_time_attribute during reconfigure saves empty string."""
    entry = _mock_entry(
        subentries_data=[
            _entity_subentry_data(
                template="countdown",
                remaining_time_attribute="remaining",
            )
        ]
    )
    entry.add_to_hass(hass)

    subentry_id = next(iter(entry.subentries))
    assert entry.subentries[subentry_id].data[CONF_REMAINING_TIME_ATTR] == "remaining"

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "countdown"}),
    )
    assert result["step_id"] == "details"

    details = _mock_details_input("countdown")
    del details[CONF_REMAINING_TIME_ATTR]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=details,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data[CONF_REMAINING_TIME_ATTR] == ""


async def test_subentry_reconfigure_clearing_steps_attrs(hass: HomeAssistant) -> None:
    """Clearing steps attribute selectors during reconfigure saves empty strings."""
    entry = _mock_entry(
        subentries_data=[
            _entity_subentry_data(
                template="steps",
                progress_attribute="percent",
                current_step_attribute="step",
            )
        ]
    )
    entry.add_to_hass(hass)

    subentry_id = next(iter(entry.subentries))
    assert entry.subentries[subentry_id].data[CONF_PROGRESS_ATTRIBUTE] == "percent"
    assert entry.subentries[subentry_id].data[CONF_CURRENT_STEP_ATTR] == "step"

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "steps"}),
    )
    assert result["step_id"] == "details"

    details = _mock_details_input("steps")
    del details[CONF_PROGRESS_ATTRIBUTE]
    del details[CONF_CURRENT_STEP_ATTR]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=details,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    data = entry.subentries[subentry_id].data
    assert data[CONF_PROGRESS_ATTRIBUTE] == ""
    assert data[CONF_CURRENT_STEP_ATTR] == ""


async def test_subentry_rejects_invalid_url(hass: HomeAssistant) -> None:
    """Invalid URL scheme shows error on step 2."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    # Step 1
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "alert"}),
    )
    assert result["step_id"] == "details"

    # Step 2 with invalid URL
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_details_input("alert", **{CONF_URL: "ftp://evil.example.com"}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_URL: "invalid_url"}


async def test_subentry_rejects_invalid_secondary_url(hass: HomeAssistant) -> None:
    """Invalid secondary URL scheme shows error on correct field."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    # Step 1
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "alert"}),
    )
    assert result["step_id"] == "details"

    # Step 2 with invalid secondary URL
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_details_input("alert", **{CONF_SECONDARY_URL: "ftp://evil.example.com"}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SECONDARY_URL: "invalid_url"}


# --- _parse_thresholds / _serialize_thresholds ---


def test_parse_thresholds_full():
    """Parse threshold entries with value, color, and label."""
    result = _parse_thresholds("25:red:Hot, 18:blue:Cold")
    assert result == [
        {"value": 25.0, "color": "red", "label": "Hot"},
        {"value": 18.0, "color": "blue", "label": "Cold"},
    ]


def test_parse_thresholds_value_only():
    """Parse threshold with value only (no color/label)."""
    result = _parse_thresholds("20")
    assert result == [{"value": 20.0}]


def test_parse_thresholds_value_and_color():
    """Parse threshold with value and color but no label."""
    result = _parse_thresholds("25:red")
    assert result == [{"value": 25.0, "color": "red"}]


def test_parse_thresholds_empty():
    """Empty string returns empty list."""
    assert _parse_thresholds("") == []


def test_parse_thresholds_invalid_value():
    """Non-numeric value is skipped."""
    result = _parse_thresholds("abc:red:Hot, 18:blue:Cold")
    assert result == [{"value": 18.0, "color": "blue", "label": "Cold"}]


def test_parse_thresholds_max_five():
    """More than 5 thresholds are truncated."""
    result = _parse_thresholds("1, 2, 3, 4, 5, 6, 7")
    assert len(result) == 5


def test_serialize_thresholds_roundtrip():
    """Serialization produces parseable output."""
    original = [
        {"value": 25.0, "color": "red", "label": "Hot"},
        {"value": 18.0, "color": "blue"},
        {"value": 20.0},
    ]
    serialized = _serialize_thresholds(original)
    roundtrip = _parse_thresholds(serialized)
    assert roundtrip == original


def test_serialize_thresholds_empty():
    """Empty list serializes to empty string."""
    assert _serialize_thresholds([]) == ""


# --- timeline subentry flow ---


async def test_subentry_timeline_template(hass: HomeAssistant) -> None:
    """Timeline template subentry completes successfully."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    # Step 1
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_ENTITY_ID: "sensor.temperature", CONF_TEMPLATE: "timeline"}),
    )
    assert result["step_id"] == "details"

    # Step 2 with timeline fields
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_details_input(
            "timeline",
            **{
                CONF_SERIES: "current_temperature=Current, target_temperature=Target",
                CONF_UNIT: "\u00b0C",
                CONF_SCALE: "logarithmic",
                CONF_DECIMALS: 2,
                CONF_SMOOTHING: True,
                CONF_THRESHOLDS: "25:red:Hot",
                CONF_HISTORY_PERIOD: 6,
            },
        ),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentries = list(entry.subentries.values())
    assert len(subentries) == 1
    subentry_data = subentries[0].data
    assert subentry_data[CONF_TEMPLATE] == "timeline"
    assert subentry_data[CONF_SERIES] == {
        "current_temperature": "Current",
        "target_temperature": "Target",
    }
    assert subentry_data[CONF_UNIT] == "\u00b0C"
    assert subentry_data[CONF_SCALE] == "logarithmic"
    assert subentry_data[CONF_DECIMALS] == 2
    assert subentry_data[CONF_SMOOTHING] is True
    assert subentry_data[CONF_THRESHOLDS] == [{"value": 25.0, "color": "red", "label": "Hot"}]
    assert subentry_data[CONF_HISTORY_PERIOD] == 6
