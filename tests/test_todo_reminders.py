"""Tests for the to-do list reminders (tracked_todo subentries)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pushward.answer_stream import AnswerStream, notification_answer_signal
from custom_components.pushward.api import (
    PushWardApiError,
    PushWardAuthError,
    PushWardForbiddenError,
    PushWardNotFoundError,
)
from custom_components.pushward.const import (
    CONF_ENTITY_ID,
    CONF_SUBENTRY_ID,
    CONF_TODO_ALL_DAY_TIME,
    CONF_TODO_DONE_BUTTON,
    CONF_TODO_LEVEL,
    CONF_TODO_MAX_SCHEDULED,
    CONF_TODO_OFFSET_MINUTES,
    CONF_TODO_RETRACT,
    DOMAIN,
    TODO_METADATA_LIST,
    TODO_METADATA_UID,
)
from custom_components.pushward.e2e import E2EError
from custom_components.pushward.todo_reminders import (
    TodoReminderManager,
    async_cancel_stored_schedules,
    build_todo_store,
    e2e_unavailable_issue_id,
)

from .test_answer_stream import FakeFeed, _until
from .test_answer_stream import _answer as _feed_answer

NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)  # 10:00 in Europe/Warsaw
ENTITY = "todo.reminders"
SUB = "sub-1"


def _config(**overrides) -> dict:
    cfg = {
        CONF_SUBENTRY_ID: SUB,
        CONF_ENTITY_ID: ENTITY,
        CONF_TODO_OFFSET_MINUTES: 0,
        CONF_TODO_ALL_DAY_TIME: "09:00:00",
        CONF_TODO_LEVEL: "active",
        CONF_TODO_DONE_BUTTON: True,
        CONF_TODO_MAX_SCHEDULED: 10,
    }
    cfg.update(overrides)
    return cfg


def _api() -> AsyncMock:
    api = AsyncMock()
    ids = iter(range(100, 1000))
    api.create_notification = AsyncMock(side_effect=lambda *a, **kw: {"id": next(ids), "status": "scheduled"})
    api.cancel_scheduled_notification = AsyncMock()
    api.list_scheduled_notifications = AsyncMock(return_value=[])
    api.get_scheduled_notification = AsyncMock(return_value=None)
    api.poll_notification_answer = AsyncMock(return_value=({"status": "pending"}, True))
    api.retract_notifications = AsyncMock(return_value=1)
    return api


class _TodoList:
    """A fake to-do entity: its state plus todo.get_items / todo.update_item."""

    def __init__(self, hass: HomeAssistant, items: list[dict]) -> None:
        self.hass = hass
        self.items = items
        self.updates: list[dict] = []

        async def get_items(call: ServiceCall) -> dict:
            entity_ids = call.data["entity_id"]
            entity_ids = [entity_ids] if isinstance(entity_ids, str) else entity_ids
            status = call.data.get("status")
            items = [item for item in self.items if not status or item.get("status") in status]
            return {eid: {"items": items} for eid in entity_ids}

        async def update_item(call: ServiceCall) -> None:
            self.updates.append(dict(call.data))

        hass.services.async_register("todo", "get_items", get_items, supports_response=SupportsResponse.ONLY)
        hass.services.async_register("todo", "update_item", update_item)
        self.write()

    def write(self) -> None:
        self.hass.states.async_set(ENTITY, str(len(self.items)), {"friendly_name": "Reminders"})


@pytest.fixture
async def setup(hass: HomeAssistant, freezer):
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Warsaw")
    entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-1")
    entry.add_to_hass(hass)
    managers: list[TodoReminderManager] = []

    async def _start(items: list[dict], api: AsyncMock | None = None, answer_stream=None, **cfg) -> tuple:
        todo = _TodoList(hass, items)
        api = api or _api()
        manager = TodoReminderManager(hass, api, [_config(**cfg)], entry, "hlk_test", answer_stream=answer_stream)
        managers.append(manager)
        await manager.async_start()
        await hass.async_block_till_done()
        return manager, api, todo

    yield _start
    for manager in managers:
        await manager.async_stop()


def _records(manager: TodoReminderManager) -> dict:
    return manager._lists[SUB]["items"]


async def test_datetime_item_gets_a_reminder_at_due_minus_offset(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, _ = await setup(items, **{CONF_TODO_OFFSET_MINUTES: 30})

    api.create_notification.assert_awaited_once()
    args, kwargs = api.create_notification.call_args
    assert args == ("Dentist", "Reminders")  # no description: the list name is the body
    assert kwargs["send_at"] == datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
    assert kwargs["level"] == "active"
    assert kwargs["metadata"] == {TODO_METADATA_LIST: SUB, TODO_METADATA_UID: "a"}
    assert kwargs["actions"] == [{"id": "done", "title": "Done"}]
    assert kwargs["source_display_name"] == "Reminders"
    assert _records(manager)["a"]["status"] == "scheduled"


async def test_date_only_item_uses_the_all_day_time(setup) -> None:
    items = [{"uid": "a", "summary": "Bins", "description": "Blue bin", "status": "needs_action", "due": "2026-10-03"}]
    _, api, _ = await setup(items, **{CONF_TODO_ALL_DAY_TIME: "07:30:00", CONF_TODO_DONE_BUTTON: False})

    args, kwargs = api.create_notification.call_args
    assert args == ("Bins", "Blue bin")
    assert kwargs["send_at"] == datetime(2026, 10, 3, 5, 30, tzinfo=UTC)  # 07:30 CEST
    assert kwargs["actions"] is None


async def test_undated_overdue_and_far_items_get_nothing(setup) -> None:
    items = [
        {"uid": "undated", "summary": "Someday", "status": "needs_action"},
        {"uid": "overdue", "summary": "Late", "status": "needs_action", "due": "2026-09-30T12:00:00+00:00"},
        {"uid": "far", "summary": "Far", "status": "needs_action", "due": "2027-12-01T12:00:00+00:00"},
    ]
    _, api, _ = await setup(items)

    api.create_notification.assert_not_awaited()


async def test_lead_time_reaching_into_the_past_sends_soon(setup) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T08:10:00+00:00"}]
    _, api, _ = await setup(items, **{CONF_TODO_OFFSET_MINUTES: 30})

    assert api.create_notification.call_args[1]["send_at"] == NOW + timedelta(seconds=60)


async def test_only_the_soonest_items_up_to_the_cap(setup) -> None:
    items = [
        {"uid": f"i{d}", "summary": f"Day {d}", "status": "needs_action", "due": f"2026-10-{d:02d}T12:00:00+00:00"}
        for d in (9, 3, 5)
    ]
    manager, api, _ = await setup(items, **{CONF_TODO_MAX_SCHEDULED: 2})

    assert [call.args[0] for call in api.create_notification.call_args_list] == ["Day 3", "Day 5"]
    assert set(_records(manager)) == {"i3", "i5"}


async def test_edit_replaces_the_schedule_with_a_purge(hass: HomeAssistant, setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, todo = await setup(items)
    first_id = _records(manager)["a"]["schedule_id"]

    todo.items[0] = {**todo.items[0], "due": "2026-10-02T16:00:00+02:00"}
    await manager._async_reconcile(SUB)

    api.cancel_scheduled_notification.assert_awaited_once_with(first_id, purge=True)
    assert api.create_notification.await_count == 2
    assert _records(manager)["a"]["schedule_id"] != first_id


async def test_completed_item_cancels_without_purge(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, todo = await setup(items)
    schedule_id = _records(manager)["a"]["schedule_id"]

    todo.items.clear()
    await manager._async_reconcile(SUB)

    api.cancel_scheduled_notification.assert_awaited_once_with(schedule_id, purge=False)
    assert "a" not in _records(manager)


async def test_schedule_canceled_elsewhere_is_left_alone_until_the_due_time_changes(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, todo = await setup(items)
    schedule_id = _records(manager)["a"]["schedule_id"]

    # The owner stopped it in the app: gone from the pending list, reads canceled.
    api.get_scheduled_notification = AsyncMock(return_value={"id": schedule_id, "status": "canceled"})
    await manager._async_reconcile(SUB, full=True)
    await manager._async_reconcile(SUB, full=True)

    assert _records(manager)["a"]["status"] == "stopped"
    assert api.create_notification.await_count == 1
    api.cancel_scheduled_notification.assert_not_awaited()

    todo.items[0] = {**todo.items[0], "summary": "Dentist, bring the card"}  # wording only
    await manager._async_reconcile(SUB)
    assert api.create_notification.await_count == 1

    todo.items[0] = {**todo.items[0], "due": "2026-10-03T15:00:00+02:00"}  # a new time
    await manager._async_reconcile(SUB)

    assert api.create_notification.await_count == 2
    assert _records(manager)["a"]["status"] == "scheduled"


async def test_sent_reminder_is_not_sent_again(hass: HomeAssistant, setup, freezer) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T09:00:00+00:00"}]
    manager, api, _ = await setup(items, **{CONF_TODO_OFFSET_MINUTES: 30})
    assert api.create_notification.await_count == 1

    freezer.move_to(NOW + timedelta(minutes=45))  # past the reminder, before the due time
    await manager._async_reconcile(SUB)

    assert _records(manager)["a"]["status"] == "sent"
    assert api.create_notification.await_count == 1


async def test_first_full_pass_purges_this_lists_unknown_schedules(setup) -> None:
    api = _api()
    api.list_scheduled_notifications = AsyncMock(
        return_value=[
            {"id": 7, "status": "scheduled", "metadata": {TODO_METADATA_LIST: SUB, TODO_METADATA_UID: "x"}},
            {"id": 8, "status": "scheduled", "metadata": {"from": "an automation"}},
            {"id": 9, "status": "scheduled"},
        ]
    )
    await setup([], api=api)

    api.cancel_scheduled_notification.assert_awaited_once_with(7, purge=True)


async def test_unavailable_list_changes_nothing(hass: HomeAssistant, setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, _ = await setup(items)

    hass.states.async_set(ENTITY, "unavailable")
    await manager._async_reconcile(SUB, full=True)

    api.cancel_scheduled_notification.assert_not_awaited()
    assert _records(manager)["a"]["status"] == "scheduled"


async def test_an_edit_that_keeps_the_count_triggers_a_reconcile(hass: HomeAssistant, setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    _, api, todo = await setup(items)

    todo.items[0] = {**todo.items[0], "summary": "Dentist at 3"}
    todo.write()  # same state: HA fires state_reported, not state_changed
    await hass.async_block_till_done()
    async_fire_time_changed(hass, NOW + timedelta(seconds=5))  # past the debounce
    await hass.async_block_till_done()

    assert api.create_notification.await_count == 2


async def test_removing_the_list_cancels_its_pending_reminders(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, _ = await setup(items)
    schedule_id = _records(manager)["a"]["schedule_id"]

    await manager.async_reload([])

    api.cancel_scheduled_notification.assert_awaited_once_with(schedule_id)
    assert SUB not in manager._lists


async def test_done_tap_completes_the_item(setup) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T09:00:00+00:00"}]
    manager, api, todo = await setup(items)
    rec = _records(manager)["a"]
    rec.update(status="sent", watch_until=(NOW + timedelta(hours=24)).isoformat())
    api.get_scheduled_notification = AsyncMock(
        return_value={"id": rec["schedule_id"], "status": "sent", "notification_id": 55}
    )
    api.poll_notification_answer = AsyncMock(
        return_value=({"notification_id": 55, "status": "answered", "action_id": "done"}, True)
    )

    assert await manager._async_watch_once(SUB, "a", rec, hold=20) is True

    api.poll_notification_answer.assert_awaited_once_with(55, hold=20)
    assert todo.updates == [{"item": "a", "status": "completed", "entity_id": ENTITY}]
    assert rec["watch_until"] is None


async def test_watch_waits_while_the_reminder_is_being_sent(setup) -> None:
    manager, api, _ = await setup([])
    rec = {"schedule_id": 5, "status": "sent", "notification_id": None, "send_at": NOW.isoformat()}
    api.get_scheduled_notification = AsyncMock(return_value={"id": 5, "status": "sending"})

    assert await manager._async_watch_once(SUB, "a", rec, hold=20) is False
    api.poll_notification_answer.assert_not_awaited()


async def test_watch_stops_when_there_is_no_answer_to_read(setup) -> None:
    manager, api, _ = await setup([])
    rec = {"schedule_id": 5, "status": "sent", "notification_id": 55, "send_at": NOW.isoformat()}
    api.poll_notification_answer = AsyncMock(side_effect=PushWardNotFoundError("404", status_code=404))

    with pytest.raises(PushWardNotFoundError):
        await manager._async_watch_once(SUB, "a", rec, hold=20)


async def test_server_error_keeps_the_records(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, todo = await setup(items)
    api.cancel_scheduled_notification = AsyncMock(side_effect=PushWardApiError("down", status_code=503))

    todo.items.clear()
    await manager._async_reconcile(SUB)

    assert "a" in _records(manager)  # retried on the next pass


async def test_entry_removal_cancels_stored_schedules(hass: HomeAssistant, hass_storage) -> None:
    store = build_todo_store(hass, "entry-9")
    await store.async_save(
        {
            "lists": {
                SUB: {
                    "entity_id": ENTITY,
                    "items": {
                        "a": {"schedule_id": 11, "status": "scheduled"},
                        "b": {"schedule_id": 12, "status": "sent"},
                    },
                }
            }
        }
    )
    api = _api()

    await async_cancel_stored_schedules(hass, api, "entry-9")

    api.cancel_scheduled_notification.assert_awaited_once_with(11)
    assert await build_todo_store(hass, "entry-9").async_load() is None


async def test_settings_change_does_not_resend_a_sent_reminder(setup, freezer) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T09:00:00+00:00"}]
    manager, api, _ = await setup(items, **{CONF_TODO_OFFSET_MINUTES: 30})
    freezer.move_to(NOW + timedelta(minutes=45))
    await manager._async_reconcile(SUB)
    assert _records(manager)["a"]["status"] == "sent"

    await manager.async_reload([_config(**{CONF_TODO_OFFSET_MINUTES: 30, CONF_TODO_DONE_BUTTON: False})])
    await manager._async_reconcile(SUB, full=True)

    assert api.create_notification.await_count == 1


async def test_settings_change_replaces_a_pending_reminder(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, _ = await setup(items)
    first_id = _records(manager)["a"]["schedule_id"]
    api.list_scheduled_notifications = AsyncMock(return_value=[{"id": first_id, "status": "scheduled"}])

    await manager.async_reload([_config(**{CONF_TODO_OFFSET_MINUTES: 60})])
    await manager._async_reconcile(SUB, full=True)

    api.cancel_scheduled_notification.assert_any_await(first_id, purge=True)
    assert api.create_notification.call_args[1]["send_at"] == datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


async def test_start_cancels_a_list_removed_while_unloaded(hass: HomeAssistant, setup, hass_storage) -> None:
    await build_todo_store(hass, "entry-1").async_save(
        {"lists": {"gone": {"entity_id": "todo.old", "items": {"x": {"schedule_id": 42, "status": "scheduled"}}}}}
    )
    _, api, _ = await setup([])

    api.cancel_scheduled_notification.assert_any_await(42)


async def test_pending_schedule_gone_from_the_server_is_scheduled_again(setup) -> None:
    """A 404 (key revoked, backup restored) is not an owner stop: the item gets a new reminder."""
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, _ = await setup(items)
    first_id = _records(manager)["a"]["schedule_id"]

    api.get_scheduled_notification = AsyncMock(return_value=None)
    await manager._async_reconcile(SUB, full=True)

    assert api.create_notification.await_count == 2
    assert _records(manager)["a"]["schedule_id"] != first_id
    assert _records(manager)["a"]["status"] == "scheduled"


async def test_a_new_integration_key_reschedules_pending_reminders(hass: HomeAssistant, setup, hass_storage) -> None:
    await build_todo_store(hass, "entry-1").async_save(
        {
            "key": "old-key-hash",
            "lists": {
                SUB: {
                    "entity_id": ENTITY,
                    "items": {
                        "a": {
                            "schedule_id": 7,
                            "status": "scheduled",
                            "version": "x",
                            "fingerprint": "y",
                            "send_at": "2026-10-02T13:00:00+00:00",
                            "due_at": "2026-10-02T13:00:00+00:00",
                        }
                    },
                }
            },
        }
    )
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, _ = await setup(items)

    api.cancel_scheduled_notification.assert_not_awaited()  # the new key cannot reach the old row
    api.create_notification.assert_awaited_once()
    assert _records(manager)["a"]["schedule_id"] != 7


async def test_items_held_back_by_the_cap_still_go_out(setup, freezer) -> None:
    due = (NOW + timedelta(minutes=30)).isoformat()
    items = [{"uid": f"i{n}", "summary": f"Item {n}", "status": "needs_action", "due": due} for n in range(3)]
    manager, api, _ = await setup(items, **{CONF_TODO_MAX_SCHEDULED: 2})
    assert api.create_notification.await_count == 2

    freezer.move_to(NOW + timedelta(minutes=30, seconds=15))  # the first two went out at the due time
    await manager._async_reconcile(SUB)

    assert api.create_notification.await_count == 3
    assert api.create_notification.call_args[1]["send_at"] == NOW + timedelta(minutes=31, seconds=15)


async def test_long_overdue_item_gets_nothing(setup) -> None:
    items = [{"uid": "a", "summary": "Late", "status": "needs_action", "due": "2026-10-01T06:30:00+00:00"}]
    _, api, _ = await setup(items)  # 90 minutes past due

    api.create_notification.assert_not_awaited()


async def test_sent_reminder_without_done_button_is_not_repeated(setup, freezer) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T08:05:00+00:00"}]
    manager, api, _ = await setup(items, **{CONF_TODO_DONE_BUTTON: False})

    for minutes in (5.25, 7, 30, 70):
        freezer.move_to(NOW + timedelta(minutes=minutes))
        await manager._async_reconcile(SUB)

    assert api.create_notification.await_count == 1


async def test_item_that_leaves_and_comes_back_is_not_reminded_again(setup, freezer) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T09:00:00+00:00"}]
    manager, api, todo = await setup(items, **{CONF_TODO_OFFSET_MINUTES: 30})
    freezer.move_to(NOW + timedelta(minutes=45))
    await manager._async_reconcile(SUB)  # sent

    saved = todo.items[:]
    todo.items.clear()  # completed...
    await manager._async_reconcile(SUB)
    todo.items.extend(saved)  # ...and undone
    await manager._async_reconcile(SUB)

    assert api.create_notification.await_count == 1


async def test_an_edit_during_a_pass_is_picked_up(hass: HomeAssistant, setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, todo = await setup(items)

    async def create_and_edit(*args, **kwargs):
        # The owner moves the item while this pass is still creating.
        if args[0] == "Dentist, moved":
            return {"id": 501}
        todo.items[0] = {**todo.items[0], "summary": "Dentist, moved"}
        manager._dirty.add(SUB)
        return {"id": 500}

    todo.items.append({"uid": "b", "summary": "Bins", "status": "needs_action", "due": "2026-10-03"})
    api.create_notification = AsyncMock(side_effect=create_and_edit)
    await manager._async_reconcile(SUB)

    assert _records(manager)["a"]["schedule_id"] == 501


async def test_account_cap_holds_creates_until_the_next_full_pass(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    api = _api()
    api.create_notification = AsyncMock(side_effect=PushWardApiError("limit", status_code=409))
    manager, api, todo = await setup(items, api=api)
    assert api.create_notification.await_count == 1

    todo.items[0] = {**todo.items[0], "summary": "Dentist at 3"}
    await manager._async_reconcile(SUB)  # edit-driven: still held
    assert api.create_notification.await_count == 1

    await manager._async_reconcile(SUB, full=True)
    assert api.create_notification.await_count == 2


async def test_refused_item_is_not_retried_until_edited(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    api = _api()
    api.create_notification = AsyncMock(side_effect=PushWardApiError("bad", status_code=400))
    manager, api, todo = await setup(items, api=api)

    await manager._async_reconcile(SUB, full=True)
    assert api.create_notification.await_count == 1

    todo.items[0] = {**todo.items[0], "summary": "Dentist at 3"}
    await manager._async_reconcile(SUB)
    assert api.create_notification.await_count == 2


async def test_refusal_after_send_at_passed_is_retried(setup, freezer) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T08:10:00+00:00"}]
    api = _api()

    async def refuse_late(*_args, **_kwargs):
        freezer.tick(timedelta(seconds=90))  # the retries outlasted the near-due delay
        raise PushWardApiError("send_at must be in the future", status_code=400)

    api.create_notification = AsyncMock(side_effect=refuse_late)
    manager, api, _ = await setup(items, api=api, **{CONF_TODO_OFFSET_MINUTES: 30})
    assert api.create_notification.await_count == 1

    api.create_notification = AsyncMock(return_value={"id": 7})
    await manager._async_reconcile(SUB, full=True)
    assert api.create_notification.await_count == 1
    assert api.create_notification.call_args[1]["send_at"] > NOW + timedelta(seconds=90)


async def test_late_refusal_does_not_hold_back_the_next_reminder(setup, freezer) -> None:
    items = [
        {"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T08:10:00+00:00"},
        {"uid": "b", "summary": "Dentist", "status": "needs_action", "due": "2026-10-01T08:40:00+00:00"},
    ]
    api = _api()
    calls = []

    async def first_late(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 1:
            freezer.tick(timedelta(seconds=90))
            raise PushWardApiError("send_at must be in the future", status_code=400)
        return {"id": 8}

    api.create_notification = AsyncMock(side_effect=first_late)
    await setup(items, api=api, **{CONF_TODO_OFFSET_MINUTES: 30})
    assert calls == ["Call", "Dentist"]


async def test_item_that_cannot_be_encrypted_waits_for_an_edit(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    api = _api()
    api.create_notification = AsyncMock(side_effect=E2EError("too long to encrypt"))
    manager, api, todo = await setup(items, api=api)

    await manager._async_reconcile(SUB, full=True)
    assert api.create_notification.await_count == 1

    todo.items[0] = {**todo.items[0], "summary": "Dentist at 3"}
    await manager._async_reconcile(SUB)
    assert api.create_notification.await_count == 2


async def test_reminders_ask_to_be_trimmed_to_fit_an_envelope(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    _, api, _ = await setup(items)

    assert api.create_notification.call_args.kwargs["trim_to_fit"] is True


async def test_an_item_that_cannot_be_encrypted_does_not_hold_up_the_rest(setup) -> None:
    items = [
        {"uid": "a", "summary": "Bad\ud800", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"},
        {"uid": "b", "summary": "Fine", "status": "needs_action", "due": "2026-10-02T16:00:00+02:00"},
    ]
    api = _api()
    ids = iter(range(100, 1000))

    async def create(title, body, **kwargs):
        if "\ud800" in title:
            raise E2EError("the text is not valid Unicode")
        return {"id": next(ids), "status": "scheduled"}

    api.create_notification = AsyncMock(side_effect=create)
    manager, api, _ = await setup(items, api=api)

    assert set(_records(manager)) == {"b"}


async def test_a_key_change_retries_refused_items_without_an_edit(setup, hass: HomeAssistant) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    api = _api()
    api.create_notification = AsyncMock(side_effect=E2EError("the text is not valid Unicode"))
    manager, api, _ = await setup(items, api=api)
    assert api.create_notification.await_count == 1

    api.create_notification = AsyncMock(return_value={"id": 7, "status": "scheduled"})
    manager.async_retry_refused()
    await hass.async_block_till_done()

    api.create_notification.assert_awaited_once()
    assert _records(manager)["a"]["schedule_id"] == 7


def _e2e_issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, e2e_unavailable_issue_id("entry-1"))


async def test_org_key_refusing_encryption_raises_one_repair_issue(setup, hass: HomeAssistant) -> None:
    items = [
        {"uid": "a", "summary": "One", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"},
        {"uid": "b", "summary": "Two", "status": "needs_action", "due": "2026-10-02T16:00:00+02:00"},
    ]
    api = _api()
    refusal = PushWardApiError("refused", status_code=422, code="notification.encryption_unavailable")
    api.create_notification = AsyncMock(side_effect=refusal)
    manager, api, _ = await setup(items, api=api)

    assert api.create_notification.await_count == 2
    issue = _e2e_issue(hass)
    assert issue is not None
    assert issue.translation_key == "todo_e2e_unavailable"
    assert not issue.is_persistent
    assert [i for i in ir.async_get(hass).issues.values() if i.domain == DOMAIN] == [issue]

    # A key change clears it, even while the retried reminders fail for other reasons.
    api.create_notification = AsyncMock(side_effect=PushWardApiError("bad", status_code=400))
    manager.async_retry_refused()
    await hass.async_block_till_done()
    assert api.create_notification.await_count == 2
    assert _e2e_issue(hass) is None

    # A new key that is still refused raises it again.
    api.create_notification = AsyncMock(side_effect=refusal)
    manager.async_retry_refused()
    await hass.async_block_till_done()
    assert _e2e_issue(hass) is not None


async def test_e2e_issue_clears_on_an_accepted_reminder_and_on_stop(setup, hass: HomeAssistant) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    api = _api()
    api.create_notification = AsyncMock(
        side_effect=PushWardApiError("refused", status_code=422, code="notification.encryption_unavailable")
    )
    manager, api, todo = await setup(items, api=api)
    assert _e2e_issue(hass) is not None

    api.create_notification = AsyncMock(return_value={"id": 7, "status": "scheduled"})
    todo.items[0] = {**todo.items[0], "summary": "Dentist at 3"}
    await manager._async_reconcile(SUB)
    assert _e2e_issue(hass) is None

    manager._raise_e2e_issue()
    await manager.async_stop()
    assert _e2e_issue(hass) is None


async def test_other_refusals_raise_no_repair_issue(setup, hass: HomeAssistant) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    api = _api()
    api.create_notification = AsyncMock(
        side_effect=PushWardApiError("bad", status_code=422, code="notification.invalid")
    )
    await setup(items, api=api)

    assert _e2e_issue(hass) is None


async def test_reminders_carry_a_collapse_id_per_item(setup) -> None:
    items = [
        {"uid": "a", "summary": "One", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"},
        {"uid": "b", "summary": "Two", "status": "needs_action", "due": "2026-10-02T16:00:00+02:00"},
    ]
    _, api, _ = await setup(items)

    ids = [call.kwargs["collapse_id"] for call in api.create_notification.call_args_list]
    assert len(set(ids)) == 2
    assert all(i.startswith("ha-todo-") and len(i) <= 64 for i in ids)


async def test_failed_cancel_on_removal_is_retried(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, _ = await setup(items)
    schedule_id = _records(manager)["a"]["schedule_id"]
    api.cancel_scheduled_notification = AsyncMock(side_effect=PushWardApiError("down", status_code=503))

    await manager.async_reload([])
    assert manager._orphans == [schedule_id]

    api.cancel_scheduled_notification = AsyncMock()
    await manager._async_reconcile_all()
    api.cancel_scheduled_notification.assert_awaited_once_with(schedule_id)
    assert manager._orphans == []


async def test_lead_time_is_real_minutes_across_dst(setup, freezer) -> None:
    """Europe/Warsaw leaves summer time at 03:00 on 2026-10-25."""
    freezer.move_to(datetime(2026, 10, 24, 12, 0, tzinfo=UTC))
    items = [{"uid": "a", "summary": "Early", "status": "needs_action", "due": "2026-10-25"}]
    _, api, _ = await setup(items, **{CONF_TODO_ALL_DAY_TIME: "04:00:00", CONF_TODO_OFFSET_MINUTES: 120})

    # 04:00 CET is 03:00 UTC; two real hours before is 01:00 UTC.
    assert api.create_notification.call_args[1]["send_at"] == datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


async def test_unavailable_list_with_a_reminder_past_its_time_does_not_spin(
    hass: HomeAssistant, setup, freezer
) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T08:30:00+00:00"}]
    manager, _, _ = await setup(items)
    hass.states.async_set(ENTITY, "unavailable")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, NOW + timedelta(seconds=5))  # the debounced pass for that write
    await hass.async_block_till_done()
    freezer.move_to(NOW + timedelta(minutes=35))

    calls = 0
    locked = manager._async_reconcile_locked

    async def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        await locked(*args, **kwargs)

    manager._async_reconcile_locked = counting
    await manager._async_reconcile(SUB, full=True)
    for _ in range(20):
        await hass.async_block_till_done()

    assert calls == 1
    assert _records(manager)["a"]["status"] == "sent"  # marked even though the list could not be read


async def test_rate_limited_create_is_retried_on_the_next_pass(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    api = _api()
    api.create_notification = AsyncMock(side_effect=PushWardApiError("slow down", status_code=429))
    manager, api, _ = await setup(items, api=api)

    api.create_notification = AsyncMock(return_value={"id": 300})
    await manager._async_reconcile(SUB)

    assert _records(manager)["a"]["schedule_id"] == 300


async def test_done_tap_is_retried_when_completing_fails(hass: HomeAssistant, setup) -> None:
    manager, api, todo = await setup([])
    rec = {"schedule_id": 5, "status": "sent", "notification_id": 55, "send_at": NOW.isoformat(), "watch_until": "x"}
    api.poll_notification_answer = AsyncMock(return_value=({"status": "answered", "action_id": "done"}, True))
    hass.states.async_set(ENTITY, "unavailable")

    assert await manager._async_watch_once(SUB, "a", rec, hold=20) is False
    assert rec["watch_until"] == "x"  # still watched: the next round tries again

    todo.write()  # back
    await manager._async_watch_once(SUB, "a", rec, hold=20)
    assert todo.updates == [{"item": "a", "status": "completed", "entity_id": ENTITY}]
    assert rec["watch_until"] is None


async def test_removing_a_list_mid_pass_stops_its_creates(setup) -> None:
    items = [
        {"uid": "a", "summary": "One", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"},
        {"uid": "b", "summary": "Two", "status": "needs_action", "due": "2026-10-02T16:00:00+02:00"},
    ]
    manager, api, _ = await setup([])

    async def create_then_remove(*args, **kwargs):
        manager._configs.pop(SUB, None)  # the list is removed while this pass creates
        return {"id": 400}

    api.create_notification = AsyncMock(side_effect=create_then_remove)
    manager._async_open_items = AsyncMock(return_value=items)
    await manager._async_reconcile(SUB)

    assert api.create_notification.await_count == 1


async def test_a_sent_reminder_lifts_the_account_cap_hold(setup, freezer) -> None:
    items = [
        {"uid": "a", "summary": "Soon", "status": "needs_action", "due": "2026-10-01T08:10:00+00:00"},
        {"uid": "b", "summary": "Later", "status": "needs_action", "due": "2026-10-02T12:00:00+00:00"},
    ]
    api = _api()
    ids = iter(range(700, 800))

    async def room_for_one(*args, **kwargs):
        if args[0] == "Later" and api.create_notification.await_count == 2:
            raise PushWardApiError("limit", status_code=409)
        return {"id": next(ids)}

    api.create_notification = AsyncMock(side_effect=room_for_one)
    manager, api, _ = await setup(items, api=api)
    assert SUB in manager._blocked

    freezer.move_to(NOW + timedelta(minutes=10, seconds=15))  # "Soon" went out, freeing its slot
    await manager._async_reconcile(SUB)

    assert _records(manager)["b"]["status"] == "scheduled"


async def test_renaming_after_the_reminder_went_out_does_not_send_it_again(setup, freezer) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T08:10:00+00:00"}]
    manager, api, todo = await setup(items)
    freezer.move_to(NOW + timedelta(minutes=10, seconds=15))
    await manager._async_reconcile(SUB)  # sent

    todo.items[0] = {**todo.items[0], "summary": "Call mum", "description": "About Sunday"}
    await manager._async_reconcile(SUB)
    assert api.create_notification.await_count == 1
    assert _records(manager)["a"]["status"] == "sent"

    todo.items[0] = {**todo.items[0], "due": "2026-10-01T09:00:00+00:00"}  # moved: remind again
    await manager._async_reconcile(SUB)
    assert api.create_notification.await_count == 2


# --- Done taps from the answer stream -------------------------------------------


class _FakeStream:
    """Stands in for the entry's AnswerStream: delivers the answers of the schedules in ``tracked``."""

    def __init__(self) -> None:
        self.tracked: set[int] = set()
        self.released: list[tuple] = []

    def delivers_answer(self, schedule_id, notification_id) -> bool:
        return schedule_id in self.tracked

    def release(self, schedule_id=None, notification_id=None) -> None:
        self.released.append((schedule_id, notification_id))
        self.tracked.discard(schedule_id)


def _done(notification_id: int = 55, action_id: str = "done") -> dict:
    return {"notification_id": notification_id, "status": "answered", "action_id": action_id}


_CALL = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T09:00:00+00:00"}]


async def test_reminders_the_stream_tracks_get_no_read_of_their_own(hass: HomeAssistant, setup) -> None:
    stream = _FakeStream()
    manager, api, todo = await setup(_CALL, answer_stream=stream)
    rec = _records(manager)["a"]
    stream.tracked.add(rec["schedule_id"])
    rec.update(status="sent", watch_until=(NOW + timedelta(hours=24)).isoformat())

    manager._watch_wakeup.set()
    for _ in range(5):
        await hass.async_block_till_done()

    api.get_scheduled_notification.assert_not_awaited()
    api.poll_notification_answer.assert_not_awaited()

    async_dispatcher_send(hass, notification_answer_signal("entry-1"), _done(), rec["schedule_id"])
    await hass.async_block_till_done()

    assert todo.updates == [{"item": "a", "status": "completed", "entity_id": ENTITY}]
    assert rec["watch_until"] is None
    assert rec["notification_id"] == 55
    api.poll_notification_answer.assert_not_awaited()
    assert (rec["schedule_id"], 55) in stream.released


async def test_reminders_ask_the_stream_to_wait_only_as_long_as_the_watch(setup) -> None:
    _, api, _ = await setup(_CALL)

    assert api.create_notification.call_args.kwargs["answer_window"] == timedelta(hours=24)


async def test_a_reminder_whose_item_was_completed_is_released(setup, freezer) -> None:
    stream = _FakeStream()
    manager, _, todo = await setup(list(_CALL), answer_stream=stream, **{CONF_TODO_OFFSET_MINUTES: 30})
    freezer.move_to(NOW + timedelta(minutes=45))
    await manager._async_reconcile(SUB)  # sent, and watched
    rec = _records(manager)["a"]
    stream.tracked.add(rec["schedule_id"])
    assert stream.released == []

    todo.items.clear()  # ticked off in Home Assistant
    await manager._async_reconcile(SUB)

    assert rec["watch_until"] is None
    assert stream.released == [(rec["schedule_id"], None)]


async def test_a_reminder_past_its_watch_is_released(setup, freezer) -> None:
    stream = _FakeStream()
    manager, _, _ = await setup(_CALL, answer_stream=stream, **{CONF_TODO_OFFSET_MINUTES: 30})
    freezer.move_to(NOW + timedelta(minutes=45))
    await manager._async_reconcile(SUB)
    rec = _records(manager)["a"]

    freezer.move_to(NOW + timedelta(hours=25))
    await manager._async_reconcile(SUB, full=True)

    assert (rec["schedule_id"], None) in stream.released
    assert "a" not in _records(manager)  # past the late grace too: forgotten


async def test_a_reminder_canceled_before_its_time_is_released(setup) -> None:
    stream = _FakeStream()
    manager, api, todo = await setup(list(_CALL), answer_stream=stream)
    schedule_id = _records(manager)["a"]["schedule_id"]

    todo.items.clear()
    await manager._async_reconcile(SUB)

    api.cancel_scheduled_notification.assert_awaited_once_with(schedule_id, purge=False)
    assert stream.released == [(schedule_id, None)]


async def test_a_stream_answer_before_the_reminder_is_marked_sent(hass: HomeAssistant, setup, freezer) -> None:
    manager, _, todo = await setup(_CALL, answer_stream=_FakeStream())
    rec = _records(manager)["a"]
    assert rec["status"] == "scheduled"

    freezer.move_to(NOW + timedelta(hours=1, seconds=5))  # sent, and tapped before the pass after it
    async_dispatcher_send(hass, notification_answer_signal("entry-1"), _done(), rec["schedule_id"])
    await hass.async_block_till_done()

    assert rec["status"] == "sent"
    assert todo.updates == [{"item": "a", "status": "completed", "entity_id": ENTITY}]
    assert rec["watch_until"] is None


async def test_a_stream_answer_matches_by_notification_id(hass: HomeAssistant, setup) -> None:
    manager, _, todo = await setup(_CALL, answer_stream=_FakeStream())
    rec = _records(manager)["a"]
    rec.update(status="sent", notification_id=55, watch_until=(NOW + timedelta(hours=24)).isoformat())

    async_dispatcher_send(hass, notification_answer_signal("entry-1"), _done(55), None)
    async_dispatcher_send(hass, notification_answer_signal("entry-1"), _done(56), None)  # someone else's
    await hass.async_block_till_done()

    assert todo.updates == [{"item": "a", "status": "completed", "entity_id": ENTITY}]


async def test_a_stream_answer_other_than_done_ends_the_watch(hass: HomeAssistant, setup) -> None:
    manager, _, todo = await setup(_CALL, answer_stream=_FakeStream())
    rec = _records(manager)["a"]
    rec.update(status="sent", watch_until=(NOW + timedelta(hours=24)).isoformat())

    async_dispatcher_send(hass, notification_answer_signal("entry-1"), _done(action_id="other"), rec["schedule_id"])
    await hass.async_block_till_done()

    assert todo.updates == []
    assert rec["watch_until"] is None


async def test_a_stream_answer_after_the_watch_ended_is_ignored(hass: HomeAssistant, setup) -> None:
    manager, _, todo = await setup(_CALL, answer_stream=_FakeStream())
    rec = _records(manager)["a"]
    rec.update(status="sent", watch_until=(NOW - timedelta(minutes=1)).isoformat())

    async_dispatcher_send(hass, notification_answer_signal("entry-1"), _done(), rec["schedule_id"])
    await hass.async_block_till_done()

    assert todo.updates == []


async def test_a_stream_answer_while_the_store_loads_is_acted_on(hass: HomeAssistant, setup, hass_storage) -> None:
    """The stream starts first: an answer it reads during the manager's start must not be lost."""
    sent = {
        "schedule_id": 5,
        "status": "sent",
        "notification_id": 55,
        "send_at": NOW.isoformat(),
        "due_at": (NOW + timedelta(hours=1)).isoformat(),
        "watch_until": (NOW + timedelta(hours=24)).isoformat(),
    }
    await build_todo_store(hass, "entry-1").async_save({"lists": {SUB: {"entity_id": ENTITY, "items": {"a": sent}}}})
    load = Store.async_load

    async def load_after_an_answer(store):
        async_dispatcher_send(hass, notification_answer_signal("entry-1"), _done(55), 5)
        return await load(store)

    with patch.object(Store, "async_load", load_after_an_answer):
        manager, _, todo = await setup(list(_CALL), answer_stream=_FakeStream())

    assert todo.updates == [{"item": "a", "status": "completed", "entity_id": ENTITY}]
    assert _records(manager)["a"]["watch_until"] is None


async def test_a_reminder_the_stream_reads_gets_one_read_of_its_own_at_the_end(
    hass: HomeAssistant, setup, freezer
) -> None:
    stream = _FakeStream()
    manager, api, _ = await setup(list(_CALL), answer_stream=stream)
    rec = _records(manager)["a"]
    stream.tracked.add(rec["schedule_id"])
    rec.update(status="sent", notification_id=55, watch_until=(NOW + timedelta(hours=24)).isoformat())
    api.poll_notification_answer = AsyncMock(return_value=({"status": "pending"}, False))

    manager._watch_wakeup.set()
    for _ in range(5):
        await hass.async_block_till_done()
    api.poll_notification_answer.assert_not_awaited()

    freezer.move_to(NOW + timedelta(hours=23, minutes=31))
    manager._watch_wakeup.set()
    for _ in range(5):
        await hass.async_block_till_done()
    manager._watch_wakeup.set()
    for _ in range(5):
        await hass.async_block_till_done()

    api.poll_notification_answer.assert_awaited_once_with(55, hold=0)
    assert rec["final_read"] is True


async def test_a_failed_completion_from_the_stream_is_retried_by_an_own_read(hass: HomeAssistant, setup) -> None:
    """The stream reads an answer once: when completing fails, the manager reads it again itself."""
    stream = _FakeStream()
    manager, api, todo = await setup(_CALL, answer_stream=stream)
    rec = _records(manager)["a"]
    rec.update(status="sent", watch_until=(NOW + timedelta(hours=24)).isoformat())
    hass.states.async_set(ENTITY, "unavailable")

    async_dispatcher_send(hass, notification_answer_signal("entry-1"), _done(), rec["schedule_id"])
    for _ in range(5):
        await hass.async_block_till_done()

    assert todo.updates == []
    assert rec["complete_failures"] == 1
    assert rec["watch_until"] is not None
    api.poll_notification_answer.assert_awaited_with(55, hold=20)


# --- with the real answer stream ----------------------------------------------

SEND_AT = NOW + timedelta(hours=1)  # _CALL is due then, and the list has no lead time
SENT_ROW = {
    "id": 100,
    "status": "sent",
    "send_at": SEND_AT.isoformat(),
    "sent_at": SEND_AT.isoformat(),
    "notification_id": 55,
}


def _wired_api(feed: FakeFeed) -> AsyncMock:
    """_api() whose creates and cancels reach the answer stream the way the real client's do."""
    api = _api()
    api.on_answerable_send = None
    api.on_schedule_canceled = None
    api.list_notification_answers = feed
    ids = iter(range(100, 1000))

    async def create(*args, **kwargs):
        created = {"id": next(ids), "status": "scheduled", "send_at": kwargs["send_at"].isoformat()}
        if kwargs.get("actions") and api.on_answerable_send is not None:
            api.on_answerable_send(created, True, window=kwargs.get("answer_window"))
        return created

    async def cancel(schedule_id, *, purge=False):
        if api.on_schedule_canceled is not None:
            api.on_schedule_canceled(schedule_id)

    api.create_notification = AsyncMock(side_effect=create)
    api.cancel_scheduled_notification = AsyncMock(side_effect=cancel)
    api.get_scheduled_notification = AsyncMock(return_value=SENT_ROW)
    return api


@pytest.fixture
async def wired(hass: HomeAssistant, setup):
    """A to-do manager and the entry's real AnswerStream over one fake feed."""
    streams: list[AnswerStream] = []

    async def _start() -> tuple:
        feed = FakeFeed()
        api = _wired_api(feed)
        stream = AnswerStream(hass, api, hass.config_entries.async_get_entry("entry-1"), "hlk_test")
        await stream.async_start()
        streams.append(stream)
        manager, api, todo = await setup(list(_CALL), api=api, answer_stream=stream)
        return manager, api, todo, stream, feed

    with (
        patch("custom_components.pushward.answer_stream.ANSWER_MIN_POLL_INTERVAL", 0),
        patch("custom_components.pushward.answer_stream.ANSWER_STREAM_EARLY_RETURN_JITTER_SECONDS", 0),
    ):
        yield _start
        for stream in streams:
            await stream.async_stop()


async def _until_sent_and_waited_for(hass: HomeAssistant, freezer, stream, feed) -> None:
    """The reminder goes out as notification 55, and the stream learns it."""
    freezer.move_to(SEND_AT)
    async_fire_time_changed(hass, SEND_AT)
    await _until(lambda: len(feed.calls) == 2, "the stream's first read")
    freezer.move_to(SEND_AT + timedelta(seconds=10))
    feed.reply(cursor="100")
    await _until(lambda: "55" in stream._pending, "the notification the reminder became")


async def test_a_done_tap_the_stream_reads_completes_the_item(hass: HomeAssistant, wired, freezer) -> None:
    manager, api, todo, stream, feed = await wired()
    rec = _records(manager)["a"]
    assert stream._schedules["100"]["window"] == 24 * 3600
    await _until_sent_and_waited_for(hass, freezer, stream, feed)

    feed.reply(_feed_answer(55, action_id="done", answered_at=SEND_AT.isoformat()), cursor="101")
    await _until(lambda: todo.updates, "the item to be completed")

    assert todo.updates == [{"item": "a", "status": "completed", "entity_id": ENTITY}]
    assert rec["notification_id"] == 55
    await _until(lambda: rec["watch_until"] is None, "the watch to end")
    await _until(lambda: not stream.running, "the stream to go idle")
    api.poll_notification_answer.assert_not_awaited()


async def test_a_failed_completion_moves_to_the_watch_loops_own_reads(hass: HomeAssistant, wired, freezer) -> None:
    manager, api, todo, stream, feed = await wired()
    rec = _records(manager)["a"]
    await _until_sent_and_waited_for(hass, freezer, stream, feed)
    hass.states.async_set(ENTITY, "unavailable")

    async def poll(notification_id, *, hold):
        todo.write()  # the list is back by the time the manager reads the answer again
        return {"notification_id": notification_id, "status": "answered", "action_id": "done"}, True

    api.poll_notification_answer = AsyncMock(side_effect=poll)
    feed.reply(_feed_answer(55, action_id="done", answered_at=SEND_AT.isoformat()), cursor="101")
    await _until(lambda: todo.updates, "the item to be completed on the retry")

    assert rec["complete_failures"] == 1
    api.poll_notification_answer.assert_awaited_once_with(55, hold=20)
    assert todo.updates == [{"item": "a", "status": "completed", "entity_id": ENTITY}]
    await _until(lambda: rec["watch_until"] is None, "the watch to end")


async def test_a_halted_stream_hands_its_reminders_back(hass: HomeAssistant, wired, freezer) -> None:
    manager, api, _, stream, feed = await wired()
    rec = _records(manager)["a"]
    await _until_sent_and_waited_for(hass, freezer, stream, feed)
    await manager._async_reconcile(SUB)  # marked sent, and watched
    assert rec["status"] == "sent"
    manager._watch_wakeup.set()
    for _ in range(5):
        await hass.async_block_till_done()
    api.poll_notification_answer.assert_not_awaited()  # the stream reads it

    feed.fail(PushWardForbiddenError("no notifications permission", status_code=403))
    await _until(lambda: not stream.running, "the stream to halt")
    manager._watch_wakeup.set()  # its next look, at most TODO_WATCH_IDLE_SECONDS later
    await _until(lambda: api.poll_notification_answer.await_count, "a read of its own")

    api.poll_notification_answer.assert_awaited_with(55, hold=20)


async def test_a_reminder_canceled_before_its_time_leaves_the_stream(hass: HomeAssistant, wired) -> None:
    manager, api, todo, stream, _ = await wired()
    assert set(stream._schedules) == {"100"}
    assert stream._unsub_timer is not None

    todo.items.clear()  # ticked off before the reminder went out
    await manager._async_reconcile(SUB)

    api.cancel_scheduled_notification.assert_awaited_once_with(100, purge=False)
    assert stream._schedules == {}
    assert stream._unsub_timer is None
    assert not stream.running


# --- taking back sent reminders -------------------------------------------

_RETRACT_ON = {CONF_TODO_OFFSET_MINUTES: 30, CONF_TODO_RETRACT: True}


async def _sent(setup, freezer, items: list[dict] | None = None, **cfg) -> tuple:
    """A list whose reminders (due 09:00 UTC, sent 08:30) went out."""
    items = items or [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T09:00:00+00:00"}]
    manager, api, todo = await setup(items, **{**_RETRACT_ON, **cfg})
    freezer.move_to(NOW + timedelta(minutes=45))
    await manager._async_reconcile(SUB)
    assert all(rec["status"] == "sent" for rec in _records(manager).values())
    return manager, api, todo


async def test_completed_item_keeps_its_sent_reminder_with_the_switch_off(setup, freezer) -> None:
    manager, api, todo = await _sent(setup, freezer, **{CONF_TODO_RETRACT: False})

    todo.items[0]["status"] = "completed"
    await manager._async_reconcile(SUB)

    api.retract_notifications.assert_not_awaited()


async def test_old_list_config_without_the_switch_retracts_nothing(setup, freezer) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T09:00:00+00:00"}]
    manager, api, todo = await setup(items, **{CONF_TODO_OFFSET_MINUTES: 30})
    assert CONF_TODO_RETRACT not in manager._configs[SUB]
    freezer.move_to(NOW + timedelta(minutes=45))
    await manager._async_reconcile(SUB)

    todo.items.clear()
    await manager._async_reconcile(SUB)

    api.retract_notifications.assert_not_awaited()


@pytest.mark.parametrize("finish", ["completed", "removed"])
async def test_finished_item_takes_back_its_sent_reminder_once(setup, freezer, finish: str) -> None:
    manager, api, todo = await _sent(setup, freezer)
    collapse_id = api.create_notification.call_args.kwargs["collapse_id"]

    if finish == "completed":
        todo.items[0]["status"] = "completed"
    else:
        todo.items.clear()
    await manager._async_reconcile(SUB)
    await manager._async_reconcile(SUB, full=True)
    await manager._async_reconcile(SUB)

    api.retract_notifications.assert_awaited_once_with(collapse_id)
    assert _records(manager)["a"]["retracted"] is True  # kept until 90 minutes past due


async def test_retracted_flag_survives_a_restart(setup, freezer) -> None:
    manager, api, todo = await _sent(setup, freezer)
    todo.items[0]["status"] = "completed"
    await manager._async_reconcile(SUB)
    api.retract_notifications.assert_awaited_once()
    await manager.async_stop()

    done = [{"uid": "a", "summary": "Call", "status": "completed", "due": "2026-10-01T09:00:00+00:00"}]
    restarted, api2, _ = await setup(done, **_RETRACT_ON)

    assert _records(restarted)["a"]["retracted"] is True
    api2.retract_notifications.assert_not_awaited()


async def test_done_tap_takes_the_reminder_back_before_its_record_goes(setup, freezer) -> None:
    manager, api, todo = await _sent(setup, freezer)
    rec = _records(manager)["a"]
    api.get_scheduled_notification = AsyncMock(
        return_value={"id": rec["schedule_id"], "status": "sent", "notification_id": 55}
    )
    api.poll_notification_answer = AsyncMock(
        return_value=({"notification_id": 55, "status": "answered", "action_id": "done"}, True)
    )
    freezer.move_to(NOW + timedelta(minutes=60 + 91))  # past due and its grace: nothing else keeps the record
    await manager._async_watch_once(SUB, "a", rec, hold=0)
    assert todo.updates == [{"item": "a", "status": "completed", "entity_id": ENTITY}]
    assert rec["watch_until"] is None

    todo.items[0]["status"] = "completed"
    await manager._async_reconcile(SUB)

    api.retract_notifications.assert_awaited_once_with(api.create_notification.call_args.kwargs["collapse_id"])
    assert _records(manager)["a"]["retracted"] is True
    await manager._async_reconcile(SUB)
    assert "a" not in _records(manager)


async def test_pending_reminder_of_a_finished_item_is_canceled_not_retracted(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, todo = await setup(items, **_RETRACT_ON)

    todo.items.clear()
    await manager._async_reconcile(SUB)

    api.cancel_scheduled_notification.assert_awaited_once()
    api.retract_notifications.assert_not_awaited()


async def test_stopped_reminder_is_not_retracted(setup) -> None:
    items = [{"uid": "a", "summary": "Dentist", "status": "needs_action", "due": "2026-10-02T15:00:00+02:00"}]
    manager, api, todo = await setup(items, **_RETRACT_ON)
    api.get_scheduled_notification = AsyncMock(
        return_value={"id": _records(manager)["a"]["schedule_id"], "status": "canceled"}
    )
    await manager._async_reconcile(SUB, full=True)
    assert _records(manager)["a"]["status"] == "stopped"

    todo.items.clear()
    await manager._async_reconcile(SUB)

    api.retract_notifications.assert_not_awaited()


async def test_item_moved_to_a_new_due_time_is_not_retracted(setup, freezer) -> None:
    manager, api, todo = await _sent(setup, freezer)

    todo.items[0] = {**todo.items[0], "due": "2026-10-02T09:00:00+00:00"}
    await manager._async_reconcile(SUB)

    api.retract_notifications.assert_not_awaited()
    assert api.create_notification.await_count == 2
    assert _records(manager)["a"]["status"] == "scheduled"


async def test_failed_retract_is_tried_on_three_passes_then_dropped(setup, freezer) -> None:
    manager, api, todo = await _sent(setup, freezer, **{CONF_TODO_DONE_BUTTON: False})
    api.retract_notifications = AsyncMock(side_effect=PushWardApiError("down", status_code=503))
    todo.items.clear()
    freezer.move_to(NOW + timedelta(minutes=60 + 91))  # the record would finish on this pass

    for attempt in (1, 2):
        await manager._async_reconcile(SUB)
        assert api.retract_notifications.await_count == attempt
        assert _records(manager)["a"]["retract_failures"] == attempt  # kept for the next try
    await manager._async_reconcile(SUB)
    await manager._async_reconcile(SUB)

    assert api.retract_notifications.await_count == 3
    assert "a" not in _records(manager)


async def test_retract_after_a_failure_succeeds_on_the_next_pass(setup, freezer) -> None:
    manager, api, todo = await _sent(setup, freezer)
    api.retract_notifications = AsyncMock(side_effect=[PushWardApiError("busy", status_code=429), 1])
    todo.items.clear()

    await manager._async_reconcile(SUB)
    assert "retracted" not in _records(manager)["a"]
    await manager._async_reconcile(SUB)
    await manager._async_reconcile(SUB)

    assert api.retract_notifications.await_count == 2
    assert _records(manager)["a"]["retracted"] is True


async def test_slow_retract_is_cut_off_and_tried_again(hass: HomeAssistant, setup, freezer) -> None:
    manager, api, todo = await _sent(setup, freezer)
    called = asyncio.Event()

    async def _hang(_collapse_id: str) -> int:
        called.set()
        await asyncio.Event().wait()  # a 429 ladder or an outage
        return 1

    api.retract_notifications = AsyncMock(side_effect=_hang)
    todo.items.clear()
    task = hass.async_create_task(manager._async_reconcile(SUB))
    try:
        await _until(called.is_set, "the retract")
        async_fire_time_changed(hass, NOW + timedelta(minutes=45, seconds=11))
        await _until(task.done, "the retract to be cut off")
    finally:
        task.cancel()  # without the bound the pass would hold the list's lock for good

    assert _records(manager)["a"]["retract_failures"] == 1
    assert "retracted" not in _records(manager)["a"]

    api.retract_notifications = AsyncMock(return_value=1)
    await manager._async_reconcile(SUB)
    api.retract_notifications.assert_awaited_once()
    assert _records(manager)["a"]["retracted"] is True


async def test_at_most_five_retracts_at_once(setup, freezer) -> None:
    due = "2026-10-01T09:00:00+00:00"
    items = [{"uid": f"i{n}", "summary": f"Item {n}", "status": "needs_action", "due": due} for n in range(7)]
    manager, api, todo = await _sent(setup, freezer, items, **{CONF_TODO_DONE_BUTTON: False})
    todo.items.clear()
    freezer.move_to(NOW + timedelta(minutes=60 + 91))  # every record would finish on this pass

    await manager._async_reconcile(SUB)
    assert api.retract_notifications.await_count == 5
    await manager._async_reconcile(SUB)  # the burst is spent
    assert api.retract_notifications.await_count == 5
    assert len(_records(manager)) == 2  # the two still to take back
    freezer.move_to(NOW + timedelta(minutes=60 + 91, seconds=12))  # 10 a minute: room for two
    await manager._async_reconcile(SUB)
    await manager._async_reconcile(SUB)

    assert api.retract_notifications.await_count == 7
    assert len({call.args[0] for call in api.retract_notifications.call_args_list}) == 7
    assert _records(manager) == {}


async def test_retract_waits_for_the_server_to_send_a_reminder_just_due(hass: HomeAssistant, setup, freezer) -> None:
    items = [{"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T09:00:00+00:00"}]
    manager, api, todo = await setup(items, **_RETRACT_ON)
    send_at = datetime(2026, 10, 1, 8, 30, tzinfo=UTC)
    todo.items[0]["status"] = "completed"  # ticked off just before its time
    freezer.move_to(send_at + timedelta(seconds=1))
    await manager._async_reconcile(SUB)

    assert _records(manager)["a"]["status"] == "sent"  # by the clock only: the server may not have sent it yet
    api.retract_notifications.assert_not_awaited()

    freezer.move_to(send_at + timedelta(seconds=12))
    async_fire_time_changed(hass, send_at + timedelta(seconds=12))
    await hass.async_block_till_done()

    api.retract_notifications.assert_awaited_once()
    assert _records(manager)["a"]["retracted"] is True


async def test_rejected_key_on_a_retract_asks_for_reauth_without_using_a_try(setup, freezer) -> None:
    manager, api, todo = await _sent(setup, freezer)
    api.retract_notifications = AsyncMock(side_effect=PushWardAuthError("revoked", status_code=401))
    todo.items.clear()

    with patch.object(manager._entry, "async_start_reauth") as reauth:
        await manager._async_reconcile(SUB)

    reauth.assert_called_once()
    assert "retract_failures" not in _records(manager)["a"]


async def test_a_failed_retract_leaves_the_others_their_tries(setup, freezer) -> None:
    due = "2026-10-01T09:00:00+00:00"
    items = [{"uid": f"i{n}", "summary": f"Item {n}", "status": "needs_action", "due": due} for n in range(3)]
    manager, api, todo = await _sent(setup, freezer, items)
    api.retract_notifications = AsyncMock(side_effect=PushWardApiError("down", status_code=503))
    todo.items.clear()

    await manager._async_reconcile(SUB)

    api.retract_notifications.assert_awaited_once()
    assert sorted(rec.get("retract_failures", 0) for rec in _records(manager).values()) == [0, 0, 1]


async def test_lists_share_one_retract_budget(hass: HomeAssistant, setup, freezer) -> None:
    due = "2026-10-01T09:00:00+00:00"
    items = [{"uid": f"i{n}", "summary": f"Item {n}", "status": "needs_action", "due": due} for n in range(4)]
    manager, api, todo = await setup(items, **_RETRACT_ON)
    # A second list on the same to-do entity: its own records and collapse ids.
    await manager.async_reload([manager._configs[SUB], _config(**{**_RETRACT_ON, CONF_SUBENTRY_ID: "sub-2"})])
    await hass.async_block_till_done()
    freezer.move_to(NOW + timedelta(minutes=45))
    for sub_id in (SUB, "sub-2"):
        await manager._async_reconcile(sub_id)
    todo.items.clear()

    await manager._async_reconcile_all()
    assert api.retract_notifications.await_count == 5  # the account's burst, not 5 per list

    freezer.move_to(NOW + timedelta(minutes=45, seconds=30))  # 10 a minute: room for 5 more
    await manager._async_reconcile_all()
    assert api.retract_notifications.await_count == 8
    assert len({call.args[0] for call in api.retract_notifications.call_args_list}) == 8


async def test_pending_reminders_are_canceled_before_sent_ones_are_taken_back(setup, freezer) -> None:
    items = [
        {"uid": "a", "summary": "Call", "status": "needs_action", "due": "2026-10-01T09:00:00+00:00"},
        {"uid": "b", "summary": "Pay", "status": "needs_action", "due": "2026-10-01T10:00:00+00:00"},
    ]
    manager, api, todo = await setup(items, **_RETRACT_ON)
    freezer.move_to(NOW + timedelta(minutes=45))  # a went out, b is due to
    order: list[str] = []
    api.cancel_scheduled_notification = AsyncMock(side_effect=lambda *a, **kw: order.append("cancel"))
    api.retract_notifications = AsyncMock(side_effect=lambda *a: order.append("retract") or 1)
    todo.items.clear()

    await manager._async_reconcile(SUB)

    assert order == ["cancel", "retract"]


async def test_a_reopened_item_gets_a_collapse_id_that_was_not_taken_back(setup, freezer) -> None:
    manager, api, todo = await _sent(setup, freezer)
    first = api.create_notification.call_args.kwargs["collapse_id"]
    todo.items[0]["status"] = "completed"
    await manager._async_reconcile(SUB)
    api.retract_notifications.assert_awaited_once_with(first)

    # Undone and moved to a time soon after: the retract may still be on its way to a device.
    todo.items[0] = {**todo.items[0], "status": "needs_action", "due": "2026-10-01T09:20:00+00:00"}
    await manager._async_reconcile(SUB)
    second = api.create_notification.call_args.kwargs["collapse_id"]
    assert api.create_notification.await_count == 2
    assert second != first

    freezer.move_to(NOW + timedelta(minutes=55))  # its reminder (08:50) went out too
    todo.items[0]["status"] = "completed"
    await manager._async_reconcile(SUB)
    assert api.retract_notifications.call_args.args == (second,)
    assert len(manager._lists[SUB]["retracted"]) == 2

    freezer.move_to(NOW + timedelta(hours=25))
    await manager._async_reconcile(SUB)
    assert manager._lists[SUB]["retracted"] == {}  # past the hold: the ids are free again


@pytest.mark.parametrize("status", ["canceled", "failed"])
async def test_a_reminder_found_unsent_after_its_time_is_not_retracted(setup, freezer, status: str) -> None:
    manager, api, todo = await _sent(setup, freezer)
    rec = _records(manager)["a"]
    api.get_scheduled_notification = AsyncMock(return_value={"id": rec["schedule_id"], "status": status})
    await manager._async_watch_once(SUB, "a", rec, hold=0)
    assert rec["status"] == "stopped"

    todo.items.clear()
    await manager._async_reconcile(SUB)

    api.retract_notifications.assert_not_awaited()
