"""Mutes: the client calls, the mute/unmute actions and the per-account pause switch."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pushward.api import (
    PushWardAccountPermissionError,
    PushWardApiClient,
    PushWardForbiddenError,
    PushWardQuotaExceededError,
)
from custom_components.pushward.const import (
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    DOMAIN,
    MAX_RETRIES,
    USAGE_LIMIT_RESOURCES,
    USAGE_UPDATE_INTERVAL,
)
from custom_components.pushward.quota import QuotaGate

from .conftest import (
    async_setup_with_api,
    make_api_client,
    make_mock_response,
    make_mock_session,
    make_quota_error,
    make_usage_payload,
)
from .server_contract import assert_valid_mute, assert_valid_mute_request

NOW = datetime(2026, 11, 25, 20, 0, tzinfo=UTC)
QUIET_HOURS = {"start_minute": 1320, "end_minute": 420, "time_zone": "Europe/Warsaw"}
FORBIDDEN = PushWardAccountPermissionError("insufficient permissions: requires account:manage", status_code=403)


def _mute(mute_id: str = "m1", target: str = "*", applies_to: str = "both", **extra) -> dict:
    mute = {
        "id": mute_id,
        "target": target,
        "applies_to": applies_to,
        "mode": "silent",
        "created_at": "2026-11-25T19:00:00Z",
        **extra,
    }
    assert_valid_mute(mute)
    return mute


def _list_body(*mutes: dict) -> dict:
    return {"mutes": list(mutes)}


# --- client ---


async def test_list_mutes_reads_the_list() -> None:
    mutes = [_mute(), _mute("m2", "ha-washer", "notifications", schedule=QUIET_HOURS)]
    session = make_mock_session(make_mock_response(200, json_body=_list_body(*mutes)))

    assert await make_api_client(session).list_mutes() == mutes
    assert session.request.call_args.args[:2] == ("GET", "https://api.example.com/app/mutes")


async def test_list_mutes_reads_a_null_list_as_empty() -> None:
    session = make_mock_session(make_mock_response(200, json_body={"mutes": None}))
    assert await make_api_client(session).list_mutes() == []


async def test_create_mute_sends_a_body_the_server_accepts() -> None:
    created = _mute("m9", "ha-washer", "notifications", label="Washer")
    session = make_mock_session(make_mock_response(201, json_body=created))
    until = dt_util.utcnow() + timedelta(hours=1)

    result = await make_api_client(session).create_mute(
        "ha-washer", applies_to="notifications", mode="off", label="Washer", until=until
    )

    assert result == created
    assert session.request.call_args.args[:2] == ("POST", "https://api.example.com/app/mutes")
    body = session.request.call_args.kwargs["json"]
    assert_valid_mute_request(body)
    assert body == {
        "target": "ha-washer",
        "applies_to": "notifications",
        "mode": "off",
        "label": "Washer",
        "until": until.isoformat(),
    }


async def test_create_mute_leaves_out_what_was_not_given() -> None:
    session = make_mock_session(make_mock_response(201, json_body=_mute()))

    await make_api_client(session).create_mute("*", applies_to="both", mode="silent")

    body = session.request.call_args.kwargs["json"]
    assert_valid_mute_request(body)
    assert body == {"target": "*", "applies_to": "both", "mode": "silent"}


async def test_delete_mute_takes_a_mute_already_gone() -> None:
    session = make_mock_session(make_mock_response(404, text='{"code": "mute.not_found"}'))

    await make_api_client(session).delete_mute("m1")

    assert session.request.call_args.args[:2] == ("DELETE", "https://api.example.com/app/mutes/m1")


@pytest.mark.parametrize(
    ("applies_to", "deleted"),
    [(None, ["m1", "m2"]), ("notifications", ["m2"]), ("activities", [])],
)
async def test_unmute_deletes_only_matching_rows_without_a_schedule(applies_to, deleted) -> None:
    listed = _list_body(
        _mute("m1"),
        _mute("m2", applies_to="notifications", until="2026-11-26T07:00:00Z"),
        _mute("m3", schedule=QUIET_HOURS),
        _mute("m4", "ha-washer"),
    )
    session = make_mock_session(make_mock_response(200, json_body=listed), *[make_mock_response(204) for _ in deleted])

    assert await make_api_client(session).unmute("*", applies_to) == len(deleted)

    requests = [call.args[:2] for call in session.request.call_args_list]
    assert requests == [
        ("GET", "https://api.example.com/app/mutes"),
        *[("DELETE", f"https://api.example.com/app/mutes/{mute_id}") for mute_id in deleted],
    ]


async def test_mute_403_is_an_account_permission_error() -> None:
    body = json.dumps({"code": "integration_key.permission_denied", "detail": "insufficient permissions"})
    for call in ("list_mutes", "create_mute", "delete_mute"):
        client = make_api_client(make_mock_session(make_mock_response(403, text=body)))
        args = {"list_mutes": (), "create_mute": ("*",), "delete_mute": ("m1",)}[call]
        kwargs = {"applies_to": "both", "mode": "silent"} if call == "create_mute" else {}
        with pytest.raises(PushWardAccountPermissionError) as err:
            await getattr(client, call)(*args, **kwargs)
        assert isinstance(err.value, PushWardForbiddenError)


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_mute_calls_retry_a_rate_limit_429(mock_sleep) -> None:
    session = make_mock_session(
        make_mock_response(429, text='{"code": "rate_limit.exceeded"}', headers={"Retry-After": "2"}),
        make_mock_response(200, json_body=_list_body(_mute())),
    )

    assert await make_api_client(session).list_mutes() == [_mute()]
    assert session.request.call_count == 2
    mock_sleep.assert_awaited_once_with(2.0)


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_mute_quota_429_is_typed_and_not_retried(mock_sleep) -> None:
    session = make_mock_session(make_mock_response(429, text='{"code": "quota.exceeded", "kind": "notifications"}'))

    with pytest.raises(PushWardQuotaExceededError):
        await make_api_client(session).create_mute("*", applies_to="both", mode="silent")
    assert session.request.call_count == 1


async def test_a_paused_quota_does_not_hold_back_mutes(hass: HomeAssistant) -> None:
    """Mutes are not metered: with every quota kind paused they still go out."""
    gate = QuotaGate(hass, "mutes_entry")
    for resource in USAGE_LIMIT_RESOURCES:
        gate.arm(make_quota_error(resource.kind, reset_at=dt_util.utcnow() + timedelta(days=3)))
    session = make_mock_session(
        make_mock_response(200, json_body=_list_body()),
        make_mock_response(201, json_body=_mute()),
        make_mock_response(204),
    )
    client = PushWardApiClient(session, "https://api.example.com", "k", quota_gate=gate)

    await client.list_mutes()
    await client.create_mute("*", applies_to="both", mode="silent")
    await client.delete_mute("m1")

    assert session.request.call_count == 3
    assert set(gate.snapshot()) == {resource.kind for resource in USAGE_LIMIT_RESOURCES}
    gate.async_shutdown()


# --- integration setup helpers ---


def _mock_api(*mutes: dict, list_error: Exception | None = None) -> AsyncMock:
    api = AsyncMock()
    api.get_me = AsyncMock(return_value=make_usage_payload())
    api.list_mutes = AsyncMock(side_effect=list_error) if list_error else AsyncMock(return_value=list(mutes))
    api.create_mute = AsyncMock(return_value=_mute())
    api.unmute = AsyncMock(return_value=1)
    return api


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: "test-key"},
        version=2,
        unique_id="user-123",
    )


async def _setup(hass: HomeAssistant, api: AsyncMock) -> MockConfigEntry:
    entry = _entry()
    await async_setup_with_api(hass, entry, api)
    return entry


def _me(account: str) -> dict:
    """An /auth/me body whose key has ``account`` as its Account permission."""
    return make_usage_payload(integration_key={"id": "k1", "name": "HA", "permissions": {"account": account}})


async def _tick(hass: HomeAssistant, freezer, seconds: int = USAGE_UPDATE_INTERVAL + 1) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _switch_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_pause_notifications")
    assert entity_id is not None
    return entity_id


# --- mute / unmute actions ---


async def test_mute_action_defaults(hass: HomeAssistant) -> None:
    api = _mock_api()
    await _setup(hass, api)

    await hass.services.async_call(DOMAIN, "mute", {"target": "ha-washer"}, blocking=True)

    api.create_mute.assert_awaited_once_with("ha-washer", applies_to="both", mode="silent", label=None, until=None)
    # Only a "*" change can move the pause switch, so nothing is re-read here.
    assert api.list_mutes.await_count == 1


async def test_mute_action_passes_every_field(hass: HomeAssistant) -> None:
    api = _mock_api()
    await _setup(hass, api)

    await hass.services.async_call(
        DOMAIN,
        "mute",
        {"target": "ha-washer", "applies_to": "activities", "mode": "off", "label": "Washer"},
        blocking=True,
    )

    api.create_mute.assert_awaited_once_with(
        "ha-washer", applies_to="activities", mode="off", label="Washer", until=None
    )


async def test_mute_action_reads_an_unquoted_yaml_off(hass: HomeAssistant) -> None:
    api = _mock_api()
    await _setup(hass, api)

    await hass.services.async_call(DOMAIN, "mute", {"target": "*", "mode": False}, blocking=True)

    assert api.create_mute.await_args.kwargs["mode"] == "off"


async def test_mute_action_duration_ends_that_long_from_now(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    api = _mock_api()
    await _setup(hass, api)

    await hass.services.async_call(DOMAIN, "mute", {"target": "*", "duration": {"hours": 2}}, blocking=True)

    assert api.create_mute.await_args.kwargs["until"] == NOW + timedelta(hours=2)


async def test_mute_action_until_is_read_in_home_assistant_time(hass: HomeAssistant) -> None:
    api = _mock_api()
    await _setup(hass, api)

    await hass.services.async_call(DOMAIN, "mute", {"target": "*", "until": "2026-11-26 07:00:00"}, blocking=True)

    local = datetime(2026, 11, 26, 7, 0, tzinfo=dt_util.get_default_time_zone())
    assert api.create_mute.await_args.kwargs["until"] == dt_util.as_utc(local)


@pytest.mark.parametrize(
    "data",
    [
        {"target": "*", "duration": {"hours": 1}, "until": "2026-11-26 07:00:00"},
        {"target": "*", "duration": {"seconds": 0}},
        {"target": ""},
        {"target": "x" * 129},
        {"target": "*", "applies_to": "widgets"},
        {"target": "*", "mode": "loud"},
        {"target": "*", "label": "x" * 101},
        {},
    ],
    ids=["duration-and-until", "zero-duration", "blank", "long", "applies_to", "mode", "label", "no-target"],
)
async def test_mute_action_refuses_bad_input(hass: HomeAssistant, data: dict) -> None:
    api = _mock_api()
    await _setup(hass, api)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "mute", data, blocking=True)
    api.create_mute.assert_not_awaited()


async def test_mute_action_on_everything_rereads_the_pause_switch(hass: HomeAssistant) -> None:
    api = _mock_api()
    entry = await _setup(hass, api)
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_OFF

    api.list_mutes.return_value = [_mute()]
    await hass.services.async_call(DOMAIN, "mute", {"target": "*"}, blocking=True)
    await hass.async_block_till_done()

    assert api.list_mutes.await_count == 2
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_ON


@pytest.mark.parametrize(
    ("data", "args"),
    [
        ({"target": "*"}, ("*", None)),
        ({"target": "ha-washer", "applies_to": "notifications"}, ("ha-washer", "notifications")),
    ],
)
async def test_unmute_action(hass: HomeAssistant, data: dict, args: tuple) -> None:
    api = _mock_api()
    await _setup(hass, api)

    await hass.services.async_call(DOMAIN, "unmute", data, blocking=True)

    api.unmute.assert_awaited_once_with(*args)


async def test_unmute_action_with_nothing_to_remove_is_not_an_error(hass: HomeAssistant) -> None:
    """The real client: nothing matches, so it lists and deletes nothing."""
    session = make_mock_session(
        make_mock_response(200, json_body=_list_body()),
        make_mock_response(200, json_body=_list_body(_mute("m3", schedule=QUIET_HOURS))),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: "test-key"},
        version=2,
        unique_id="user-123",
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.pushward.async_get_clientsession", return_value=session),
        patch.object(PushWardApiClient, "get_me", AsyncMock(return_value=make_usage_payload())),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(DOMAIN, "unmute", {"target": "ha-washer"}, blocking=True)

    assert [call.args[0] for call in session.request.call_args_list] == ["GET", "GET"]


@pytest.mark.parametrize(("service", "data"), [("mute", {"target": "*"}), ("unmute", {"target": "*"})])
async def test_mute_actions_explain_a_missing_account_permission(hass: HomeAssistant, service: str, data: dict) -> None:
    api = _mock_api()
    api.create_mute.side_effect = FORBIDDEN
    api.unmute.side_effect = FORBIDDEN
    await _setup(hass, api)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
    assert err.value.translation_key == "account_permission_required"


# --- pause switch ---


@pytest.mark.parametrize(
    ("mutes", "state"),
    [
        ([], STATE_OFF),
        ([_mute()], STATE_ON),
        ([_mute(until="2026-11-25T21:00:00Z")], STATE_ON),
        ([_mute(applies_to="notifications")], STATE_ON),
        ([_mute(schedule=QUIET_HOURS)], STATE_OFF),
        ([_mute(target="ha-washer")], STATE_OFF),
    ],
    ids=["none", "until-removed", "timed", "notifications-only", "quiet-hours-only", "other-target"],
)
async def test_switch_state_comes_from_the_mute_list(hass: HomeAssistant, freezer, mutes: list, state: str) -> None:
    freezer.move_to(NOW)
    entry = await _setup(hass, _mock_api(*mutes))
    assert hass.states.get(_switch_id(hass, entry)).state == state


async def test_switch_on_mutes_everything_and_rereads(hass: HomeAssistant) -> None:
    api = _mock_api()
    entry = await _setup(hass, api)
    api.list_mutes.return_value = [_mute()]

    await hass.services.async_call("switch", "turn_on", {"entity_id": _switch_id(hass, entry)}, blocking=True)
    await hass.async_block_till_done()

    api.create_mute.assert_awaited_once_with("*", applies_to="both", mode="silent")
    assert api.list_mutes.await_count == 2
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_ON


async def test_switch_off_unmutes_everything_and_rereads(hass: HomeAssistant) -> None:
    api = _mock_api(_mute(), _mute("m3", schedule=QUIET_HOURS))
    entry = await _setup(hass, api)
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_ON
    api.list_mutes.return_value = [_mute("m3", schedule=QUIET_HOURS)]

    await hass.services.async_call("switch", "turn_off", {"entity_id": _switch_id(hass, entry)}, blocking=True)
    await hass.async_block_till_done()

    api.unmute.assert_awaited_once_with("*")
    assert api.list_mutes.await_count == 2
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_OFF


async def test_switch_shows_a_quick_second_toggle_at_once(hass: HomeAssistant) -> None:
    api = _mock_api()
    entry = await _setup(hass, api)
    switch = _switch_id(hass, entry)

    api.list_mutes.return_value = [_mute()]
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    assert hass.states.get(switch).state == STATE_ON

    api.list_mutes.return_value = []
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    assert hass.states.get(switch).state == STATE_OFF

    api.list_mutes.return_value = [_mute()]
    await hass.services.async_call(DOMAIN, "mute", {"target": "*"}, blocking=True)
    assert hass.states.get(switch).state == STATE_ON
    assert api.list_mutes.await_count == 4


async def test_switch_on_keeps_a_pause_set_elsewhere(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    api = _mock_api(_mute(mode="off", until="2026-11-26T07:00:00Z"))
    entry = await _setup(hass, api)

    await hass.services.async_call("switch", "turn_on", {"entity_id": _switch_id(hass, entry)}, blocking=True)

    api.create_mute.assert_not_awaited()
    assert api.list_mutes.await_count == 2
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_ON


@pytest.mark.parametrize("applies_to", ["activities", "notifications"])
async def test_switch_on_adds_a_full_pause_beside_a_partial_one(hass: HomeAssistant, applies_to: str) -> None:
    partial = _mute(applies_to=applies_to)
    api = _mock_api(partial)
    entry = await _setup(hass, api)
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_ON
    api.list_mutes.return_value = [partial, _mute("m2")]

    await hass.services.async_call("switch", "turn_on", {"entity_id": _switch_id(hass, entry)}, blocking=True)

    api.create_mute.assert_awaited_once_with("*", applies_to="both", mode="silent")
    api.unmute.assert_not_awaited()
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_ON


async def test_switch_on_mutes_when_the_pause_it_showed_is_gone(hass: HomeAssistant) -> None:
    api = _mock_api()
    api.list_mutes.side_effect = [[_mute()], [], [_mute("m2")]]
    entry = await _setup(hass, api)

    await hass.services.async_call("switch", "turn_on", {"entity_id": _switch_id(hass, entry)}, blocking=True)

    api.create_mute.assert_awaited_once_with("*", applies_to="both", mode="silent")
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_ON


async def test_switch_turns_off_when_the_timed_mute_ends(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    ends = NOW + timedelta(minutes=5)
    api = _mock_api(
        _mute(until=ends.isoformat()), _mute("m2", applies_to="notifications", until="2026-11-25T20:02:00Z")
    )
    entry = await _setup(hass, api)
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_ON

    freezer.move_to(NOW + timedelta(minutes=3))
    async_fire_time_changed(hass, NOW + timedelta(minutes=3))
    await hass.async_block_till_done()
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_ON

    freezer.move_to(ends + timedelta(seconds=1))
    async_fire_time_changed(hass, ends + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert hass.states.get(_switch_id(hass, entry)).state == STATE_OFF
    assert api.list_mutes.await_count == 1


async def test_switch_polls_slowly(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    api = _mock_api()
    await _setup(hass, api)

    assert api.list_mutes.await_args.kwargs == {"attempts": 1, "lane": "background"}

    freezer.move_to(NOW + timedelta(seconds=USAGE_UPDATE_INTERVAL - 1))
    async_fire_time_changed(hass, NOW + timedelta(seconds=USAGE_UPDATE_INTERVAL - 1))
    await hass.async_block_till_done()
    assert api.list_mutes.await_count == 1

    freezer.move_to(NOW + timedelta(seconds=USAGE_UPDATE_INTERVAL + 1))
    async_fire_time_changed(hass, NOW + timedelta(seconds=USAGE_UPDATE_INTERVAL + 1))
    await hass.async_block_till_done()
    assert api.list_mutes.await_count == 2
    assert api.list_mutes.await_args.kwargs == {"attempts": MAX_RETRIES, "lane": "background"}


async def test_switch_without_the_account_permission_is_unavailable(
    hass: HomeAssistant, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    api = _mock_api(list_error=FORBIDDEN)
    with caplog.at_level(logging.WARNING):
        entry = await _setup(hass, api)
        await _tick(hass, freezer)

    assert hass.states.get(_switch_id(hass, entry)).state == STATE_UNAVAILABLE
    warnings = [r for r in caplog.records if "set Account to Full management" in r.getMessage()]
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    sensor = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_notifications_used")
    assert hass.states.get(sensor).state == "137"
    assert hass.services.has_service(DOMAIN, "mute")


async def test_switch_sends_nothing_while_auth_me_says_the_key_has_no_account_permission(
    hass: HomeAssistant, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    freezer.move_to(NOW)
    api = _mock_api()
    api.get_me.return_value = _me("none")
    with caplog.at_level(logging.WARNING):
        entry = await _setup(hass, api)
        await _tick(hass, freezer)

    api.list_mutes.assert_not_awaited()
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_UNAVAILABLE
    assert caplog.text.count("set Account to Full management") == 1

    # Granted in the app: the usage poll sees it, the next mute poll reads again.
    api.get_me.return_value = _me("manage")
    await _tick(hass, freezer)
    await _tick(hass, freezer)

    api.list_mutes.assert_awaited()
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_OFF


async def test_a_disabled_switch_reads_nothing(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    api = _mock_api()
    entry = _entry()
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "switch",
        DOMAIN,
        f"{entry.entry_id}_pause_notifications",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    await async_setup_with_api(hass, entry, api)
    await _tick(hass, freezer)

    api.list_mutes.assert_not_awaited()


async def test_switch_explains_a_missing_manage_permission(hass: HomeAssistant) -> None:
    api = _mock_api()
    api.create_mute.side_effect = FORBIDDEN
    entry = await _setup(hass, api)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call("switch", "turn_on", {"entity_id": _switch_id(hass, entry)}, blocking=True)
    assert err.value.translation_key == "account_permission_required"
    assert hass.states.get(_switch_id(hass, entry)).state == STATE_OFF
