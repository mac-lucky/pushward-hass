"""Several PushWard accounts (config entries) side by side: setup, naming and service routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.api import PushWardApiError, PushWardForbiddenError
from custom_components.pushward.const import CONF_INTEGRATION_KEY, CONF_SERVER_URL, DEFAULT_SERVER_URL, DOMAIN
from custom_components.pushward.widget_manager import WidgetManager

from .conftest import async_setup_with_api, make_usage_payload, make_widget_config


def _mock_api(account_id: str = "user-123") -> AsyncMock:
    api = AsyncMock()
    api.get_me = AsyncMock(return_value=make_usage_payload(id=account_id))
    api.create_notification = AsyncMock(return_value={"id": 7, "answerable": False})
    api.cancel_notification_receipts_by_tag = AsyncMock(return_value=2)
    return api


async def _add_account(
    hass: HomeAssistant, api: AsyncMock, *, unique_id: str = "user-123", title: str = "PushWard (Test)"
) -> MockConfigEntry:
    """Add and load one entry. The component is already set up for every entry after the first,
    so each setup builds its client from this api mock only."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: f"key-{unique_id}"},
        version=2,
        unique_id=unique_id,
    )
    await async_setup_with_api(hass, entry, api)
    return entry


@pytest.fixture
async def two_accounts(hass: HomeAssistant) -> tuple[tuple[MockConfigEntry, AsyncMock], ...]:
    me, anna = _mock_api("user-123"), _mock_api("user-456")
    me_entry = await _add_account(hass, me)
    anna_entry = await _add_account(hass, anna, unique_id="user-456", title="PushWard (Anna)")
    return (me_entry, me), (anna_entry, anna)


# --- setup ---


async def test_legacy_entry_adopts_account_id(hass: HomeAssistant) -> None:
    """An entry from before several accounts were allowed is keyed by its account after setup."""
    entry = await _add_account(hass, _mock_api("user-123"), unique_id=DOMAIN, title="PushWard")
    assert entry.unique_id == "user-123"
    assert entry.title == "PushWard"


async def test_legacy_entry_keeps_domain_id_when_account_taken(hass: HomeAssistant) -> None:
    """Two entries of one account (possible only before the migration) never share a unique_id."""
    await _add_account(hass, _mock_api("user-123"))
    legacy = await _add_account(hass, _mock_api("user-123"), unique_id=DOMAIN, title="PushWard")
    assert legacy.unique_id == DOMAIN


async def test_each_account_has_its_own_device(hass: HomeAssistant, two_accounts) -> None:
    (me_entry, _), (anna_entry, _) = two_accounts
    devices = dr.async_get(hass)
    for entry in (me_entry, anna_entry):
        device = devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        assert device is not None
        assert device.name == entry.title


# --- fan-out ---


@pytest.mark.parametrize(
    ("service", "data", "method"),
    [
        ("create_activity", {"slug": "ha-dryer", "name": "Dryer"}, "create_activity"),
        ("update_activity_generic", {"slug": "ha-dryer", "state": "ongoing", "progress": 0.5}, "update_activity"),
        ("update_activity", {"slug": "ha-dryer", "state": "ongoing"}, "update_activity"),
        ("end_activity", {"slug": "ha-dryer"}, "update_activity"),
        ("delete_activity", {"slug": "ha-dryer"}, "delete_activity"),
        ("send_notification", {"title": "Dryer", "body": "Done"}, "create_notification"),
        ("delete_widget", {"slug": "ha-users"}, "delete_widget"),
    ],
)
async def test_write_actions_reach_every_account(
    hass: HomeAssistant, two_accounts, service: str, data: dict, method: str
) -> None:
    (_, me), (_, anna) = two_accounts
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    getattr(me, method).assert_awaited_once()
    getattr(anna, method).assert_awaited_once()


async def test_config_entry_id_targets_only_that_account(hass: HomeAssistant, two_accounts) -> None:
    (_, me), (anna_entry, anna) = two_accounts
    await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {"config_entry_id": anna_entry.entry_id, "title": "Dryer", "body": "Done"},
        blocking=True,
    )
    anna.create_notification.assert_awaited_once()
    me.create_notification.assert_not_awaited()


async def test_config_entry_id_list(hass: HomeAssistant, two_accounts) -> None:
    (me_entry, me), (anna_entry, anna) = two_accounts
    await hass.services.async_call(
        DOMAIN,
        "end_activity",
        {"config_entry_id": [me_entry.entry_id, anna_entry.entry_id, me_entry.entry_id], "slug": "ha-dryer"},
        blocking=True,
    )
    me.update_activity.assert_awaited_once()
    anna.update_activity.assert_awaited_once()


async def test_config_entry_id_stays_out_of_activity_content(hass: HomeAssistant, two_accounts) -> None:
    (me_entry, me), _ = two_accounts
    await hass.services.async_call(
        DOMAIN,
        "update_activity_generic",
        {"config_entry_id": me_entry.entry_id, "slug": "ha-dryer", "state": "ongoing", "progress": 0.5},
        blocking=True,
    )
    content = me.update_activity.await_args.args[2]
    assert "config_entry_id" not in content
    assert content["progress"] == 0.5


async def test_unknown_config_entry_id(hass: HomeAssistant, two_accounts) -> None:
    (_, me), (_, anna) = two_accounts
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "delete_activity", {"config_entry_id": "nope", "slug": "ha-dryer"}, blocking=True
        )
    assert err.value.translation_key == "entry_not_loaded"
    me.delete_activity.assert_not_awaited()
    anna.delete_activity.assert_not_awaited()


async def test_one_account_failing_still_reaches_the_other(hass: HomeAssistant, two_accounts) -> None:
    (_, me), (_, anna) = two_accounts
    me.create_notification.side_effect = PushWardForbiddenError("no permission", status_code=403)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "send_notification", {"title": "Dryer", "body": "Done"}, blocking=True)
    anna.create_notification.assert_awaited_once()


async def test_cancel_by_tag_sums_every_account(hass: HomeAssistant, two_accounts) -> None:
    (_, me), (_, anna) = two_accounts
    response = await hass.services.async_call(
        DOMAIN, "cancel_notifications", {"tag": "garage"}, blocking=True, return_response=True
    )
    assert response == {"canceled": 4}
    me.cancel_notification_receipts_by_tag.assert_awaited_once_with("garage")
    anna.cancel_notification_receipts_by_tag.assert_awaited_once_with("garage")


@pytest.mark.parametrize("empty", ["", None, []])
async def test_empty_config_entry_id_means_every_account(hass: HomeAssistant, two_accounts, empty) -> None:
    """A blueprint's unset account input arrives empty; it must not fail the call."""
    (_, me), (_, anna) = two_accounts
    await hass.services.async_call(
        DOMAIN, "delete_activity", {"config_entry_id": empty, "slug": "ha-dryer"}, blocking=True
    )
    me.delete_activity.assert_awaited_once()
    anna.delete_activity.assert_awaited_once()


@pytest.fixture
async def one_account_down(hass: HomeAssistant) -> tuple[AsyncMock, MockConfigEntry]:
    """Two accounts set up, the second one stuck retrying its setup."""
    me = _mock_api("user-123")
    await _add_account(hass, me)
    down = _mock_api("user-456")
    down.get_me.side_effect = PushWardApiError("server unreachable")
    down_entry = await _add_account(hass, down, unique_id="user-456", title="PushWard (Anna)")
    assert down_entry.state is ConfigEntryState.SETUP_RETRY
    return me, down_entry


async def test_fan_out_skips_an_account_that_is_down(
    hass: HomeAssistant, one_account_down, caplog: pytest.LogCaptureFixture
) -> None:
    me, _ = one_account_down
    await hass.services.async_call(DOMAIN, "end_activity", {"slug": "ha-dryer"}, blocking=True)
    me.update_activity.assert_awaited_once()
    assert "skipped accounts that are not loaded: PushWard (Anna)" in caplog.text


async def test_single_account_action_still_needs_a_name_while_one_is_down(
    hass: HomeAssistant, one_account_down
) -> None:
    """Otherwise send_email would quietly go out from whichever account happens to be up."""
    me, down_entry = one_account_down
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "send_email", {"to": "a@example.com", "subject": "Hi", "body": "Hello"}, blocking=True
        )
    assert err.value.translation_key == "config_entry_required"
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "send_email",
            {"config_entry_id": down_entry.entry_id, "to": "a@example.com", "subject": "Hi", "body": "Hello"},
            blocking=True,
        )
    assert err.value.translation_key == "entry_not_loaded"
    me.send_email.assert_not_awaited()


# --- single-account actions ---


@pytest.mark.parametrize(
    ("service", "data", "return_response"),
    [
        ("send_notification", {"title": "Dryer", "body": "Done"}, True),
        ("get_notification_answer", {"notification_id": 7, "timeout": 0}, True),
        ("list_scheduled_notifications", {}, True),
        ("cancel_scheduled_notification", {"scheduled_notification_id": 3}, False),
        ("cancel_notifications", {"notification_id": 7}, False),
        ("send_email", {"to": "a@example.com", "subject": "Hi", "body": "Hello"}, False),
    ],
)
async def test_single_account_actions_need_config_entry_id(
    hass: HomeAssistant, two_accounts, service: str, data: dict, return_response: bool
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=return_response)
    assert err.value.translation_key == "config_entry_required"
    assert err.value.translation_placeholders == {"service": f"{DOMAIN}.{service}"}


async def test_send_notification_response_from_the_named_account(hass: HomeAssistant, two_accounts) -> None:
    (_, me), (anna_entry, anna) = two_accounts
    anna.create_notification.return_value = {"id": 42, "answerable": True}
    response = await hass.services.async_call(
        DOMAIN,
        "send_notification",
        {"config_entry_id": anna_entry.entry_id, "title": "Gate", "body": "Open?"},
        blocking=True,
        return_response=True,
    )
    assert response == {"notification_id": 42, "answerable": True}
    me.create_notification.assert_not_awaited()


async def test_send_email_with_config_entry_id(hass: HomeAssistant, two_accounts) -> None:
    (me_entry, me), (_, anna) = two_accounts
    await hass.services.async_call(
        DOMAIN,
        "send_email",
        {"config_entry_id": me_entry.entry_id, "to": "a@example.com", "subject": "Hi", "body": "Hello"},
        blocking=True,
    )
    me.send_email.assert_awaited_once()
    anna.send_email.assert_not_awaited()


# --- widgets ---


async def test_delete_widget_by_entity_uses_the_owning_account(hass: HomeAssistant, two_accounts) -> None:
    (_, me), (anna_entry, anna) = two_accounts
    hass.states.async_set("sensor.users", "42")
    manager = WidgetManager(hass, anna, [make_widget_config(slug="ha-users", entity_id="sensor.users")], anna_entry)
    await manager.async_start()
    hass.data[DOMAIN][anna_entry.entry_id]["widget_manager"] = manager

    await hass.services.async_call(DOMAIN, "delete_widget", {"entity_id": "sensor.users"}, blocking=True)

    anna.delete_widget.assert_awaited_once_with("ha-users")
    me.delete_widget.assert_not_awaited()
    await manager.async_stop()
