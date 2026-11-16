"""Tests for the sidebar panel's registration and its websocket commands."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from homeassistant import config_entries
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward import panel
from custom_components.pushward.api import PushWardApiError, PushWardAuthError, PushWardQuotaExceededError
from custom_components.pushward.const import (
    CONF_BATTERY_DEVICES,
    CONF_CHARGING_ENTITY,
    CONF_ENTITY_ID,
    CONF_HISTORY_PERIOD,
    CONF_IMAGE_URL,
    CONF_INTEGRATION_KEY,
    CONF_LABEL,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PRIORITY,
    CONF_PROGRESS_ENTITY,
    CONF_SERVER_URL,
    CONF_SLUG,
    CONF_SOUND,
    CONF_STAT_ROWS,
    CONF_TEMPLATE,
    CONF_TILES,
    CONF_TODO_LEVEL,
    CONF_TODO_MAX_SCHEDULED,
    CONF_UPDATE_INTERVAL,
    CONF_WIDGET_NAME,
    CONF_WIDGET_TEMPLATE,
    DEFAULT_SERVER_URL,
    DOMAIN,
    QUOTA_KIND_LIVE_ACTIVITY_UPDATES,
    QUOTA_KIND_NOTIFICATIONS,
    QUOTA_KIND_WIDGET_UPDATES,
    SUBENTRY_TYPE_ENTITY,
    SUBENTRY_TYPE_TODO,
    SUBENTRY_TYPE_WIDGET,
    WIDGET_TEMPLATE_BATTERY,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_TREND,
)
from custom_components.pushward.e2e import E2EError, open_envelope
from custom_components.pushward.panel import PANEL_URL_PATH, STATIC_URL_PATH
from custom_components.pushward.websocket import MAX_PAYLOAD_BYTES

from .conftest import (
    IMAGE_URL,
    async_setup_with_api,
    make_entity_config,
    make_usage_payload,
    make_widget_config,
    patch_image_download,
)
from .test_config_flow import _mock_core_input, _mock_details_input, _nest_widget

WASHER = "binary_sensor.washer"
TODO_LIST = "todo.shopping"
EMPTY_LIST = "todo.chores"
NOW = "2026-10-01T08:00:00+00:00"


def _api() -> AsyncMock:
    api = AsyncMock()
    api.get_me = AsyncMock(return_value=make_usage_payload())
    api.create_notification = AsyncMock(return_value={"id": 7, "status": "scheduled"})
    api.list_scheduled_notifications = AsyncMock(return_value=[])
    api.get_scheduled_notification = AsyncMock(return_value=None)
    api._request_with_retry = AsyncMock(return_value=(201, {"slug": "ha-x"}))
    api.e2e_key = None
    return api


def _todo_service(hass: HomeAssistant, items: dict[str, list[dict]]) -> None:
    """A fake todo.get_items over a fixed set of lists."""

    async def get_items(call: ServiceCall) -> dict:
        entity_ids = call.data["entity_id"]
        entity_ids = [entity_ids] if isinstance(entity_ids, str) else entity_ids
        return {eid: {"items": items.get(eid, [])} for eid in entity_ids}

    hass.services.async_register("todo", "get_items", get_items, supports_response=SupportsResponse.ONLY)
    for entity_id in items:
        hass.states.async_set(entity_id, str(len(items[entity_id])), {"friendly_name": entity_id.split(".")[1]})


def _entry(unique_id: str = "user-123") -> MockConfigEntry:
    entity = make_entity_config()
    widget = make_widget_config()
    return MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: f"hlk_{unique_id}"},
        version=2,
        unique_id=unique_id,
        subentries_data=[
            ConfigSubentryData(
                data=entity, subentry_type=SUBENTRY_TYPE_ENTITY, title="Washer", unique_id=entity[CONF_ENTITY_ID]
            ),
            ConfigSubentryData(
                data=widget, subentry_type=SUBENTRY_TYPE_WIDGET, title="Users", unique_id=widget[CONF_SLUG]
            ),
            ConfigSubentryData(
                data={CONF_ENTITY_ID: TODO_LIST, CONF_TODO_LEVEL: "active"},
                subentry_type=SUBENTRY_TYPE_TODO,
                title="Shopping",
                unique_id=f"todo:{TODO_LIST}",
            ),
        ],
    )


def _subentry_id(entry: MockConfigEntry, subentry_type: str) -> str:
    return next(sub.subentry_id for sub in entry.subentries.values() if sub.subentry_type == subentry_type)


@pytest.fixture
async def loaded(hass: HomeAssistant, freezer) -> tuple[MockConfigEntry, AsyncMock]:
    freezer.move_to(NOW)
    hass.states.async_set(WASHER, "on", {"friendly_name": "Washer"})
    hass.states.async_set("sensor.users", "42", {"friendly_name": "Users"})
    hass.states.async_set("sensor.temp", "21.5", {"friendly_name": "Temperature"})
    _todo_service(
        hass,
        {
            TODO_LIST: [
                {"uid": "later", "summary": "Bread", "status": "needs_action", "due": "2026-10-03T09:00:00+00:00"},
                {"uid": "soon", "summary": "Milk", "status": "needs_action", "due": "2026-10-02T09:00:00+00:00"},
            ],
            EMPTY_LIST: [{"uid": "undated", "summary": "Someday", "status": "needs_action"}],
        },
    )
    entry, api = _entry(), _api()
    await async_setup_with_api(hass, entry, api)
    return entry, api


@pytest.fixture
async def ws(hass: HomeAssistant, loaded, hass_ws_client):
    client = await hass_ws_client(hass)

    async def call(msg_type: str, **fields: Any) -> dict:
        await client.send_json_auto_id({"type": msg_type, **fields})
        return await client.receive_json()

    return call


def _loaded_data(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    return hass.data[DOMAIN][entry.entry_id]


# --- panel ---


async def test_panel_lives_while_any_account_is_loaded(hass: HomeAssistant) -> None:
    panels = hass.data.setdefault(frontend.DATA_PANELS, {})
    first, second = _entry("user-1"), _entry("user-2")
    await async_setup_with_api(hass, first, _api())
    registered = panels[PANEL_URL_PATH]
    assert registered.require_admin
    assert registered.sidebar_title == "PushWard"
    assert registered.config["_panel_custom"]["name"] == "pushward-panel"
    version = json.loads((Path(panel.__file__).parent / "manifest.json").read_text())["version"]
    assert registered.config["_panel_custom"]["module_url"] == f"{STATIC_URL_PATH}/pushward-panel.js?v={version}"

    await async_setup_with_api(hass, second, _api())
    await hass.config_entries.async_unload(first.entry_id)
    assert PANEL_URL_PATH in panels
    await hass.config_entries.async_unload(second.entry_id)
    assert PANEL_URL_PATH not in panels

    [static] = [
        r
        for r in hass.http.app.router.resources()
        if isinstance(r, web.StaticResource) and r.canonical == STATIC_URL_PATH
    ]
    assert static.get_info()["directory"] == Path(panel.__file__).parent / "frontend"


async def test_panel_survives_an_account_set_up_while_the_last_one_stops(hass: HomeAssistant) -> None:
    panels = hass.data.setdefault(frontend.DATA_PANELS, {})
    first, second = _entry("user-1"), _entry("user-2")
    await async_setup_with_api(hass, first, _api())
    manager = hass.data[DOMAIN][first.entry_id]["manager"]
    stopping, release = asyncio.Event(), asyncio.Event()
    real_stop = manager.async_stop

    async def slow_stop() -> None:
        stopping.set()
        await release.wait()
        await real_stop()

    manager.async_stop = slow_stop
    unload = hass.async_create_task(hass.config_entries.async_unload(first.entry_id))
    await stopping.wait()

    second.add_to_hass(hass)
    with patch("custom_components.pushward.PushWardApiClient", return_value=_api()):
        assert await hass.config_entries.async_setup(second.entry_id)
    release.set()
    await unload

    assert second.state is config_entries.ConfigEntryState.LOADED
    assert PANEL_URL_PATH in panels
    await panel.async_register_panel(hass)  # registering a present panel again is a no-op


def test_panel_strings_match_the_panel_script() -> None:
    """Every string the panel asks for has an en.json label, and every label is still used."""
    root = Path(panel.__file__).parent
    script = (root / "frontend" / "pushward-panel.js").read_text()
    used = set(re.findall(r'_p\(\s*"([a-z_]+)"', script)) | set(re.findall(r'"(tab_[a-z_]+)"', script))
    labels = json.loads((root / "translations" / "en.json").read_text())["selector"]["panel"]["options"]
    assert used == set(labels)


# --- pushward/preview ---


async def test_preview_saved_running_activity_is_its_last_pushed_frame(hass: HomeAssistant, loaded, ws) -> None:
    entry, _ = loaded
    tracked = _loaded_data(hass, entry)["manager"]._tracked[WASHER]
    assert tracked.last_content is not None
    tracked.last_content = {**tracked.last_content, "lines": [{"text": "kept"}]}

    reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_id=_subentry_id(entry, SUBENTRY_TYPE_ENTITY))

    assert reply["success"]
    assert reply["result"] == {
        "kind": "activity",
        "payload": {"name": "Washer", "content": tracked.last_content},
        "errors": None,
        "available": True,
        "entity_ids": [WASHER],
    }


async def test_preview_saved_idle_activity_maps_fresh(hass: HomeAssistant, loaded, ws) -> None:
    entry, _ = loaded
    _loaded_data(hass, entry)["manager"]._tracked[WASHER].last_content = None

    reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_id=_subentry_id(entry, SUBENTRY_TYPE_ENTITY))

    content = reply["result"]["payload"]["content"]
    assert content["template"] == "generic"
    assert content["icon"] == "washer"


async def test_preview_draft_activity_maps_without_network(hass: HomeAssistant, loaded, ws) -> None:
    entry, api = loaded
    api.reset_mock()
    draft = {
        **_mock_core_input(**{CONF_ENTITY_ID: "sensor.temp", CONF_TEMPLATE: "gauge"}),
        **_mock_details_input("gauge", **{CONF_IMAGE_URL: IMAGE_URL}),
    }

    with patch_image_download(b"not fetched") as session:
        reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_type=SUBENTRY_TYPE_ENTITY, data=draft)

    result = reply["result"]
    assert result["available"]
    assert result["payload"]["content"]["template"] == "gauge"
    assert "thumbhash" not in str(result["payload"]["content"])
    session.get.assert_not_called()
    assert api.mock_calls == []


async def test_preview_draft_activity_errors_match_the_details_step(hass: HomeAssistant, loaded, ws) -> None:
    entry, _ = loaded
    core = _mock_core_input(**{CONF_ENTITY_ID: "sensor.temp", CONF_TEMPLATE: "gauge"})
    details = _mock_details_input("gauge", **{CONF_MIN_VALUE: 100.0, CONF_MAX_VALUE: 50.0})

    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY), context={"source": config_entries.SOURCE_USER}
    )
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], user_input=core)
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], user_input=details)
    assert flow["errors"] == {CONF_MIN_VALUE: "invalid_gauge_range"}

    reply = await ws(
        "pushward/preview", entry_id=entry.entry_id, subentry_type=SUBENTRY_TYPE_ENTITY, data={**core, **details}
    )

    assert reply["result"] == {
        "kind": "activity",
        "payload": None,
        "errors": flow["errors"],
        "available": False,
        "entity_ids": [],
    }


@pytest.mark.parametrize(("field", "value"), [(CONF_UPDATE_INTERVAL, 0), (CONF_PRIORITY, 11), (CONF_SOUND, "bogus")])
async def test_preview_draft_activity_refuses_what_the_form_selectors_refuse(
    hass: HomeAssistant, loaded, ws, field: str, value: Any
) -> None:
    entry, _ = loaded
    core = _mock_core_input(**{CONF_ENTITY_ID: "sensor.temp"})
    details = _mock_details_input("generic", **{field: value})

    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ENTITY), context={"source": config_entries.SOURCE_USER}
    )
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], user_input=core)
    with pytest.raises(InvalidData) as refused:
        await hass.config_entries.subentries.async_configure(flow["flow_id"], user_input=details)
    assert refused.value.path[-1] == field

    reply = await ws(
        "pushward/preview", entry_id=entry.entry_id, subentry_type=SUBENTRY_TYPE_ENTITY, data={**core, **details}
    )

    assert reply["result"]["errors"] == {field: refused.value.error_message}
    assert reply["result"]["payload"] is None


async def test_preview_draft_todo_refuses_what_the_form_selectors_refuse(loaded, ws) -> None:
    entry, _ = loaded
    draft = {CONF_ENTITY_ID: TODO_LIST, CONF_TODO_MAX_SCHEDULED: 0}

    reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_type=SUBENTRY_TYPE_TODO, data=draft)

    assert set(reply["result"]["errors"]) == {CONF_TODO_MAX_SCHEDULED}
    assert reply["result"]["payload"] is None


async def test_preview_unavailable_entity(hass: HomeAssistant, loaded, ws) -> None:
    entry, _ = loaded
    draft = _mock_core_input(**{CONF_ENTITY_ID: "sensor.missing"})

    reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_type=SUBENTRY_TYPE_ENTITY, data=draft)

    assert reply["result"] == {
        "kind": "activity",
        "payload": None,
        "errors": None,
        "available": False,
        "entity_ids": ["sensor.missing"],
    }


async def test_preview_saved_widget_is_its_last_sent_content(hass: HomeAssistant, loaded, ws) -> None:
    entry, _ = loaded
    tracked = _loaded_data(hass, entry)["widget_manager"]._tracked["ha-users"]
    assert tracked.last_content is not None

    reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_id=_subentry_id(entry, SUBENTRY_TYPE_WIDGET))

    assert reply["result"]["kind"] == "widget"
    assert reply["result"]["payload"] == {"name": "Users", "template": "value", "content": tracked.last_content}


async def test_preview_saved_widget_without_its_entity_is_unavailable(hass: HomeAssistant, loaded, ws) -> None:
    entry, _ = loaded
    assert _loaded_data(hass, entry)["widget_manager"]._tracked["ha-users"].last_content is not None
    hass.states.async_remove("sensor.users")

    reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_id=_subentry_id(entry, SUBENTRY_TYPE_WIDGET))

    assert reply["result"] == {
        "kind": "widget",
        "payload": None,
        "errors": None,
        "available": False,
        "entity_ids": ["sensor.users"],
    }


async def test_preview_draft_widget(hass: HomeAssistant, loaded, ws) -> None:
    entry, api = loaded
    api.reset_mock()
    draft = {CONF_ENTITY_ID: "sensor.temp", CONF_WIDGET_TEMPLATE: "value", CONF_SLUG: "", CONF_WIDGET_NAME: "Draft"}

    reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_type=SUBENTRY_TYPE_WIDGET, data=draft)

    payload = reply["result"]["payload"]
    assert payload["name"] == "Draft"
    assert payload["template"] == "value"
    assert payload["content"]["value"] == 21.5
    assert api.mock_calls == []


async def test_preview_saved_trend_widget_has_its_points(hass: HomeAssistant, loaded, ws) -> None:
    trend = make_widget_config(
        **{CONF_ENTITY_ID: "sensor.temp", CONF_SLUG: "ha-temp", CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_TREND}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: "hlk_trend"},
        version=2,
        unique_id="user-trend",
        subentries_data=[
            ConfigSubentryData(data=trend, subentry_type=SUBENTRY_TYPE_WIDGET, title="Temp", unique_id="ha-temp")
        ],
    )
    await async_setup_with_api(hass, entry, _api())
    hass.states.async_set("sensor.temp", "22.5", {"friendly_name": "Temperature"})
    await hass.async_block_till_done()
    tracked = _loaded_data(hass, entry)["widget_manager"]._tracked["ha-temp"]
    assert tracked.last_content["points"] == [21.5, 22.5]
    buffer = list(tracked.points_buffer)

    reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_id=_subentry_id(entry, SUBENTRY_TYPE_WIDGET))

    assert reply["result"]["available"]
    assert reply["result"]["payload"]["content"] == tracked.last_content
    assert list(tracked.points_buffer) == buffer


async def test_preview_draft_trend_widget_reads_its_history(hass: HomeAssistant, loaded, ws) -> None:
    entry, api = loaded
    api.reset_mock()
    draft = {
        CONF_ENTITY_ID: "sensor.temp",
        CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_TREND,
        CONF_SLUG: "",
        **_nest_widget(WIDGET_TEMPLATE_TREND, {CONF_WIDGET_NAME: "Temp", CONF_HISTORY_PERIOD: 60}),
    }
    history = {"sensor.temp": [{"timestamp": 1000.0, "value": 19.0}, {"timestamp": 2000.0, "value": 20.0}]}

    with patch(
        "custom_components.pushward.widget_manager.async_recorder_states", AsyncMock(return_value=history)
    ) as read:
        reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_type=SUBENTRY_TYPE_WIDGET, data=draft)

    read.assert_awaited_once()
    assert reply["result"]["payload"]["content"]["points"] == [19.0, 20.0, 21.5]
    assert api.mock_calls == []


async def test_preview_draft_widget_errors_match_the_details_step(hass: HomeAssistant, loaded, ws) -> None:
    entry, _ = loaded
    step1 = {CONF_ENTITY_ID: "sensor.users", CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST}
    details = _nest_widget(WIDGET_TEMPLATE_STAT_LIST, {CONF_STAT_ROWS: [], CONF_WIDGET_NAME: "Stats"})

    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_WIDGET), context={"source": config_entries.SOURCE_USER}
    )
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], user_input=step1)
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], user_input=details)
    assert flow["errors"] == {CONF_STAT_ROWS: "stat_rows_required"}

    reply = await ws(
        "pushward/preview", entry_id=entry.entry_id, subentry_type=SUBENTRY_TYPE_WIDGET, data={**step1, **details}
    )

    assert reply["result"]["errors"] == flow["errors"]
    assert reply["result"]["payload"] is None


async def test_preview_saved_todo_is_the_reminder_the_manager_sends(hass: HomeAssistant, loaded, ws) -> None:
    entry, api = loaded
    sent = [c for c in api.create_notification.call_args_list if c.args[0] == "Milk"]
    assert sent, "the manager scheduled the next due item"
    args, kwargs = sent[0]

    reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_id=_subentry_id(entry, SUBENTRY_TYPE_TODO))

    payload = reply["result"]["payload"]
    assert reply["result"]["kind"] == "notification"
    assert payload == {
        "title": args[0],
        "body": args[1],
        **{key: kwargs[key] for key in payload if key not in ("title", "body")},
    }
    assert set(payload) == {
        "title",
        "body",
        "level",
        "thread_id",
        "collapse_id",
        "source",
        "source_display_name",
        "metadata",
        "actions",
    }


async def test_preview_draft_todo_without_due_items_is_a_sample(hass: HomeAssistant, loaded, ws) -> None:
    entry, api = loaded
    api.reset_mock()

    reply = await ws(
        "pushward/preview", entry_id=entry.entry_id, subentry_type=SUBENTRY_TYPE_TODO, data={CONF_ENTITY_ID: EMPTY_LIST}
    )

    payload = reply["result"]["payload"]
    assert payload["title"] == "Sample reminder"
    assert payload["source_display_name"] == "chores"
    assert api.mock_calls == []


async def test_preview_names_the_entities_each_subentry_reads(hass: HomeAssistant, ws) -> None:
    board = make_entity_config(
        **{
            CONF_ENTITY_ID: "binary_sensor.home",
            CONF_SLUG: "ha-board",
            CONF_TEMPLATE: "board",
            CONF_PROGRESS_ENTITY: "sensor.progress",
            CONF_TILES: [
                {CONF_LABEL: "CPU", CONF_ENTITY_ID: "sensor.cpu"},
                {CONF_LABEL: "Home", CONF_ENTITY_ID: "binary_sensor.home"},
            ],
        }
    )
    battery = make_widget_config(
        **{
            CONF_SLUG: "ha-batteries",
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_BATTERY,
            CONF_BATTERY_DEVICES: [
                {"name": "Phone", CONF_ENTITY_ID: "sensor.phone", CONF_CHARGING_ENTITY: "binary_sensor.charging"},
                {"name": "Watch", CONF_ENTITY_ID: "sensor.watch"},
            ],
        }
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: "hlk_bound"},
        version=2,
        unique_id="user-bound",
        subentries_data=[
            ConfigSubentryData(
                data=board, subentry_type=SUBENTRY_TYPE_ENTITY, title="Board", unique_id="binary_sensor.home"
            ),
            ConfigSubentryData(
                data=battery, subentry_type=SUBENTRY_TYPE_WIDGET, title="Batteries", unique_id="ha-batteries"
            ),
            ConfigSubentryData(
                data={CONF_ENTITY_ID: EMPTY_LIST},
                subentry_type=SUBENTRY_TYPE_TODO,
                title="Chores",
                unique_id=f"todo:{EMPTY_LIST}",
            ),
        ],
    )
    await async_setup_with_api(hass, entry, _api())

    async def bound(subentry_type: str) -> list[str]:
        reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_id=_subentry_id(entry, subentry_type))
        return reply["result"]["entity_ids"]

    assert await bound(SUBENTRY_TYPE_ENTITY) == ["binary_sensor.home", "sensor.progress", "sensor.cpu"]
    assert await bound(SUBENTRY_TYPE_WIDGET) == ["sensor.phone", "binary_sensor.charging", "sensor.watch"]
    assert await bound(SUBENTRY_TYPE_TODO) == [EMPTY_LIST]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"subentry_type": SUBENTRY_TYPE_ENTITY},
        {"data": {CONF_ENTITY_ID: WASHER}},
        {"subentry_id": "x", "data": {CONF_ENTITY_ID: WASHER}},
        {"subentry_type": SUBENTRY_TYPE_ENTITY, "data": {"no_entity": True}},
    ],
)
async def test_preview_needs_exactly_one_form(loaded, ws, fields: dict) -> None:
    entry, _ = loaded
    reply = await ws("pushward/preview", entry_id=entry.entry_id, **fields)
    assert reply["error"]["code"] == "invalid_format"


async def test_preview_unknown_subentry(loaded, ws) -> None:
    entry, _ = loaded
    reply = await ws("pushward/preview", entry_id=entry.entry_id, subentry_id="nope")
    assert reply["error"]["code"] == "not_found"


# --- pushward/send_test ---


async def test_send_test_activity_send_and_end(loaded, ws) -> None:
    entry, api = loaded
    api.reset_mock()
    content = {"template": "generic", "state": "Running"}

    reply = await ws(
        "pushward/send_test",
        entry_id=entry.entry_id,
        kind="activity",
        action="send",
        payload={"name": "Washer", "content": content},
    )
    assert reply["success"]
    api.create_activity.assert_awaited_once_with("ha-preview", "Washer", 1)
    api.update_activity.assert_awaited_once_with("ha-preview", "ongoing", content)

    reply = await ws("pushward/send_test", entry_id=entry.entry_id, kind="activity", action="end")
    assert reply["success"]
    api.update_activity.assert_awaited_with("ha-preview", "ended", {})


async def test_send_test_widget_send_and_end(loaded, ws) -> None:
    entry, api = loaded
    api.reset_mock()

    payload = {"name": "Users", "template": "value", "content": {"value": 42}}
    reply = await ws("pushward/send_test", entry_id=entry.entry_id, kind="widget", action="send", payload=payload)
    assert reply["success"]
    api.create_widget.assert_awaited_once_with("ha-preview-widget", "Users", "value", {"value": 42})

    reply = await ws("pushward/send_test", entry_id=entry.entry_id, kind="widget", action="end")
    assert reply["success"]
    api.delete_widget.assert_awaited_once_with("ha-preview-widget")


async def test_send_test_notification_uses_the_preview_collapse_id(loaded, ws) -> None:
    entry, api = loaded
    api.reset_mock()
    payload = {"title": "Milk", "body": "shopping", "collapse_id": "ha-todo-real", "push": True}

    reply = await ws("pushward/send_test", entry_id=entry.entry_id, kind="notification", action="send", payload=payload)

    assert reply["success"]
    api.create_notification.assert_awaited_once_with(
        title="Milk", body="shopping", collapse_id="ha-preview", trim_to_fit=True
    )


@pytest.mark.parametrize(
    ("kind", "action", "payload"),
    [
        ("notification", "end", None),
        ("activity", "send", None),
        ("activity", "send", {"name": "x", "content": {"template": "bogus"}}),
        ("activity", "send", {"name": "x", "content": {}}),
        ("widget", "send", {"template": "gauge"}),
        ("widget", "send", {"template": "bogus", "content": {}}),
        ("notification", "send", {"body": "no title"}),
        ("widget", "send", {"template": "value", "content": {"label": "x" * MAX_PAYLOAD_BYTES}}),
    ],
)
async def test_send_test_rejects_bad_requests_without_a_call(loaded, ws, kind: str, action: str, payload) -> None:
    entry, api = loaded
    api.reset_mock()
    fields = {"payload": payload} if payload is not None else {}

    reply = await ws("pushward/send_test", entry_id=entry.entry_id, kind=kind, action=action, **fields)

    assert reply["error"]["code"] == "invalid_format"
    assert api.mock_calls == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PushWardQuotaExceededError("widget_updates"), "quota_exceeded"),
        (PushWardAuthError("Invalid integration key", status_code=401), "auth_failed"),
        (PushWardApiError("DELETE /widgets/ha-preview-widget failed (422): bad slug", status_code=422), "api_error"),
        (E2EError("title and body must not be empty"), "api_error"),
    ],
)
async def test_send_test_maps_client_errors(loaded, ws, error: Exception, code: str) -> None:
    entry, api = loaded
    api.delete_widget.side_effect = error

    reply = await ws("pushward/send_test", entry_id=entry.entry_id, kind="widget", action="end")

    assert reply["error"]["code"] == code
    assert "hlk_" not in reply["error"]["message"]


# --- pushward/api ---


@pytest.mark.parametrize(
    ("method", "path", "quota_kind"),
    [
        ("POST", "/activities", QUOTA_KIND_LIVE_ACTIVITY_UPDATES),
        ("PATCH", "/activities/ha-washer", QUOTA_KIND_LIVE_ACTIVITY_UPDATES),
        ("POST", "/notifications", QUOTA_KIND_NOTIFICATIONS),
        ("POST", "/widgets", None),
        ("PATCH", "/widgets/ha-users", QUOTA_KIND_WIDGET_UPDATES),
        ("GET", "/app/status", None),
        ("GET", "/app/status?channel=play", None),
    ],
)
async def test_api_proxies_allowlisted_routes(loaded, ws, method: str, path: str, quota_kind: str | None) -> None:
    entry, api = loaded
    api._request_with_retry.reset_mock()

    reply = await ws(
        "pushward/api",
        entry_id=entry.entry_id,
        method=method,
        path=path,
        body={"slug": "ha-x"},
        content_type="application/merge-patch+json",
    )

    assert reply["result"] == {"status": 201, "body": {"slug": "ha-x"}}
    call = api._request_with_retry.call_args
    assert call.args == (method, path)
    assert call.kwargs["quota_kind"] == quota_kind
    assert call.kwargs["json"] == {"slug": "ha-x"}
    assert call.kwargs["content_type"] == "application/merge-patch+json"
    assert call.kwargs["raw"] is True
    assert (call.kwargs["idempotency_key"] is not None) == (path == "/notifications")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/activities/ha-washer"),
        ("GET", "/activities"),
        ("GET", "/auth/me"),
        ("POST", "/activities/ha-washer"),
        ("PATCH", "/activities/not a slug"),
        ("PATCH", "/activities/-dash-first"),
        ("PATCH", "/activities/{slug}"),
        ("PATCH", "/activities/ha-washer\n"),
        ("PATCH", "/activities/../auth/me"),
        ("PATCH", "/widgets/.."),
        ("POST", "https://evil.example/activities"),
        ("POST", "//evil.example/activities"),
        ("GET", "/app/status?channel=other"),
        ("post", "/activities"),
    ],
)
async def test_api_refuses_everything_else_without_a_call(loaded, ws, method: str, path: str) -> None:
    entry, api = loaded
    api._request_with_retry.reset_mock()

    reply = await ws("pushward/api", entry_id=entry.entry_id, method=method, path=path)

    assert reply["error"]["code"] == "not_allowed"
    api._request_with_retry.assert_not_awaited()


async def test_api_seals_a_notification_with_the_entry_key(loaded, ws) -> None:
    entry, api = loaded
    api.e2e_key = bytes(range(32))
    api._request_with_retry.reset_mock()

    reply = await ws(
        "pushward/api",
        entry_id=entry.entry_id,
        method="POST",
        path="/notifications",
        body={"title": "Hi", "body": "there", "level": "active"},
    )

    assert reply["success"]
    sent = api._request_with_retry.call_args.kwargs["json"]
    assert set(sent) == {"level", "encrypted"}
    assert open_envelope(api.e2e_key, sent["encrypted"]) == {"title": "Hi", "body": "there"}

    api._request_with_retry.reset_mock()
    reply = await ws("pushward/api", entry_id=entry.entry_id, method="POST", path="/notifications", body={"title": 1})
    assert reply["error"]["code"] == "api_error"
    api._request_with_retry.assert_not_awaited()


async def test_api_refuses_an_oversized_body(loaded, ws) -> None:
    entry, api = loaded
    api._request_with_retry.reset_mock()

    reply = await ws(
        "pushward/api", entry_id=entry.entry_id, method="POST", path="/widgets", body={"x": "y" * MAX_PAYLOAD_BYTES}
    )

    assert reply["error"]["code"] == "invalid_format"
    api._request_with_retry.assert_not_awaited()


# --- every command ---

_COMMANDS = [
    ("pushward/preview", {"subentry_type": SUBENTRY_TYPE_ENTITY, "data": {CONF_ENTITY_ID: WASHER}}),
    ("pushward/send_test", {"kind": "widget", "action": "end"}),
    ("pushward/api", {"method": "GET", "path": "/app/status"}),
]


@pytest.mark.parametrize(("msg_type", "fields"), _COMMANDS)
async def test_unknown_and_unloaded_entries_are_not_found(
    hass: HomeAssistant, loaded, ws, msg_type: str, fields: dict
) -> None:
    entry, api = loaded
    assert (await ws(msg_type, entry_id="missing", **fields))["error"]["code"] == "not_found"

    await hass.config_entries.async_unload(entry.entry_id)
    api.reset_mock()
    assert (await ws(msg_type, entry_id=entry.entry_id, **fields))["error"]["code"] == "not_found"
    assert api.mock_calls == []


@pytest.mark.parametrize(("msg_type", "fields"), _COMMANDS)
async def test_non_admin_users_are_refused(
    hass: HomeAssistant, loaded, hass_ws_client, hass_read_only_access_token: str, msg_type: str, fields: dict
) -> None:
    entry, api = loaded
    api.reset_mock()
    client = await hass_ws_client(hass, hass_read_only_access_token)

    await client.send_json_auto_id({"type": msg_type, "entry_id": entry.entry_id, **fields})
    reply = await client.receive_json()

    assert reply["error"]["code"] == "unauthorized"
    assert api.mock_calls == []
