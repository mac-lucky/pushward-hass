"""Tests for the options flow: the end-to-end encryption key."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.const import (
    CONF_E2E_KEY,
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    DOMAIN,
)
from custom_components.pushward.e2e import key_id

from .conftest import make_usage_payload

KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
KID = key_id(bytes.fromhex(KEY_HEX))


def _entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: "hlk_test"},
        options=options or {},
        version=2,
        unique_id=DOMAIN,
    )


async def test_options_flow_stores_the_key_normalized(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["description_placeholders"] == {"key_id": "-"}

    spaced = " ".join(KEY_HEX.upper()[i : i + 16] for i in range(0, 64, 16))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_E2E_KEY: f" {spaced}\n"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_E2E_KEY: KEY_HEX}


async def test_options_flow_never_sends_the_stored_key_back(hass: HomeAssistant) -> None:
    entry = _entry({CONF_E2E_KEY: KEY_HEX})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"] == {"key_id": KID}
    for marker in result["data_schema"].schema:
        assert "suggested_value" not in (marker.description or {})
    assert KEY_HEX not in str(result)


async def test_options_flow_empty_field_keeps_the_key(hass: HomeAssistant) -> None:
    entry = _entry({CONF_E2E_KEY: KEY_HEX})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_E2E_KEY: "  "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_E2E_KEY: KEY_HEX}


async def test_options_flow_replaces_the_key(hass: HomeAssistant) -> None:
    other = "ff" * 32
    entry = _entry({CONF_E2E_KEY: KEY_HEX})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_E2E_KEY: other.upper()})
    assert entry.options == {CONF_E2E_KEY: other}


@pytest.mark.parametrize("typed", ["", "ff" * 32])
async def test_options_flow_remove_turns_encryption_off(hass: HomeAssistant, typed: str) -> None:
    entry = _entry({CONF_E2E_KEY: KEY_HEX})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_E2E_KEY: typed, "remove_e2e_key": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {}


async def test_options_flow_offers_remove_only_with_a_key(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert [str(marker) for marker in result["data_schema"].schema] == [CONF_E2E_KEY]


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("hlk_0123456789abcdef", "e2e_key_is_integration_key"),
        ("hla_0123456789abcdef", "e2e_key_is_integration_key"),
        (KEY_HEX[:-2], "invalid_e2e_key"),
        (KEY_HEX[:-1] + "z", "invalid_e2e_key"),
    ],
)
async def test_options_flow_refuses_what_is_not_a_key(hass: HomeAssistant, value: str, error: str) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_E2E_KEY: value})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_E2E_KEY: error}
    assert entry.options == {}


async def test_changing_the_key_swaps_it_in_without_a_reload(hass: HomeAssistant) -> None:
    """Live Activities keep running: only the client's key changes."""
    api = AsyncMock()
    api.get_me = AsyncMock(return_value=make_usage_payload())
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.pushward.PushWardApiClient", return_value=api) as client_cls:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert client_cls.call_args.kwargs["e2e_key"] is None

    data = hass.data[DOMAIN][entry.entry_id]
    with (
        patch.object(data["manager"], "async_reload") as reload,
        patch.object(data["todo_manager"], "async_retry_refused") as retry_refused,
    ):
        hass.config_entries.async_update_entry(entry, options={CONF_E2E_KEY: KEY_HEX})
        await hass.async_block_till_done()
        assert api.e2e_key == bytes.fromhex(KEY_HEX)

        hass.config_entries.async_update_entry(entry, options={})
        await hass.async_block_till_done()
        assert api.e2e_key is None
    reload.assert_not_called()
    # A reminder refused under the old key gets another try under the new one.
    assert retry_refused.call_count == 2
