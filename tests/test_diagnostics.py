"""Tests for the PushWard config-entry diagnostics dump.

A diagnostics file is attached to bug reports, so two properties matter most:
the integration key is never leaked, and a broken board/log (or any) payload is
visible (subentry config + last rendered content).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.const import (
    CONF_ACTIVITY_NAME,
    CONF_END_STATES,
    CONF_ENTITY_ID,
    CONF_INTEGRATION_KEY,
    CONF_LABEL,
    CONF_SERVER_URL,
    CONF_SLUG,
    CONF_START_STATES,
    CONF_TAP_ACTION_URL,
    CONF_TEMPLATE,
    CONF_TILES,
    DEFAULT_SERVER_URL,
    DOMAIN,
    SUBENTRY_TYPE_ENTITY,
    SUBENTRY_TYPE_WIDGET,
)
from custom_components.pushward.diagnostics import async_get_config_entry_diagnostics

from .conftest import make_entity_config, make_usage_payload, make_widget_config

MOCK_KEY = "hlk_secret_value"


def _mock_api() -> AsyncMock:
    api = AsyncMock()
    api.create_activity = AsyncMock()
    api.update_activity = AsyncMock()
    api.delete_activity = AsyncMock()
    api.create_widget = AsyncMock()
    api.patch_widget = AsyncMock()
    api.delete_widget = AsyncMock()
    api.get_me = AsyncMock(return_value=make_usage_payload())
    return api


def _entry_with_subentries() -> MockConfigEntry:
    entity = make_entity_config(**{CONF_ENTITY_ID: "binary_sensor.washer", CONF_SLUG: "ha-washer"})
    widget = make_widget_config(**{CONF_ENTITY_ID: "sensor.users", CONF_SLUG: "ha-users"})
    return MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: MOCK_KEY},
        version=2,
        unique_id=DOMAIN,
        subentries_data=[
            ConfigSubentryData(
                data=entity,
                subentry_type=SUBENTRY_TYPE_ENTITY,
                title=entity[CONF_ACTIVITY_NAME],
                unique_id=entity[CONF_ENTITY_ID],
            ),
            ConfigSubentryData(
                data=widget,
                subentry_type=SUBENTRY_TYPE_WIDGET,
                title="Users",
                unique_id=widget[CONF_SLUG],
            ),
        ],
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, api: AsyncMock) -> None:
    entry.add_to_hass(hass)
    with patch("custom_components.pushward.PushWardApiClient", return_value=api):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_diagnostics_redacts_integration_key(hass: HomeAssistant) -> None:
    """The hlk_ integration key never appears in the diagnostics dump."""
    api = _mock_api()
    entry = _entry_with_subentries()
    await _setup(hass, entry, api)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["data"][CONF_INTEGRATION_KEY] != MOCK_KEY
    assert MOCK_KEY not in str(diag)


async def test_diagnostics_includes_subentries(hass: HomeAssistant) -> None:
    """Both the tracked entity and widget subentries appear with their config."""
    api = _mock_api()
    entry = _entry_with_subentries()
    await _setup(hass, entry, api)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    types = {s["subentry_type"] for s in diag["subentries"]}
    assert types == {SUBENTRY_TYPE_ENTITY, SUBENTRY_TYPE_WIDGET}
    entity_sub = next(s for s in diag["subentries"] if s["subentry_type"] == SUBENTRY_TYPE_ENTITY)
    assert entity_sub["config"][CONF_ENTITY_ID] == "binary_sensor.washer"
    assert "is_active" in entity_sub


async def test_diagnostics_includes_board_last_content(hass: HomeAssistant) -> None:
    """A board activity's last rendered content (tiles) is visible in the dump."""
    api = _mock_api()
    entity = make_entity_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.home",
            CONF_SLUG: "ha-home",
            CONF_TEMPLATE: "board",
            CONF_START_STATES: ["on"],
            CONF_END_STATES: ["off"],
            CONF_TILES: [{CONF_LABEL: "CPU", CONF_ENTITY_ID: "sensor.cpu"}],
        }
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: MOCK_KEY},
        version=2,
        unique_id=DOMAIN,
        subentries_data=[
            ConfigSubentryData(
                data=entity,
                subentry_type=SUBENTRY_TYPE_ENTITY,
                title="Home",
                unique_id=entity[CONF_ENTITY_ID],
            ),
        ],
    )
    hass.states.async_set("sensor.cpu", "72")
    await _setup(hass, entry, api)
    hass.states.async_set("binary_sensor.home", "on")
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)

    board_sub = next(s for s in diag["subentries"] if s["config"].get(CONF_TEMPLATE) == "board")
    assert board_sub["is_active"] is True
    assert board_sub["last_content"]["template"] == "board"
    assert board_sub["last_content"]["tiles"][0]["label"] == "CPU"


async def test_diagnostics_redacts_tap_action_url(hass: HomeAssistant) -> None:
    """Webhook tap-action URLs (which can embed tokens) are redacted in both the
    subentry config and the rendered last_content — the dump is meant for public bug reports."""
    api = _mock_api()
    secret_url = "https://hooks.example.com/api/webhook/SECRET_TOKEN"
    entity = make_entity_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.washer",
            CONF_SLUG: "ha-washer",
            CONF_START_STATES: ["on"],
            CONF_END_STATES: ["off"],
            CONF_TAP_ACTION_URL: secret_url,
        }
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: MOCK_KEY},
        version=2,
        unique_id=DOMAIN,
        subentries_data=[
            ConfigSubentryData(
                data=entity,
                subentry_type=SUBENTRY_TYPE_ENTITY,
                title="Washer",
                unique_id=entity[CONF_ENTITY_ID],
            ),
        ],
    )
    hass.states.async_set("binary_sensor.washer", "off")
    await _setup(hass, entry, api)
    hass.states.async_set("binary_sensor.washer", "on")
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)

    # The secret must not appear anywhere — neither in config nor rendered last_content.
    assert secret_url not in str(diag)
    entity_sub = next(s for s in diag["subentries"] if s["subentry_type"] == SUBENTRY_TYPE_ENTITY)
    assert entity_sub["config"][CONF_TAP_ACTION_URL] != secret_url
