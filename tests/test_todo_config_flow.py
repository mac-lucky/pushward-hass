"""Tests for the tracked to-do list subentry flow."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushward.const import (
    CONF_ENTITY_ID,
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    CONF_TODO_ALL_DAY_TIME,
    CONF_TODO_DONE_BUTTON,
    CONF_TODO_LEVEL,
    CONF_TODO_MAX_SCHEDULED,
    CONF_TODO_OFFSET_MINUTES,
    DOMAIN,
    SUBENTRY_TYPE_TODO,
)

FORM = {
    CONF_ENTITY_ID: "todo.reminders",
    CONF_TODO_OFFSET_MINUTES: 15.0,
    CONF_TODO_ALL_DAY_TIME: "08:00:00",
    CONF_TODO_LEVEL: "time-sensitive",
    CONF_TODO_DONE_BUTTON: True,
    CONF_TODO_MAX_SCHEDULED: 5.0,
}
STORED = {**FORM, CONF_TODO_OFFSET_MINUTES: 15, CONF_TODO_MAX_SCHEDULED: 5}


def _entry(*subentries: ConfigSubentryData) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: "https://api.example.com", CONF_INTEGRATION_KEY: "hlk_test"},
        version=2,
        unique_id=DOMAIN,
        subentries_data=list(subentries),
    )


async def test_add_tracked_todo_list(hass: HomeAssistant) -> None:
    hass.states.async_set("todo.reminders", "2", {"friendly_name": "Reminders"})
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_TODO), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input=FORM)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(iter(entry.subentries.values()))
    assert subentry.subentry_type == SUBENTRY_TYPE_TODO
    assert subentry.title == "Reminders"
    assert subentry.unique_id == "todo:todo.reminders"
    assert dict(subentry.data) == STORED


async def test_same_list_twice_aborts(hass: HomeAssistant) -> None:
    existing = ConfigSubentryData(
        data=STORED, subentry_type=SUBENTRY_TYPE_TODO, title="Reminders", unique_id="todo:todo.reminders"
    )
    entry = _entry(existing)
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_TODO), context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input=FORM)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_tracked_todo_list(hass: HomeAssistant) -> None:
    existing = ConfigSubentryData(
        data=STORED, subentry_type=SUBENTRY_TYPE_TODO, title="Reminders", unique_id="todo:todo.reminders"
    )
    entry = _entry(existing)
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input={**FORM, CONF_TODO_OFFSET_MINUTES: 60.0, CONF_TODO_DONE_BUTTON: False}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    data = entry.subentries[subentry_id].data
    assert data[CONF_TODO_OFFSET_MINUTES] == 60
    assert data[CONF_TODO_DONE_BUTTON] is False


async def test_list_already_tracked_as_an_activity_can_be_added(hass: HomeAssistant) -> None:
    """Subentry unique ids are checked across types; the to-do list one is prefixed."""
    activity = ConfigSubentryData(
        data={CONF_ENTITY_ID: "todo.reminders"},
        subentry_type="tracked_entity",
        title="Reminders activity",
        unique_id="todo.reminders",
    )
    entry = _entry(activity)
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_TODO), context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input=FORM)

    assert result["type"] is FlowResultType.CREATE_ENTRY
