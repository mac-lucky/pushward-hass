"""Tests for the to-do list reminders (tracked_todo subentries)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pushward.api import PushWardApiError, PushWardNotFoundError
from custom_components.pushward.const import (
    CONF_ENTITY_ID,
    CONF_SUBENTRY_ID,
    CONF_TODO_ALL_DAY_TIME,
    CONF_TODO_DONE_BUTTON,
    CONF_TODO_LEVEL,
    CONF_TODO_MAX_SCHEDULED,
    CONF_TODO_OFFSET_MINUTES,
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
            return {eid: {"items": list(self.items)} for eid in entity_ids}

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

    async def _start(items: list[dict], api: AsyncMock | None = None, **cfg) -> tuple:
        todo = _TodoList(hass, items)
        api = api or _api()
        manager = TodoReminderManager(hass, api, [_config(**cfg)], entry, "hlk_test")
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

    # A new key that still cannot encrypt raises it again.
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
