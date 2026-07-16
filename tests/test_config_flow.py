"""Tests for PushWard config flow and subentry flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.api import PushWardAuthError
from custom_components.pushward.config_flow import (
    _details_schema,
    _hex_to_rgb,
    _parse_board_tiles,
    _parse_color_list,
    _parse_csv,
    _parse_entity_input,
    _parse_float_list,
    _parse_int_list,
    _parse_log_columns,
    _parse_series_entities,
    _parse_state_labels,
    _parse_thresholds,
    _parse_widget_input,
    _parse_widget_stat_rows,
    _resolve_series_entity_labels,
    _rgb_to_hex,
    _serialize_board_tiles,
    _serialize_csv,
    _serialize_float_list,
    _serialize_log_columns,
    _serialize_series_entities,
    _serialize_thresholds,
    _serialize_widget_stat_rows,
    _validate_integration_key,
    _widget_details_schema,
)
from custom_components.pushward.const import (
    BOARD_MAX_TILES,
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
    CONF_ENTITY_ID,
    CONF_FIRED_AT_ATTRIBUTE,
    CONF_FIRED_AT_ENTITY,
    CONF_HISTORY_PERIOD,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_INTEGRATION_KEY,
    CONF_LABEL,
    CONF_LIVE_PROGRESS,
    CONF_LOG_COLUMNS,
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
    CONF_SERIES_ENTITIES,
    CONF_SERVER_URL,
    CONF_SEVERITY,
    CONF_SEVERITY_LABEL,
    CONF_SLUG,
    CONF_SMOOTHING,
    CONF_SOUND,
    CONF_START_STATES,
    CONF_STAT_ROWS,
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
    CONF_VALUE_SCALE,
    CONF_WARNING_THRESHOLD,
    CONF_WIDGET_NAME,
    CONF_WIDGET_POLL_INTERVAL,
    CONF_WIDGET_TEMPLATE,
    CONF_WIDGET_TRIGGER_MODE,
    DEFAULT_SERVER_URL,
    DEFAULT_VALUE_SCALE,
    DEFAULT_WIDGET_POLL_INTERVAL,
    DOMAIN,
    LIVE_PROGRESS_TEMPLATES,
    LOG_MAX_COLUMNS,
    MAX_SEVERITY_LABEL_LEN,
    SUBENTRY_TYPE_ENTITY,
    SUBENTRY_TYPE_WIDGET,
    TIMELINE_MAX_SERIES,
    TIMELINE_SERIES_LABEL_MAX,
    VALUE_SCALE_PERCENT,
    WIDGET_MAX_STAT_ROWS,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_PROGRESS,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TRIGGER_EVENT,
    WIDGET_TRIGGER_POLL,
    validate_url,
)

from .conftest import make_entity_config, make_mock_state

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
    # live_progress animates the current step across start_date..end_date, which is
    # only derivable from a remaining-time source -- so steps must offer one.
    assert CONF_LIVE_PROGRESS in schema_keys
    assert CONF_REMAINING_TIME_ATTR in schema_keys
    assert CONF_SEVERITY not in schema_keys


@pytest.mark.parametrize("template", LIVE_PROGRESS_TEMPLATES)
def test_live_progress_templates_also_offer_a_remaining_time_source(template: str) -> None:
    """A live_progress toggle without a remaining-time source can never fire."""
    schema_keys = {str(k) for k in _details_schema("binary_sensor.washer", template, {}).schema}
    assert CONF_LIVE_PROGRESS in schema_keys
    assert CONF_REMAINING_TIME_ATTR in schema_keys
    assert CONF_REMAINING_TIME_ENTITY in schema_keys


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


async def test_subentry_reconfigure_prefills_step_weights_and_colors_as_text(hass: HomeAssistant) -> None:
    """Stored lists must render back as CSV text: the details field is a str schema.

    Without the serialize step the raw list reaches a str default, so the user
    sees "[1.0, 2.5, 1.0]" and reparsing wipes their config.
    """
    entry = _mock_entry(
        subentries_data=[
            _entity_subentry_data(
                template="steps",
                step_weights=[1.0, 2.5, 1.0],
                step_colors=["green", "", "red"],
            )
        ]
    )
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_TEMPLATE: "steps"}),
    )
    assert result["step_id"] == "details"

    defaults = {
        str(k): k.default()
        for k in result["data_schema"].schema
        if getattr(k, "default", vol.UNDEFINED) is not vol.UNDEFINED
    }
    assert defaults[CONF_STEP_WEIGHTS] == "1, 2.5, 1"
    assert defaults[CONF_STEP_COLORS] == "green, , red"


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


async def test_subentry_rejects_dangerous_url_scheme(hass: HomeAssistant) -> None:
    """javascript: and other dangerous schemes are rejected on step 2."""
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

    # Step 2 with a security-blocked scheme
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_details_input("alert", **{CONF_URL: "javascript:alert(1)"}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_URL: "invalid_url"}


async def test_subentry_rejects_dangerous_secondary_url(hass: HomeAssistant) -> None:
    """Dangerous scheme on secondary URL surfaces error on the right field."""
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

    # Step 2 with a security-blocked scheme
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_details_input("alert", **{CONF_SECONDARY_URL: "data:text/html,bad"}),
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


async def test_subentry_timeline_series_entities(hass: HomeAssistant) -> None:
    """A timeline subentry binds separate entities as series, freezing labels at save time."""
    hass.states.async_set("sensor.bedroom_pm25", "12", {"friendly_name": "Bedroom Air"})
    hass.states.async_set("sensor.office_pm25", "8")
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_core_input(**{CONF_ENTITY_ID: "binary_sensor.air", CONF_TEMPLATE: "timeline"}),
    )
    assert result["step_id"] == "details"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_details_input(
            "timeline",
            **{CONF_SERIES_ENTITIES: "sensor.bedroom_pm25, Office=sensor.office_pm25"},
        ),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    data = next(iter(entry.subentries.values())).data
    # Unlabeled series defaults to the entity friendly name; explicit label kept.
    assert data[CONF_SERIES_ENTITIES] == [
        {CONF_LABEL: "Bedroom Air", CONF_ENTITY_ID: "sensor.bedroom_pm25"},
        {CONF_LABEL: "Office", CONF_ENTITY_ID: "sensor.office_pm25"},
    ]


# --- _details_schema new field tests ---


def _schema_keys(schema: vol.Schema) -> set[str]:
    """Extract string key names from a voluptuous schema."""
    return {m.schema if hasattr(m, "schema") else m for m in schema.schema}


def test_details_schema_common_has_sound_background_text_color() -> None:
    """Common fields sound, background_color*, and text_color* appear in all templates."""
    schema = _details_schema("binary_sensor.foo", "generic", defaults={})
    keys = _schema_keys(schema)
    assert CONF_SOUND in keys
    assert CONF_BACKGROUND_COLOR in keys
    assert CONF_BACKGROUND_COLOR_ATTRIBUTE in keys
    assert CONF_TEXT_COLOR in keys
    assert CONF_TEXT_COLOR_ATTRIBUTE in keys


def test_details_schema_countdown_has_warning_threshold_and_alarm() -> None:
    """Countdown template includes warning_threshold and alarm fields."""
    schema = _details_schema("binary_sensor.foo", "countdown", defaults={})
    keys = _schema_keys(schema)
    assert CONF_WARNING_THRESHOLD in keys
    assert CONF_ALARM in keys


def test_details_schema_steps_has_step_labels_and_rows() -> None:
    """Steps template includes step_labels and step_rows fields."""
    schema = _details_schema("binary_sensor.foo", "steps", defaults={})
    keys = _schema_keys(schema)
    assert CONF_STEP_LABELS in keys
    assert CONF_STEP_ROWS in keys


def test_details_schema_steps_has_step_weights_and_colors() -> None:
    """Steps template exposes the per-step width and color fields."""
    schema = _details_schema("binary_sensor.foo", "steps", defaults={})
    keys = _schema_keys(schema)
    assert CONF_STEP_WEIGHTS in keys
    assert CONF_STEP_COLORS in keys


def test_details_schema_widget_progress_has_value_scale() -> None:
    """Only the progress widget gets the percent-vs-fraction dropdown."""
    keys = _schema_keys(_widget_details_schema("sensor.foo", WIDGET_TEMPLATE_PROGRESS, defaults={}))
    assert CONF_VALUE_SCALE in keys

    keys = _schema_keys(_widget_details_schema("sensor.foo", WIDGET_TEMPLATE_GAUGE, defaults={}))
    assert CONF_VALUE_SCALE not in keys


def test_details_schema_alert_has_fired_at_attribute() -> None:
    """Alert template includes fired_at_attribute field."""
    schema = _details_schema("binary_sensor.foo", "alert", defaults={})
    keys = _schema_keys(schema)
    assert CONF_FIRED_AT_ATTRIBUTE in keys


def test_details_schema_timeline_has_units() -> None:
    """Timeline template includes units field."""
    schema = _details_schema("sensor.temp", "timeline", defaults={})
    keys = _schema_keys(schema)
    assert CONF_UNITS in keys


def test_details_schema_board_has_tiles() -> None:
    """Board template includes the tiles field."""
    schema = _details_schema("binary_sensor.foo", "board", defaults={})
    keys = _schema_keys(schema)
    assert CONF_TILES in keys


def test_details_schema_log_has_log_level_attribute() -> None:
    """Log template includes the log_level_attribute field."""
    schema = _details_schema("binary_sensor.foo", "log", defaults={})
    keys = _schema_keys(schema)
    assert CONF_LOG_LEVEL_ATTRIBUTE in keys


# --- _parse_int_list tests ---


def test_parse_int_list_basic() -> None:
    """Basic CSV of integers returns list of ints."""
    assert _parse_int_list("1, 2, 3") == [1, 2, 3]


def test_parse_int_list_skips_non_integers() -> None:
    """Non-integer tokens are silently skipped."""
    assert _parse_int_list("1, abc, 3") == [1, 3]


def test_parse_int_list_empty_string_returns_empty() -> None:
    """Empty string returns empty list."""
    assert _parse_int_list("") == []


# --- _parse_float_list / _parse_color_list tests ---


def test_parse_float_list_basic() -> None:
    assert _parse_float_list("1, 2.5, 3") == [1.0, 2.5, 3.0]


def test_parse_float_list_skips_non_numbers() -> None:
    assert _parse_float_list("1, abc, 3") == [1.0, 3.0]


def test_parse_float_list_empty_string_returns_empty() -> None:
    assert _parse_float_list("") == []


def test_float_list_round_trips_through_serialize() -> None:
    """Reconfigure re-renders the stored list as text, so the pair must round-trip."""
    assert _serialize_float_list([1.0, 2.5, 3.0]) == "1, 2.5, 3"
    assert _parse_float_list(_serialize_float_list([1.0, 2.5, 3.0])) == [1.0, 2.5, 3.0]
    assert _serialize_float_list([]) == ""


def test_parse_color_list_keeps_empty_entries() -> None:
    """Colors are positional, so an omitted one must hold its slot, not collapse the list."""
    assert _parse_color_list("red,,blue") == ["red", "", "blue"]
    assert _parse_color_list("red, , blue") == ["red", "", "blue"]


def test_parse_color_list_trailing_comma_adds_a_slot() -> None:
    """Unlike the float parser, empties are significant here, so a stray comma is a step.

    content_mapper drops step_colors wholesale on a length mismatch, so this typo
    silently costs the user every per-step color.
    """
    assert _parse_color_list("red, blue, green,") == ["red", "blue", "green", ""]
    assert _parse_color_list(",") == ["", ""]
    assert _parse_float_list("1, 2, 3,") == [1.0, 2.0, 3.0]


def test_parse_color_list_empty_string_returns_empty() -> None:
    assert _parse_color_list("") == []


def test_color_list_round_trips_through_serialize() -> None:
    assert _parse_color_list(_serialize_csv(["red", "", "blue"])) == ["red", "", "blue"]


# --- _parse_entity_input new field tests ---


def _base_user_input(**overrides) -> dict:
    """Minimal valid user_input for _parse_entity_input."""
    data = {
        CONF_ENTITY_ID: "binary_sensor.foo",
        CONF_TEMPLATE: "generic",
        CONF_SLUG: "ha-foo",
        CONF_ACTIVITY_NAME: "Foo",
        CONF_START_STATES: ["on"],
        CONF_END_STATES: ["off"],
        CONF_PRIORITY: 1,
        CONF_UPDATE_INTERVAL: 5,
    }
    data.update(overrides)
    return data


def test_parse_entity_input_step_labels_parsed_to_dict() -> None:
    """step_labels CSV is parsed into a dict."""
    result = _parse_entity_input(_base_user_input(**{CONF_STEP_LABELS: "1=Init, 2=Build"}))
    assert result[CONF_STEP_LABELS] == {"1": "Init", "2": "Build"}


def test_parse_entity_input_step_rows_parsed_to_int_list() -> None:
    """step_rows CSV is parsed into a list of ints."""
    result = _parse_entity_input(_base_user_input(**{CONF_STEP_ROWS: "1, 2, 3"}))
    assert result[CONF_STEP_ROWS] == [1, 2, 3]


def test_parse_entity_input_step_weights_parsed_to_float_list() -> None:
    """step_weights CSV is parsed into a list of floats."""
    result = _parse_entity_input(_base_user_input(**{CONF_STEP_WEIGHTS: "1, 2.5, 1"}))
    assert result[CONF_STEP_WEIGHTS] == [1.0, 2.5, 1.0]


def test_parse_entity_input_step_colors_keeps_blank_slot() -> None:
    """'red,,blue' is 3 steps, the middle one on the accent color. Not 2 steps."""
    result = _parse_entity_input(_base_user_input(**{CONF_STEP_COLORS: "red,,blue"}))
    assert result[CONF_STEP_COLORS] == ["red", "", "blue"]


def test_parse_entity_input_units_parsed_to_dict() -> None:
    """units CSV is parsed into a dict."""
    result = _parse_entity_input(_base_user_input(**{CONF_UNITS: "Temp=°C, Humidity=%"}))
    assert result[CONF_UNITS] == {"Temp": "°C", "Humidity": "%"}


def test_parse_entity_input_alarm_stored_as_bool() -> None:
    """alarm=True is stored as True; omitted defaults to False."""
    result_true = _parse_entity_input(_base_user_input(**{CONF_ALARM: True}))
    assert result_true[CONF_ALARM] is True

    result_false = _parse_entity_input(_base_user_input())
    assert result_false[CONF_ALARM] is False


def test_parse_entity_input_warning_threshold_int_or_none() -> None:
    """warning_threshold is stored as int when provided, None when omitted."""
    result_with = _parse_entity_input(_base_user_input(**{CONF_WARNING_THRESHOLD: 30}))
    assert result_with[CONF_WARNING_THRESHOLD] == 30
    assert isinstance(result_with[CONF_WARNING_THRESHOLD], int)

    result_without = _parse_entity_input(_base_user_input())
    assert result_without[CONF_WARNING_THRESHOLD] is None


# --- Companion source entity tests ---


def test_parse_entity_input_companion_entities_persisted() -> None:
    """Companion source entity keys are persisted from user input."""
    result = _parse_entity_input(
        _base_user_input(
            **{
                CONF_REMAINING_TIME_ENTITY: "sensor.washer_time",
                CONF_PROGRESS_ENTITY: "sensor.washer_progress",
                CONF_VALUE_ENTITY: "sensor.washer_value",
                CONF_CURRENT_STEP_ENTITY: "sensor.washer_step",
                CONF_FIRED_AT_ENTITY: "sensor.washer_fired",
                CONF_SUBTITLE_ENTITY: "sensor.washer_program",
            }
        )
    )
    assert result[CONF_REMAINING_TIME_ENTITY] == "sensor.washer_time"
    assert result[CONF_PROGRESS_ENTITY] == "sensor.washer_progress"
    assert result[CONF_VALUE_ENTITY] == "sensor.washer_value"
    assert result[CONF_CURRENT_STEP_ENTITY] == "sensor.washer_step"
    assert result[CONF_FIRED_AT_ENTITY] == "sensor.washer_fired"
    assert result[CONF_SUBTITLE_ENTITY] == "sensor.washer_program"


def test_parse_entity_input_companion_entities_default_empty() -> None:
    """Companion source entity keys default to empty strings when omitted."""
    result = _parse_entity_input(_base_user_input())
    assert result[CONF_REMAINING_TIME_ENTITY] == ""
    assert result[CONF_PROGRESS_ENTITY] == ""
    assert result[CONF_VALUE_ENTITY] == ""
    assert result[CONF_CURRENT_STEP_ENTITY] == ""
    assert result[CONF_FIRED_AT_ENTITY] == ""
    assert result[CONF_SUBTITLE_ENTITY] == ""


async def test_subentry_countdown_shows_remaining_time_entity(hass: HomeAssistant) -> None:
    """The countdown details schema includes the remaining-time companion entity field."""
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
    assert CONF_REMAINING_TIME_ENTITY in schema_keys
    assert CONF_SUBTITLE_ENTITY in schema_keys  # common field, always present


async def test_subentry_gauge_shows_value_entity(hass: HomeAssistant) -> None:
    """The gauge details schema includes the value companion entity field."""
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
    assert CONF_VALUE_ENTITY in schema_keys


# ---------------------------------------------------------------------------
# Widget subentry flow tests
# ---------------------------------------------------------------------------


def _widget_step1(**overrides) -> dict:
    data = {
        CONF_ENTITY_ID: "sensor.users",
        CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_VALUE,
        CONF_SLUG: "",
    }
    data.update(overrides)
    return data


def _widget_details(**overrides) -> dict:
    data = {
        CONF_WIDGET_NAME: "Users",
        CONF_VALUE_ATTRIBUTE: "",
        CONF_UNIT: "",
        CONF_LABEL: "",
        CONF_WIDGET_TRIGGER_MODE: WIDGET_TRIGGER_EVENT,
        CONF_WIDGET_POLL_INTERVAL: DEFAULT_WIDGET_POLL_INTERVAL,
    }
    data.update(overrides)
    return data


def test_parse_widget_stat_rows_round_trip() -> None:
    raw = "Users=sensor.users, Active=sensor.active:count:online, Idle=sensor.idle::widgets"
    parsed = _parse_widget_stat_rows(raw)
    assert parsed == [
        {"label": "Users", "entity_id": "sensor.users"},
        {"label": "Active", "entity_id": "sensor.active", "value_attribute": "count", "unit": "online"},
        {"label": "Idle", "entity_id": "sensor.idle", "unit": "widgets"},
    ]
    # Round-trip: serialize → parse → equal (modulo unit-only edge case below).
    serialized = _serialize_widget_stat_rows(parsed)
    reparsed = _parse_widget_stat_rows(serialized)
    assert reparsed == parsed


def test_parse_widget_stat_rows_caps_at_max() -> None:
    raw = ",".join(f"Row{i}=sensor.s{i}" for i in range(10))
    parsed = _parse_widget_stat_rows(raw)
    assert len(parsed) == WIDGET_MAX_STAT_ROWS


def test_parse_widget_stat_rows_accepts_list_passthrough() -> None:
    rows = [
        {"label": "A", "entity_id": "sensor.a"},
        {"label": "", "entity_id": "sensor.b"},  # dropped: empty label
        {"label": "C", "entity_id": ""},  # dropped: empty entity
    ]
    parsed = _parse_widget_stat_rows(rows)
    assert parsed == [{"label": "A", "entity_id": "sensor.a"}]


def test_parse_widget_input_invalid_gauge_range_raises() -> None:
    step1 = _widget_step1(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_GAUGE})
    details = _widget_details(**{CONF_MIN_VALUE: 100.0, CONF_MAX_VALUE: 50.0})
    with pytest.raises(vol.Invalid) as exc:
        _parse_widget_input(details, step1)
    assert exc.value.path == [CONF_MIN_VALUE]


def test_parse_widget_input_stat_list_requires_rows() -> None:
    step1 = _widget_step1(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST})
    details = _widget_details(**{CONF_STAT_ROWS: ""})
    with pytest.raises(vol.Invalid) as exc:
        _parse_widget_input(details, step1)
    assert exc.value.path == [CONF_STAT_ROWS]


def test_parse_widget_input_auto_generates_slug() -> None:
    step1 = _widget_step1(**{CONF_ENTITY_ID: "sensor.registered_users", CONF_SLUG: ""})
    details = _widget_details()
    result = _parse_widget_input(details, step1)
    assert result[CONF_SLUG] == "ha-sensor-registered_users"


def test_parse_widget_input_clamps_poll_interval() -> None:
    step1 = _widget_step1()
    # Below the min — should clamp up to WIDGET_POLL_INTERVAL_MIN (10).
    details = _widget_details(**{CONF_WIDGET_POLL_INTERVAL: 5})
    result = _parse_widget_input(details, step1)
    assert result[CONF_WIDGET_POLL_INTERVAL] == 10


def test_parse_widget_input_full_value_template() -> None:
    """Parser invocation: step1 + step2 → persisted subentry dict."""
    step1 = _widget_step1()
    details = _widget_details()
    result = _parse_widget_input(details, step1)
    assert result[CONF_SLUG] == "ha-sensor-users"
    assert result[CONF_WIDGET_TEMPLATE] == WIDGET_TEMPLATE_VALUE
    assert result[CONF_WIDGET_TRIGGER_MODE] == WIDGET_TRIGGER_EVENT
    assert result[CONF_WIDGET_POLL_INTERVAL] == DEFAULT_WIDGET_POLL_INTERVAL


def test_parse_widget_input_poll_mode_persists_interval() -> None:
    step1 = _widget_step1()
    details = _widget_details(**{CONF_WIDGET_TRIGGER_MODE: WIDGET_TRIGGER_POLL, CONF_WIDGET_POLL_INTERVAL: 45})
    result = _parse_widget_input(details, step1)
    assert result[CONF_WIDGET_TRIGGER_MODE] == WIDGET_TRIGGER_POLL
    assert result[CONF_WIDGET_POLL_INTERVAL] == 45


def test_parse_widget_input_unknown_trigger_falls_back_to_event() -> None:
    step1 = _widget_step1()
    details = _widget_details(**{CONF_WIDGET_TRIGGER_MODE: "garbage"})
    result = _parse_widget_input(details, step1)
    assert result[CONF_WIDGET_TRIGGER_MODE] == WIDGET_TRIGGER_EVENT


def test_parse_widget_input_value_scale_persisted() -> None:
    step1 = _widget_step1(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS})
    details = _widget_details(**{CONF_VALUE_SCALE: VALUE_SCALE_PERCENT})
    result = _parse_widget_input(details, step1)
    assert result[CONF_VALUE_SCALE] == VALUE_SCALE_PERCENT


def test_parse_widget_input_value_scale_defaults_to_auto() -> None:
    """Absent (every pre-existing widget) or garbage both land on auto."""
    step1 = _widget_step1(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_PROGRESS})
    assert _parse_widget_input(_widget_details(), step1)[CONF_VALUE_SCALE] == DEFAULT_VALUE_SCALE

    details = _widget_details(**{CONF_VALUE_SCALE: "garbage"})
    assert _parse_widget_input(details, step1)[CONF_VALUE_SCALE] == DEFAULT_VALUE_SCALE


# --- Tap action parsing & validation ---


def test_parse_entity_input_tap_action_url_persisted() -> None:
    """tap_action_url + foreground roundtrip through the entity parser."""
    result = _parse_entity_input(
        _base_user_input(
            **{
                CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
                CONF_TAP_ACTION_FOREGROUND: True,
            }
        )
    )
    assert result[CONF_TAP_ACTION_URL] == "homeassistant://navigate/lovelace/0"
    assert result[CONF_TAP_ACTION_FOREGROUND] is True


def test_parse_entity_input_url_title_and_foreground_persisted() -> None:
    """url_title and url_foreground roundtrip through the entity parser (steps/alert)."""
    result = _parse_entity_input(
        _base_user_input(
            **{
                CONF_TEMPLATE: "steps",
                CONF_URL: "https://ha.local/lovelace/laundry",
                CONF_URL_TITLE: "Open",
                CONF_URL_FOREGROUND: False,
                CONF_SECONDARY_URL: "https://ha.local/api/services/script/run",
                CONF_SECONDARY_URL_TITLE: "Run",
                CONF_SECONDARY_URL_FOREGROUND: False,
            }
        )
    )
    assert result[CONF_URL] == "https://ha.local/lovelace/laundry"
    assert result[CONF_URL_TITLE] == "Open"
    assert result[CONF_URL_FOREGROUND] is False
    assert result[CONF_SECONDARY_URL_TITLE] == "Run"
    assert result[CONF_SECONDARY_URL_FOREGROUND] is False


def test_parse_entity_input_silent_requires_http() -> None:
    """foreground=False with a custom-scheme URL is rejected as silent_requires_http."""
    with pytest.raises(vol.Invalid) as exc:
        _parse_entity_input(
            _base_user_input(
                **{
                    CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
                    CONF_TAP_ACTION_FOREGROUND: False,
                }
            )
        )
    assert exc.value.path == [CONF_TAP_ACTION_URL]
    # Voluptuous stringifies the error code into args[0]
    assert "silent_requires_http" in str(exc.value)


def test_parse_entity_input_blocks_dangerous_scheme() -> None:
    """javascript: in any URL field is rejected as invalid_url."""
    with pytest.raises(vol.Invalid) as exc:
        _parse_entity_input(_base_user_input(**{CONF_TAP_ACTION_URL: "javascript:alert(1)"}))
    assert exc.value.path == [CONF_TAP_ACTION_URL]


def test_parse_widget_input_tap_action_url_persisted() -> None:
    """tap_action_url + foreground roundtrip through the widget parser."""
    step1 = _widget_step1()
    details = _widget_details(
        **{
            CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
            CONF_TAP_ACTION_FOREGROUND: True,
        }
    )
    result = _parse_widget_input(details, step1)
    assert result[CONF_TAP_ACTION_URL] == "homeassistant://navigate/lovelace/0"
    assert result[CONF_TAP_ACTION_FOREGROUND] is True


def test_parse_widget_input_silent_requires_http() -> None:
    """Widget tap_action with foreground=False + custom scheme is rejected."""
    step1 = _widget_step1()
    details = _widget_details(
        **{
            CONF_TAP_ACTION_URL: "homeassistant://navigate/lovelace/0",
            CONF_TAP_ACTION_FOREGROUND: False,
        }
    )
    with pytest.raises(vol.Invalid) as exc:
        _parse_widget_input(details, step1)
    assert exc.value.path == [CONF_TAP_ACTION_URL]
    assert "silent_requires_http" in str(exc.value)


def test_parse_widget_input_empty_tap_action_defaults() -> None:
    """Empty tap_action_url is stored as '' (no validation triggered)."""
    step1 = _widget_step1()
    details = _widget_details()
    result = _parse_widget_input(details, step1)
    assert result[CONF_TAP_ACTION_URL] == ""
    # Default foreground is True.
    assert result[CONF_TAP_ACTION_FOREGROUND] is True


# --- Board tiles parsing & serialization ---


def test_parse_board_tiles_basic() -> None:
    """Parse 'label=entity[:attr[:unit]]' tiles into dicts."""
    parsed = _parse_board_tiles("CPU=sensor.cpu:temperature:%, Door=binary_sensor.door")
    assert parsed == [
        {
            CONF_LABEL: "CPU",
            CONF_ENTITY_ID: "sensor.cpu",
            CONF_VALUE_ATTRIBUTE: "temperature",
            CONF_UNIT: "%",
        },
        {CONF_LABEL: "Door", CONF_ENTITY_ID: "binary_sensor.door"},
    ]


def test_parse_board_tiles_icon_with_colon() -> None:
    """An MDI icon (containing a colon) survives the maxsplit-3 parse intact."""
    parsed = _parse_board_tiles("CPU=sensor.cpu:::mdi:cpu-64")
    assert len(parsed) == 1
    assert parsed[0][CONF_ICON] == "mdi:cpu-64"


def test_parse_board_tiles_caps_at_max() -> None:
    """More than BOARD_MAX_TILES tiles are truncated to the cap."""
    raw = ", ".join(f"Row{i}=sensor.s{i}" for i in range(6))
    parsed = _parse_board_tiles(raw)
    assert len(parsed) == BOARD_MAX_TILES


def test_parse_board_tiles_empty() -> None:
    """Empty string and non-string inputs return an empty list."""
    assert _parse_board_tiles("") == []
    assert _parse_board_tiles(None) == []


def test_board_tiles_round_trip() -> None:
    """parse → serialize → parse yields the same list (attr+unit+icon preserved)."""
    raw = "CPU=sensor.cpu:temperature:%:mdi:cpu-64, Door=binary_sensor.door"
    parsed = _parse_board_tiles(raw)
    reparsed = _parse_board_tiles(_serialize_board_tiles(parsed))
    assert reparsed == parsed


# --- _parse_log_columns / _serialize_log_columns ---


def test_parse_log_columns_attribute() -> None:
    """A bare word (no dot, no colon) is a tracked-entity attribute column."""
    assert _parse_log_columns("brightness") == [{"attribute": "brightness"}]


def test_parse_log_columns_entity_state() -> None:
    """A source with a dot (no colon) is another entity's state column."""
    assert _parse_log_columns("binary_sensor.door") == [{CONF_ENTITY_ID: "binary_sensor.door"}]


def test_parse_log_columns_entity_attribute() -> None:
    """An 'entity:attribute' source is another entity's attribute column."""
    assert _parse_log_columns("sensor.temp:temperature") == [
        {CONF_ENTITY_ID: "sensor.temp", "attribute": "temperature"}
    ]


def test_parse_log_columns_label_and_unit() -> None:
    """'Label=source|unit' splits off both the label and the unit suffix."""
    assert _parse_log_columns("Temp=sensor.temp:temperature|°C") == [
        {CONF_LABEL: "Temp", CONF_ENTITY_ID: "sensor.temp", "attribute": "temperature", CONF_UNIT: "°C"}
    ]


def test_parse_log_columns_attribute_with_unit() -> None:
    """A bare attribute can carry a unit suffix: 'color_temp_kelvin|K'."""
    assert _parse_log_columns("color_temp_kelvin|K") == [{"attribute": "color_temp_kelvin", CONF_UNIT: "K"}]


def test_parse_log_columns_caps_at_max() -> None:
    """More than LOG_MAX_COLUMNS columns are truncated to the cap."""
    raw = ", ".join(f"attr{i}" for i in range(LOG_MAX_COLUMNS + 3))
    assert len(_parse_log_columns(raw)) == LOG_MAX_COLUMNS


def test_parse_log_columns_empty() -> None:
    """Empty string and non-string inputs return an empty list."""
    assert _parse_log_columns("") == []
    assert _parse_log_columns(None) == []


def test_log_columns_round_trip() -> None:
    """parse → serialize → parse yields the same list across every source kind."""
    raw = "brightness, color_temp_kelvin|K, binary_sensor.door, sensor.temp:temperature, Door=binary_sensor.door"
    parsed = _parse_log_columns(raw)
    reparsed = _parse_log_columns(_serialize_log_columns(parsed))
    assert reparsed == parsed


def test_parse_entity_input_log_emits_columns() -> None:
    """A log template parses CONF_LOG_COLUMNS into a list of column dicts."""
    result = _parse_entity_input(
        _base_user_input(**{CONF_TEMPLATE: "log", CONF_LOG_COLUMNS: "brightness, sensor.temp:temperature"})
    )
    assert result[CONF_LOG_COLUMNS] == [
        {"attribute": "brightness"},
        {CONF_ENTITY_ID: "sensor.temp", "attribute": "temperature"},
    ]


# --- _parse_series_entities / _serialize_series_entities / labels ---


def test_parse_series_entities_state_and_attribute() -> None:
    """State source and 'entity:attribute' source parse into series dicts."""
    parsed = _parse_series_entities("Bedroom=sensor.bedroom_pm25, climate.hvac:current_temperature")
    assert parsed == [
        {CONF_LABEL: "Bedroom", CONF_ENTITY_ID: "sensor.bedroom_pm25"},
        {CONF_ENTITY_ID: "climate.hvac", "attribute": "current_temperature"},
    ]


def test_parse_series_entities_skips_bare_word() -> None:
    """A bare word (no dot) is not an entity_id, so it is skipped."""
    assert _parse_series_entities("brightness") == []


def test_parse_series_entities_caps_at_max() -> None:
    """More than TIMELINE_MAX_SERIES series entities are truncated to the cap."""
    raw = ", ".join(f"sensor.s{i}" for i in range(TIMELINE_MAX_SERIES + 4))
    assert len(_parse_series_entities(raw)) == TIMELINE_MAX_SERIES


def test_parse_series_entities_empty() -> None:
    """Empty string and non-string inputs return an empty list."""
    assert _parse_series_entities("") == []
    assert _parse_series_entities(None) == []


def test_series_entities_round_trip() -> None:
    """parse (labels frozen) then serialize then parse is stable."""
    raw = "Bedroom=sensor.bedroom, Set=climate.hvac:temperature, sensor.plain"
    resolved = _resolve_series_entity_labels(_parse_series_entities(raw), None)
    reparsed = _parse_series_entities(_serialize_series_entities(resolved))
    assert reparsed == resolved


def test_resolve_series_labels_defaults_to_friendly_name() -> None:
    """An unlabeled series defaults to the source entity's friendly name."""

    class _Hass:
        class states:
            @staticmethod
            def get(entity_id):
                return make_mock_state("12", {"friendly_name": "Bedroom Air"}, entity_id)

    resolved = _resolve_series_entity_labels([{CONF_ENTITY_ID: "sensor.bedroom_pm25"}], _Hass())
    assert resolved == [{CONF_LABEL: "Bedroom Air", CONF_ENTITY_ID: "sensor.bedroom_pm25"}]


def test_resolve_series_labels_attribute_suffix_and_dedupe() -> None:
    """Two attributes of one entity default to distinct labels (attr suffix + dedupe)."""

    class _Hass:
        class states:
            @staticmethod
            def get(entity_id):
                return make_mock_state("heat", {"friendly_name": "HVAC"}, entity_id)

    resolved = _resolve_series_entity_labels(
        [
            {CONF_ENTITY_ID: "climate.hvac", "attribute": "current_temperature"},
            {CONF_ENTITY_ID: "climate.hvac", "attribute": "target_temperature"},
        ],
        _Hass(),
    )
    labels = [s[CONF_LABEL] for s in resolved]
    assert labels == ["HVAC current_temperature", "HVAC target_temperature"]


def test_resolve_series_labels_duplicate_gets_numeric_suffix() -> None:
    """Two identical explicit labels are de-duplicated with a numeric suffix."""
    resolved = _resolve_series_entity_labels(
        [
            {CONF_LABEL: "Air", CONF_ENTITY_ID: "sensor.a"},
            {CONF_LABEL: "Air", CONF_ENTITY_ID: "sensor.b"},
        ],
        None,
    )
    assert [s[CONF_LABEL] for s in resolved] == ["Air", "Air 2"]


def test_resolve_series_labels_truncated_to_cap() -> None:
    """A long label is truncated to TIMELINE_SERIES_LABEL_MAX."""
    resolved = _resolve_series_entity_labels([{CONF_LABEL: "X" * 60, CONF_ENTITY_ID: "sensor.a"}], None)
    assert len(resolved[0][CONF_LABEL]) == TIMELINE_SERIES_LABEL_MAX


def test_parse_entity_input_series_entities_persisted() -> None:
    """A timeline parses CONF_SERIES_ENTITIES into a resolved list (labels frozen)."""
    result = _parse_entity_input(
        _base_user_input(**{CONF_TEMPLATE: "timeline", CONF_SERIES_ENTITIES: "Bedroom=sensor.bedroom_pm25"})
    )
    assert result[CONF_SERIES_ENTITIES] == [{CONF_LABEL: "Bedroom", CONF_ENTITY_ID: "sensor.bedroom_pm25"}]


def test_parse_entity_input_too_many_series() -> None:
    """The attribute map plus series entities may not exceed TIMELINE_MAX_SERIES."""
    series = ", ".join(f"a{i}=L{i}" for i in range(6))
    entities = ", ".join(f"L{i}=sensor.s{i}" for i in range(6))
    with pytest.raises(vol.Invalid) as exc:
        _parse_entity_input(
            _base_user_input(**{CONF_TEMPLATE: "timeline", CONF_SERIES: series, CONF_SERIES_ENTITIES: entities})
        )
    assert exc.value.path == [CONF_SERIES_ENTITIES]
    assert "too_many_series" in str(exc.value)


def test_details_schema_timeline_has_series_entities() -> None:
    """Timeline template includes the series_entities field."""
    schema = _details_schema("sensor.temp", "timeline", defaults={})
    keys = _schema_keys(schema)
    assert CONF_SERIES_ENTITIES in keys


# --- _parse_entity_input board/log fields ---


def test_parse_entity_input_board_emits_tiles() -> None:
    """A board template parses CONF_TILES into a list of tile dicts."""
    result = _parse_entity_input(_base_user_input(**{CONF_TEMPLATE: "board", CONF_TILES: "CPU=sensor.cpu"}))
    assert result[CONF_TILES] == [{CONF_LABEL: "CPU", CONF_ENTITY_ID: "sensor.cpu"}]


def test_parse_entity_input_board_requires_tiles() -> None:
    """A board template with no tiles raises tiles_required on the tiles field."""
    with pytest.raises(vol.Invalid) as exc:
        _parse_entity_input(_base_user_input(**{CONF_TEMPLATE: "board"}))
    assert exc.value.path == [CONF_TILES]
    assert "tiles_required" in str(exc.value)


def test_parse_entity_input_log_emits_level_attribute() -> None:
    """A log template persists the log_level_attribute field."""
    result = _parse_entity_input(_base_user_input(**{CONF_TEMPLATE: "log", CONF_LOG_LEVEL_ATTRIBUTE: "severity"}))
    assert result[CONF_LOG_LEVEL_ATTRIBUTE] == "severity"


# --- details step: input preserved on validation error ---


def _suggested_values(result) -> dict:
    """Map schema field name -> suggested_value from a re-shown form."""
    return {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
        if hasattr(key, "description")
    }


async def test_details_error_preserves_user_input(hass: HomeAssistant) -> None:
    """A validation error re-shows the details form pre-filled with the submission."""
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

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_details_input(
            "gauge",
            **{
                CONF_MIN_VALUE: 100.0,
                CONF_MAX_VALUE: 50.0,
                CONF_ACTIVITY_NAME: "My Gauge Activity",
            },
        ),
    )
    assert result["type"] is FlowResultType.FORM
    assert CONF_MIN_VALUE in result["errors"]

    suggested = _suggested_values(result)
    assert suggested[CONF_ACTIVITY_NAME] == "My Gauge Activity"
    assert suggested[CONF_MIN_VALUE] == 100.0
    assert suggested[CONF_MAX_VALUE] == 50.0


async def test_widget_details_error_preserves_user_input(hass: HomeAssistant) -> None:
    """The widget details form also re-fills the submission on validation error."""
    entry = _mock_entry()
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.users", "42")

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_WIDGET),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            CONF_ENTITY_ID: "sensor.users",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_SLUG: "ha-stats",
        },
    )
    assert result["step_id"] == "details"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={CONF_STAT_ROWS: "", CONF_WIDGET_NAME: "My Stat Board"},
    )
    assert result["type"] is FlowResultType.FORM
    assert CONF_STAT_ROWS in result["errors"]

    suggested = _suggested_values(result)
    assert suggested[CONF_WIDGET_NAME] == "My Stat Board"


# --- severity_label length cap ---


async def test_severity_label_over_cap_rejected(hass: HomeAssistant) -> None:
    """A severity_label longer than the server cap fails schema validation inline."""
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

    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input=_mock_details_input("alert", **{CONF_SEVERITY_LABEL: "x" * (MAX_SEVERITY_LABEL_LEN + 1)}),
        )

    # A label at the cap goes through and is stored intact.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=_mock_details_input("alert", **{CONF_SEVERITY_LABEL: "y" * MAX_SEVERITY_LABEL_LEN}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = list(entry.subentries.values())
    assert subentries[0].data[CONF_SEVERITY_LABEL] == "y" * MAX_SEVERITY_LABEL_LEN


# --- NumberSelector fields stored as int ---


async def test_number_fields_stored_as_int(hass: HomeAssistant) -> None:
    """NumberSelector floats are coerced back to int in the stored config."""
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(
        hass,
        entry,
        template="steps",
        details_overrides={
            CONF_TOTAL_STEPS: 5.0,
            CONF_PRIORITY: 3.0,
            CONF_UPDATE_INTERVAL: 10.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = next(iter(entry.subentries.values())).data
    for key, expected in ((CONF_TOTAL_STEPS, 5), (CONF_PRIORITY, 3), (CONF_UPDATE_INTERVAL, 10)):
        assert data[key] == expected
        assert isinstance(data[key], int), key


async def test_decimals_stored_as_int(hass: HomeAssistant) -> None:
    entry = _mock_entry()
    entry.add_to_hass(hass)

    result = await _add_entity_subentry(
        hass,
        entry,
        template="timeline",
        details_overrides={CONF_DECIMALS: 2.0, CONF_VALUE_ATTRIBUTE: "temperature"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = next(iter(entry.subentries.values())).data
    assert data[CONF_DECIMALS] == 2
    assert isinstance(data[CONF_DECIMALS], int)
