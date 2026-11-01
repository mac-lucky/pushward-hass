"""The notify entity: notify.send_message and notify groups reach the PushWard account."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.api import PushWardApiClient
from custom_components.pushward.const import (
    CONF_E2E_KEY,
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    DOMAIN,
)
from custom_components.pushward.e2e import open_envelope

from .conftest import (
    async_setup_with_api,
    make_mock_response,
    make_mock_session,
    make_quota_error,
    make_usage_payload,
)
from .server_contract import assert_valid_notification_request

_E2E_KEY = bytes(range(32))


def _entry(unique_id: str = "user-123", title: str = "PushWard (Test)", **kwargs) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: f"key-{unique_id}"},
        version=2,
        unique_id=unique_id,
        **kwargs,
    )


def _mock_api() -> AsyncMock:
    api = AsyncMock()
    api.get_me = AsyncMock(return_value=make_usage_payload())
    api.create_notification = AsyncMock(return_value={"id": 7, "answerable": False})
    return api


def _notify_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("notify", DOMAIN, f"{entry.entry_id}_notify")
    assert entity_id is not None
    return entity_id


async def _send(hass: HomeAssistant, entity_id: str, **data) -> None:
    await hass.services.async_call("notify", "send_message", {"entity_id": entity_id, **data}, blocking=True)


async def test_one_notify_entity_per_account_on_the_account_device(hass: HomeAssistant) -> None:
    entry = _entry()
    await async_setup_with_api(hass, entry, _mock_api())

    entity = er.async_get(hass).async_get(_notify_entity_id(hass, entry))
    assert entity.translation_key == "push_notification"
    assert entity.entity_id == "notify.pushward_test_push_notification"
    # Same service device as the usage sensors.
    sensor_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_notifications_used")
    assert er.async_get(hass).async_get(sensor_id).device_id == entity.device_id


async def test_send_message_sends_a_notification(hass: HomeAssistant) -> None:
    api = _mock_api()
    entry = _entry()
    await async_setup_with_api(hass, entry, api)

    entity_id = _notify_entity_id(hass, entry)
    await _send(hass, entity_id, message="The front door was opened.", title="Front door")

    api.create_notification.assert_awaited_once_with(title="Front door", body="The front door was opened.")
    # The entity's state is the time of the last message.
    assert hass.states.get(entity_id).state not in ("unknown", "unavailable")


@pytest.mark.parametrize("title", [None, "", "   "])
async def test_send_message_without_a_title_uses_the_location_name(hass: HomeAssistant, title: str | None) -> None:
    api = _mock_api()
    entry = _entry()
    await async_setup_with_api(hass, entry, api)
    hass.config.location_name = "Lake House"

    data = {"message": "Leak under the sink"}
    if title is not None:
        data["title"] = title
    await _send(hass, _notify_entity_id(hass, entry), **data)

    api.create_notification.assert_awaited_once_with(title="Lake House", body="Leak under the sink")


async def test_send_message_without_a_location_name_falls_back(hass: HomeAssistant) -> None:
    api = _mock_api()
    entry = _entry()
    await async_setup_with_api(hass, entry, api)
    hass.config.location_name = ""

    await _send(hass, _notify_entity_id(hass, entry), message="Hi")

    assert api.create_notification.call_args.kwargs["title"] == "Home Assistant"


async def test_send_message_goes_to_its_own_account_only(hass: HomeAssistant) -> None:
    me, anna = _mock_api(), _mock_api()
    me_entry = _entry()
    anna_entry = _entry("user-456", "PushWard (Anna)")
    await async_setup_with_api(hass, me_entry, me)
    await async_setup_with_api(hass, anna_entry, anna)

    await _send(hass, _notify_entity_id(hass, anna_entry), message="Dinner is ready")

    anna.create_notification.assert_awaited_once()
    me.create_notification.assert_not_awaited()


async def test_send_message_at_quota_raises_a_validation_error(hass: HomeAssistant) -> None:
    api = _mock_api()
    api.create_notification = AsyncMock(side_effect=make_quota_error("notifications"))
    entry = _entry()
    await async_setup_with_api(hass, entry, api)

    with pytest.raises(ServiceValidationError):
        await _send(hass, _notify_entity_id(hass, entry), message="Hi")


async def test_send_message_is_sealed_when_the_entry_has_an_e2e_key(hass: HomeAssistant) -> None:
    """The real client path: the entry's key seals title and body, nothing else carries them."""
    session = make_mock_session(make_mock_response(201, json_body={"id": 7}))
    entry = _entry(options={CONF_E2E_KEY: _E2E_KEY.hex()})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.pushward.async_get_clientsession", return_value=session),
        patch.object(PushWardApiClient, "get_me", AsyncMock(return_value=make_usage_payload())),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await _send(hass, _notify_entity_id(hass, entry), message="Opened", title="Door")

    method, url = session.request.call_args.args[:2]
    assert method == "POST"
    assert url.endswith("/notifications")
    body = session.request.call_args.kwargs["json"]
    assert_valid_notification_request(body)
    assert set(body) == {"encrypted", "push"}
    assert open_envelope(_E2E_KEY, body["encrypted"]) == {"title": "Door", "body": "Opened"}
