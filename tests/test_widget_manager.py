"""Tests for the PushWard widget manager."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.pushward.api import (
    PushWardApiError,
    PushWardAuthError,
    PushWardNotFoundError,
    PushWardWidgetPermissionError,
)
from custom_components.pushward.const import (
    CONF_BATTERY_DEVICES,
    CONF_ENTITY_ID,
    CONF_FLOW_NODES,
    CONF_HEATMAP_AGGREGATE,
    CONF_HEATMAP_WEEKS,
    CONF_HISTORY_PERIOD,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_SLUG,
    CONF_STAT_ROWS,
    CONF_SUBTITLE_TIMER_ENTITY,
    CONF_WIDGET_DENSITY,
    CONF_WIDGET_POLL_INTERVAL,
    CONF_WIDGET_STALE_AFTER,
    CONF_WIDGET_STYLE,
    CONF_WIDGET_TEMPLATE,
    CONF_WIDGET_TRIGGER_MODE,
    WIDGET_GROUP_TEMPLATES,
    WIDGET_STALE_AFTER_MIN,
    WIDGET_TEMPLATE_BATTERY,
    WIDGET_TEMPLATE_FLOW,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_HEATMAP,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_TREND,
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TRIGGER_POLL,
    widget_look_keys,
)
from custom_components.pushward.quota import quota_released_signal
from custom_components.pushward.recorder_history import async_daily_values
from custom_components.pushward.widget_manager import (
    _GROUP_ROW_SOURCES,
    WidgetManager,
    _entity_ids_for_widget,
    _permission_notification_id,
)

from .conftest import make_mock_entry, make_quota_error, make_widget_config
from .server_contract import assert_valid_widget_content


def _mock_api() -> AsyncMock:
    api = AsyncMock()
    api.create_widget = AsyncMock()
    api.patch_widget = AsyncMock()
    api.delete_widget = AsyncMock()
    return api


async def test_reload_deletes_removed_widget(hass: HomeAssistant) -> None:
    """Removing a tracked widget on reload deletes the server-side widget (no orphan leak)."""
    api = _mock_api()
    hass.states.async_set("sensor.users", "42")
    hass.states.async_set("sensor.power", "7")
    kept = make_widget_config(slug="ha-users", entity_id="sensor.users")
    removed = make_widget_config(slug="ha-power", entity_id="sensor.power")

    manager = WidgetManager(hass, api, [kept, removed], make_mock_entry())
    await manager.async_start()

    # Reload with only the kept widget → the removed one must be deleted server-side.
    await manager.async_reload([kept])

    api.delete_widget.assert_awaited_once_with("ha-power")

    await manager.async_stop()


async def test_reload_without_removal_deletes_nothing(hass: HomeAssistant) -> None:
    """A reload that keeps the same widgets must not delete anything."""
    api = _mock_api()
    hass.states.async_set("sensor.users", "42")
    config = make_widget_config()

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()

    await manager.async_reload([config])

    api.delete_widget.assert_not_awaited()

    await manager.async_stop()


async def test_reload_isolates_delete_failures(hass: HomeAssistant) -> None:
    """One failing server-delete must not strand the other removed widgets' deletes."""
    api = _mock_api()
    api.delete_widget.side_effect = [PushWardApiError("boom"), None]
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")
    one = make_widget_config(slug="ha-a", entity_id="sensor.a")
    two = make_widget_config(slug="ha-b", entity_id="sensor.b")

    manager = WidgetManager(hass, api, [one, two], make_mock_entry())
    await manager.async_start()

    # Remove both → both deletes attempted even though the first raises.
    await manager.async_reload([])

    assert api.delete_widget.await_count == 2
    assert {c.args[0] for c in api.delete_widget.await_args_list} == {"ha-a", "ha-b"}

    await manager.async_stop()


async def test_slug_for_entity_resolves_and_misses(hass: HomeAssistant) -> None:
    """slug_for_entity maps a bound entity to its widget slug, else None."""
    api = _mock_api()
    hass.states.async_set("sensor.users", "42")
    config = make_widget_config(slug="ha-users", entity_id="sensor.users")
    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()

    assert manager.slug_for_entity("sensor.users") == "ha-users"
    assert manager.slug_for_entity("sensor.nope") is None
    assert manager.slug_for_entity(None) is None

    await manager.async_stop()


async def test_initial_post_on_start(hass: HomeAssistant) -> None:
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()

    api.create_widget.assert_awaited_once()
    call_kwargs = api.create_widget.call_args.kwargs
    assert call_kwargs["slug"] == "ha-users"
    assert call_kwargs["template"] == WIDGET_TEMPLATE_VALUE
    assert call_kwargs["content"]["value"] == 42.0

    await manager.async_stop()


async def test_state_change_patches_only_when_changed(hass: HomeAssistant) -> None:
    """Changing state pushes a PATCH; resending the same state does NOT."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()

    api.reset_mock()

    # Change the value → expect a PATCH
    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()

    assert api.patch_widget.await_count == 1
    body = api.patch_widget.call_args.args[1]
    assert body["content"]["value"] == 43.0

    # Re-fire the same value (no actual change) — no PATCH
    api.reset_mock()
    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()
    api.patch_widget.assert_not_called()

    await manager.async_stop()


async def test_patch_404_recreates_widget(hass: HomeAssistant) -> None:
    """A PATCH that 404s (widget gone server-side) self-heals via a recreate POST."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()  # initial create
    assert api.create_widget.await_count == 1

    # Server has since lost the widget; the next PATCH 404s.
    api.reset_mock()
    api.patch_widget = AsyncMock(side_effect=PushWardNotFoundError("widget not found", status_code=404))

    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()

    # PATCH was attempted once, then recovered by re-POSTing the fresh content.
    assert api.patch_widget.await_count == 1
    api.create_widget.assert_awaited_once()
    assert api.create_widget.call_args.kwargs["content"]["value"] == 43.0
    assert manager._tracked["ha-users"].created is True

    await manager.async_stop()


async def test_unavailable_state_skipped(hass: HomeAssistant) -> None:
    """Going to unavailable does not push anything."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    api.reset_mock()

    hass.states.async_set("sensor.users", STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    api.patch_widget.assert_not_called()
    api.create_widget.assert_not_called()

    await manager.async_stop()


async def test_poll_mode_couples_push_throttle(hass: HomeAssistant) -> None:
    """When trigger mode is `poll`, push_throttle equals the poll interval."""
    api = _mock_api()
    config = make_widget_config(**{CONF_WIDGET_TRIGGER_MODE: WIDGET_TRIGGER_POLL, CONF_WIDGET_POLL_INTERVAL: 30})
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()

    api.create_widget.assert_awaited_once()
    assert api.create_widget.call_args.kwargs["push_throttle"] == 30

    await manager.async_stop()


async def test_event_mode_omits_push_throttle(hass: HomeAssistant) -> None:
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()

    assert api.create_widget.call_args.kwargs["push_throttle"] is None

    await manager.async_stop()


async def test_manual_refresh_force_patches_unchanged(hass: HomeAssistant) -> None:
    """async_refresh bypasses the diff cache."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    api.reset_mock()

    await manager.async_refresh(slug="ha-users")
    assert api.patch_widget.await_count == 1

    await manager.async_stop()


async def test_manual_refresh_by_entity_id(hass: HomeAssistant) -> None:
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    api.reset_mock()

    await manager.async_refresh(entity_id="sensor.users")
    assert api.patch_widget.await_count == 1

    await manager.async_stop()


async def test_manual_refresh_unknown_raises(hass: HomeAssistant) -> None:
    api = _mock_api()
    manager = WidgetManager(hass, api, [], make_mock_entry())
    await manager.async_start()

    with pytest.raises(ValueError):
        await manager.async_refresh(slug="nope")
    with pytest.raises(ValueError):
        await manager.async_refresh(entity_id="sensor.nope")

    await manager.async_stop()


async def test_widget_permission_403_surfaces_notification(hass: HomeAssistant) -> None:
    """403 widget-permission errors trigger a persistent notification."""
    api = _mock_api()
    api.create_widget = AsyncMock(
        side_effect=PushWardWidgetPermissionError("integration key does not have widget permission", status_code=403)
    )
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    with patch("custom_components.pushward.widget_manager.persistent_notification.async_create") as create_notif:
        entry = make_mock_entry()
        manager = WidgetManager(hass, api, [config], entry)
        await manager.async_start()
        assert create_notif.called
        assert create_notif.call_args.kwargs["notification_id"] == _permission_notification_id(entry.entry_id)

    await manager.async_stop()


async def test_auth_error_triggers_reauth(hass: HomeAssistant) -> None:
    """401 / PushWardAuthError calls entry.async_start_reauth exactly once."""
    api = _mock_api()
    api.create_widget = AsyncMock(side_effect=PushWardAuthError("invalid key", status_code=401))
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    entry = make_mock_entry()
    manager = WidgetManager(hass, api, [config], entry)
    await manager.async_start()
    entry.async_start_reauth.assert_called_once_with(hass)

    # Subsequent failure should not re-trigger.
    await manager.async_refresh(slug="ha-users")
    entry.async_start_reauth.assert_called_once()

    await manager.async_stop()


async def test_reload_swaps_widget_set(hass: HomeAssistant) -> None:
    api = _mock_api()
    config_a = make_widget_config(**{CONF_ENTITY_ID: "sensor.a", CONF_SLUG: "ha-a"})
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")

    manager = WidgetManager(hass, api, [config_a], make_mock_entry())
    await manager.async_start()
    assert "ha-a" in manager._tracked

    config_b = make_widget_config(**{CONF_ENTITY_ID: "sensor.b", CONF_SLUG: "ha-b"})
    await manager.async_reload([config_b])
    assert "ha-a" not in manager._tracked
    assert "ha-b" in manager._tracked

    await manager.async_stop()


async def test_gauge_initial_sync_defers_when_value_unavailable(hass: HomeAssistant) -> None:
    """Gauge POST is deferred until a valid numeric value arrives."""
    api = _mock_api()
    config = make_widget_config(**{CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_GAUGE})
    # Entity has no state yet — gauge requires a numeric value, so create is deferred.
    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    api.create_widget.assert_not_called()

    # First valid state arrives → fires create on the deferred-init path.
    hass.states.async_set("sensor.users", "50")
    await hass.async_block_till_done()

    api.create_widget.assert_awaited_once()
    assert api.create_widget.call_args.kwargs["content"]["value"] == 50.0

    await manager.async_stop()


async def test_stat_list_initial_sync_with_multiple_entities(hass: HomeAssistant) -> None:
    """stat_list widgets subscribe to every row entity and POST aggregated content."""
    api = _mock_api()
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
            CONF_STAT_ROWS: [
                {"label": "Users", "entity_id": "sensor.users"},
                {"label": "Active", "entity_id": "sensor.active"},
            ],
        }
    )
    hass.states.async_set("sensor.users", "42")
    hass.states.async_set("sensor.active", "10")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()

    api.create_widget.assert_awaited_once()
    rows = api.create_widget.call_args.kwargs["content"]["stat_rows"]
    assert rows == [
        {"label": "Users", "value": "42"},
        {"label": "Active", "value": "10"},
    ]

    # Changing either row's entity triggers a PATCH.
    api.reset_mock()
    hass.states.async_set("sensor.active", "11")
    await hass.async_block_till_done()
    assert api.patch_widget.await_count == 1

    await manager.async_stop()


async def test_cache_survives_restart(hass: HomeAssistant) -> None:
    """Persisted cache marks the widget as already created so a restart skips re-POSTing identical content."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    api.create_widget.assert_awaited_once()
    await manager.async_stop()

    # Same entry_id → same Store key. New manager should load cache and find
    # the content unchanged, so no PATCH on identical state.
    api2 = _mock_api()
    manager2 = WidgetManager(hass, api2, [config], make_mock_entry())
    await manager2.async_start()
    api2.create_widget.assert_awaited_once()  # initial sync is idempotent upsert
    api2.reset_mock()

    hass.states.async_set("sensor.users", "42")
    await hass.async_block_till_done()
    api2.patch_widget.assert_not_called()

    await manager2.async_stop()


_STAT_LIST = {
    CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_STAT_LIST,
    CONF_STAT_ROWS: [{"label": "Users", "entity_id": "sensor.users"}],
}
_LOOKS = {
    CONF_WIDGET_STYLE: "bold",
    "widget_style_large": "classic",
    CONF_WIDGET_DENSITY: "compact",
    "widget_density_lock_screen": "comfortable",
}
_SENT_LOOKS = {
    "style": "bold",
    "style_by_size": {"large": "classic"},
    "density": "compact",
    "density_by_size": {"lock_screen": "comfortable"},
}


def _looks_of(content: dict) -> dict:
    return {k: content[k] for k in _SENT_LOOKS if k in content}


@pytest.mark.parametrize(("looks", "sent"), [({}, {}), (_LOOKS, _SENT_LOOKS)], ids=["not_set", "set"])
async def test_looks_on_create_and_restart_repost(hass: HomeAssistant, looks: dict, sent: dict) -> None:
    """POST replaces the content, so set looks ride every POST and unset ones never appear."""
    config = make_widget_config(**_STAT_LIST, **looks)
    hass.states.async_set("sensor.users", "42")

    for _ in range(2):  # first start, then the re-POST after an HA restart
        api = _mock_api()
        manager = WidgetManager(hass, api, [config], make_mock_entry())
        await manager.async_start()
        assert _looks_of(api.create_widget.call_args.kwargs["content"]) == sent
        await manager.async_stop()


async def test_looks_switched_to_not_set_repost_without_them(hass: HomeAssistant) -> None:
    """The reconfigure reload re-POSTs without the keys, which is what clears them server-side."""
    api = _mock_api()
    hass.states.async_set("sensor.users", "42")
    manager = WidgetManager(hass, api, [make_widget_config(**_STAT_LIST, **_LOOKS)], make_mock_entry())
    await manager.async_start()
    assert _looks_of(api.create_widget.call_args.kwargs["content"]) == _SENT_LOOKS

    api.reset_mock()
    await manager.async_reload([make_widget_config(**_STAT_LIST)])
    assert _looks_of(api.create_widget.call_args.kwargs["content"]) == {}
    api.patch_widget.assert_not_called()

    await manager.async_stop()


async def test_looks_dropped_on_the_patch_path_send_null(hass: HomeAssistant) -> None:
    """When the restart POST is deferred, the next PATCH clears each dropped look once and then leaves it out."""
    api = _mock_api()
    hass.states.async_set("sensor.users", "42")
    manager = WidgetManager(hass, api, [make_widget_config(**_STAT_LIST, **_LOOKS)], make_mock_entry())
    await manager.async_start()
    await manager.async_stop()

    # Restart with all but the style removed while the row entity is unavailable: no POST yet.
    hass.states.async_set("sensor.users", STATE_UNAVAILABLE)
    api2 = _mock_api()
    config = make_widget_config(**_STAT_LIST, **{CONF_WIDGET_STYLE: "bold"})
    manager2 = WidgetManager(hass, api2, [config], make_mock_entry())
    await manager2.async_start()
    api2.create_widget.assert_not_called()

    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()
    assert _looks_of(api2.patch_widget.call_args.args[1]["content"]) == {
        "style": "bold",
        "style_by_size": None,
        "density": None,
        "density_by_size": None,
    }

    hass.states.async_set("sensor.users", "44")
    await hass.async_block_till_done()
    assert api2.patch_widget.await_count == 2
    assert _looks_of(api2.patch_widget.call_args.args[1]["content"]) == {"style": "bold"}

    await manager2.async_stop()


async def test_set_looks_ride_every_patch(hass: HomeAssistant) -> None:
    """A PATCH replaces a whole per-size map, so the full map rides every PATCH."""
    api = _mock_api()
    hass.states.async_set("sensor.users", "42")
    manager = WidgetManager(hass, api, [make_widget_config(**_STAT_LIST, **_LOOKS)], make_mock_entry())
    await manager.async_start()

    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()
    assert _looks_of(api.patch_widget.call_args.args[1]["content"]) == _SENT_LOOKS

    await manager.async_stop()


async def test_subentry_saved_without_look_keys_sends_none(hass: HomeAssistant) -> None:
    """A widget saved before these settings existed has none of the keys: nothing sent and no null clear."""
    config = make_widget_config(**_STAT_LIST)
    for key in (*widget_look_keys(CONF_WIDGET_STYLE), *widget_look_keys(CONF_WIDGET_DENSITY)):
        del config[key]
    api = _mock_api()
    hass.states.async_set("sensor.users", "42")
    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    assert _looks_of(api.create_widget.call_args.kwargs["content"]) == {}

    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()
    assert _looks_of(api.patch_widget.call_args.args[1]["content"]) == {}

    await manager.async_stop()


async def test_patch_404_recreate_rearms_on_redeletion(hass: HomeAssistant) -> None:
    """A recreate that sticks re-arms the guard so a later re-deletion self-heals again.

    Regression for the flag never resetting on the recreate path: without the reset a
    second out-of-band deletion (no clean PATCH between) would be skipped forever and the
    widget would stay dead.
    """
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    api.reset_mock()
    api.patch_widget = AsyncMock(side_effect=PushWardNotFoundError("widget not found", status_code=404))

    # First deletion: PATCH 404s, recreate POST succeeds -> guard re-armed.
    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()
    api.create_widget.assert_awaited_once()
    assert manager._tracked["ha-users"].recreate_attempted is False

    # Deleted AGAIN with no clean PATCH between -> the recreate must fire a 2nd time.
    hass.states.async_set("sensor.users", "44")
    await hass.async_block_till_done()
    assert api.create_widget.await_count == 2
    assert manager._tracked["ha-users"].recreate_attempted is False

    # A successful PATCH keeps the guard re-armed.
    api.patch_widget = AsyncMock()
    hass.states.async_set("sensor.users", "45")
    await hass.async_block_till_done()
    api.patch_widget.assert_awaited_once()
    assert manager._tracked["ha-users"].recreate_attempted is False

    await manager.async_stop()


async def test_patch_404_recreate_guard_holds_when_create_fails(hass: HomeAssistant) -> None:
    """The one-recreate-per-streak guard still holds when the recreate POST itself fails.

    If the recreate POST raises (server still refusing), the flag stays latched so the
    next 404 does not hammer the server with another create attempt.
    """
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()  # initial create succeeds -> created=True
    api.reset_mock()
    api.patch_widget = AsyncMock(side_effect=PushWardNotFoundError("widget not found", status_code=404))
    api.create_widget = AsyncMock(side_effect=PushWardApiError("server down", status_code=500))

    # PATCH 404s, the recreate POST also fails -> guard stays latched.
    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()
    assert api.create_widget.await_count == 1
    assert manager._tracked["ha-users"].recreate_attempted is True

    # Next 404: no second create attempt while the guard is latched.
    hass.states.async_set("sensor.users", "44")
    await hass.async_block_till_done()
    assert api.create_widget.await_count == 1
    assert api.patch_widget.await_count == 2

    await manager.async_stop()


async def test_widget_burst_trailing_resend(hass: HomeAssistant) -> None:
    """Changes landing during an in-flight PATCH re-send the newest state after."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    api.reset_mock()

    gate = asyncio.Event()
    started = asyncio.Event()

    async def _gated(*_a, **_k):
        started.set()
        await gate.wait()

    api.patch_widget.side_effect = _gated

    hass.states.async_set("sensor.users", "43")
    # Wait for the send to actually reach the gate. A bare sleep(0) yields a single loop
    # tick, which the task doesn't always win on a loaded runner (CI runs under coverage).
    await asyncio.wait_for(started.wait(), timeout=5)
    hass.states.async_set("sensor.users", "44")
    hass.states.async_set("sensor.users", "45")
    await asyncio.sleep(0)
    assert api.patch_widget.await_count == 1

    api.patch_widget.side_effect = None
    gate.set()
    await hass.async_block_till_done()

    # One gated PATCH plus one trailing re-send carrying the newest value.
    assert api.patch_widget.await_count == 2
    assert api.patch_widget.call_args.args[1]["content"]["value"] == 45.0

    await manager.async_stop()


async def test_refresh_waits_for_inflight_send(hass: HomeAssistant) -> None:
    """A forced refresh waits out an in-flight send, then runs its own -- they never overlap.

    Two _send_update calls for one widget must not interleave (they race last_content
    and the 404 recreate flag). async_refresh blocks on the pending task first.
    """
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()  # widget created; PATCH is the update path now

    gate = asyncio.Event()
    started = asyncio.Event()
    concurrency = 0
    peak = 0
    calls = 0

    async def _patch(*_a, **_k):
        nonlocal concurrency, peak, calls
        calls += 1
        first = calls == 1
        concurrency += 1
        peak = max(peak, concurrency)
        try:
            if first:
                started.set()
                await gate.wait()
        finally:
            concurrency -= 1

    api.patch_widget.side_effect = _patch

    # Kick off an event-driven send and let it reach the gate.
    hass.states.async_set("sensor.users", "43")
    await asyncio.wait_for(started.wait(), timeout=5)
    assert api.patch_widget.call_count == 1

    refresh_task = hass.async_create_task(manager.async_refresh(slug="ha-users"))
    for _ in range(10):
        await asyncio.sleep(0)
    # The refresh is parked on the in-flight send; no second PATCH yet.
    assert not refresh_task.done()
    assert api.patch_widget.call_count == 1

    gate.set()
    await refresh_task
    await hass.async_block_till_done()

    assert api.patch_widget.call_count == 2
    assert peak == 1  # the two sends never ran concurrently

    await manager.async_stop()


async def test_initial_sync_coalesces_startup_state_change(hass: HomeAssistant) -> None:
    """A state change during the boot-time create coalesces via the dirty flag, not a second POST."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())

    gate = asyncio.Event()
    started = asyncio.Event()

    async def _create(*_a, **_k):
        started.set()
        await gate.wait()

    api.create_widget.side_effect = _create

    start_task = hass.async_create_task(manager.async_start())
    await asyncio.wait_for(started.wait(), timeout=5)  # initial POST is in flight
    assert api.create_widget.call_count == 1

    # A startup state change lands mid-create. The listener is already attached, so
    # this must set the dirty flag against the create's single-flight slot, not race
    # a second POST for the same slug.
    hass.states.async_set("sensor.users", "43")
    for _ in range(10):
        await asyncio.sleep(0)
    assert api.create_widget.call_count == 1
    assert api.patch_widget.call_count == 0

    api.create_widget.side_effect = None
    gate.set()
    await start_task
    await hass.async_block_till_done()

    # The coalesced newest value is re-sent exactly once, as a PATCH.
    assert api.create_widget.call_count == 1
    assert api.patch_widget.await_count == 1
    assert api.patch_widget.call_args.args[1]["content"]["value"] == 43.0

    await manager.async_stop()


async def test_push_failure_warns_once_per_streak(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    """Repeated push failures WARN once, then DEBUG; recovery logs INFO and re-arms."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    api.reset_mock()

    def _warnings() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "ha-users" in r.getMessage()]

    api.patch_widget = AsyncMock(side_effect=PushWardApiError("boom"))
    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.users", "44")
    await hass.async_block_till_done()
    assert len(_warnings()) == 1

    api.patch_widget = AsyncMock()
    hass.states.async_set("sensor.users", "45")
    await hass.async_block_till_done()
    assert any("succeeding again" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)

    api.patch_widget = AsyncMock(side_effect=PushWardApiError("boom"))
    hass.states.async_set("sensor.users", "46")
    await hass.async_block_till_done()
    assert len(_warnings()) == 2

    await manager.async_stop()


# ----- stale_after + heartbeat -----


async def test_stale_after_on_create_and_patch(hass: HomeAssistant) -> None:
    api = _mock_api()
    config = make_widget_config(**{CONF_WIDGET_STALE_AFTER: 3600})
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    assert api.create_widget.call_args.kwargs["stale_after"] == 3600

    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()
    assert api.patch_widget.call_args.args[1]["stale_after"] == 3600

    await manager.async_stop()


async def test_stale_after_clamped_to_server_bounds(hass: HomeAssistant) -> None:
    """A hand-written config past the bounds is nudged, not sent as-is (the server 422s it)."""
    api = _mock_api()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [make_widget_config(**{CONF_WIDGET_STALE_AFTER: 5})], make_mock_entry())
    await manager.async_start()
    assert api.create_widget.call_args.kwargs["stale_after"] == WIDGET_STALE_AFTER_MIN
    await manager.async_stop()


async def test_patch_clears_stale_after_when_unset(hass: HomeAssistant) -> None:
    """An explicit null on every PATCH converges a row created before the field existed."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()

    body = api.patch_widget.call_args.args[1]
    assert "stale_after" in body
    assert body["stale_after"] is None

    await manager.async_stop()


async def test_heartbeat_armed_only_with_stale_after(hass: HomeAssistant) -> None:
    api = _mock_api()
    hass.states.async_set("sensor.users", "42")

    plain = WidgetManager(hass, api, [make_widget_config()], make_mock_entry())
    await plain.async_start()
    assert plain._tracked["ha-users"].unsub_heartbeat is None
    await plain.async_stop()

    ticking = WidgetManager(hass, api, [make_widget_config(**{CONF_WIDGET_STALE_AFTER: 3600})], make_mock_entry())
    await ticking.async_start()
    tracked = ticking._tracked["ha-users"]
    assert tracked.unsub_heartbeat is not None
    await ticking.async_stop()
    # async_stop detaches, so a later tick can't fire against a dead tracker.
    assert tracked.unsub_heartbeat is None


async def test_heartbeat_patches_despite_identical_content(hass: HomeAssistant) -> None:
    """The whole point: an unchanged entity must still re-stamp updated_at."""
    api = _mock_api()
    config = make_widget_config(**{CONF_WIDGET_STALE_AFTER: 3600})
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    api.patch_widget.assert_not_called()
    tracked = manager._tracked["ha-users"]

    # last_synced is monotonic, which async_fire_time_changed does not advance;
    # backdating it is what makes the tick read as "nothing sent in a while".
    for tick in (1, 2):
        tracked.last_synced -= 100000
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1801 * tick))
        await hass.async_block_till_done()

    # Content never changed, yet both ticks PATCHed: that is the whole point.
    assert api.patch_widget.await_count == 2
    await manager.async_stop()


async def test_heartbeat_skipped_after_recent_send(hass: HomeAssistant) -> None:
    api = _mock_api()
    config = make_widget_config(**{CONF_WIDGET_STALE_AFTER: 3600})
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], make_mock_entry())
    await manager.async_start()
    # A real update just landed, so the tick has nothing to keep alive.
    manager._tracked["ha-users"].last_synced = time.monotonic()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1801))
    await hass.async_block_till_done()

    api.patch_widget.assert_not_called()
    await manager.async_stop()


# ----- trend points -----


def _trend_config(**overrides) -> dict:
    base = {
        CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_TREND,
        CONF_SLUG: "ha-trend",
        CONF_ENTITY_ID: "sensor.power",
        CONF_MIN_VALUE: None,
        CONF_MAX_VALUE: None,
    }
    base.update(overrides)
    return make_widget_config(**base)


async def test_trend_defers_create_until_two_points(hass: HomeAssistant) -> None:
    api = _mock_api()
    hass.states.async_set("sensor.power", "100")

    manager = WidgetManager(hass, api, [_trend_config()], make_mock_entry())
    await manager.async_start()
    # One sample is not a sparkline; the create defers like gauge does.
    api.create_widget.assert_not_called()

    hass.states.async_set("sensor.power", "120")
    await hass.async_block_till_done()
    api.create_widget.assert_awaited_once()
    assert api.create_widget.call_args.kwargs["content"]["points"] == [100.0, 120.0]

    await manager.async_stop()


async def test_trend_buffer_dedupes_unchanged_value(hass: HomeAssistant) -> None:
    api = _mock_api()
    hass.states.async_set("sensor.power", "100")

    manager = WidgetManager(hass, api, [_trend_config()], make_mock_entry())
    await manager.async_start()
    tracked = manager._tracked["ha-trend"]

    # Same value inside the gap window records nothing new.
    manager._append_point(tracked, time.time() + 1, 100.0)
    assert len(tracked.points_buffer) == 1
    # A different value always records.
    manager._append_point(tracked, time.time() + 2, 101.0)
    assert len(tracked.points_buffer) == 2
    # An unchanged value past the gap records again, so a flat line still has points.
    manager._append_point(tracked, time.time() + 100000, 101.0)
    assert len(tracked.points_buffer) == 3

    await manager.async_stop()


async def test_trend_points_persist_and_restore(hass: HomeAssistant) -> None:
    api = _mock_api()
    hass.states.async_set("sensor.power", "100")

    manager = WidgetManager(hass, api, [_trend_config()], make_mock_entry())
    await manager.async_start()
    hass.states.async_set("sensor.power", "120")
    await hass.async_block_till_done()
    cached = manager._serialize_cache()["widgets"]["ha-trend"]
    assert cached["points"] == [[pytest.approx(ts), v] for ts, v in manager._tracked["ha-trend"].points_buffer]
    await manager.async_stop()

    manager2 = WidgetManager(hass, api, [_trend_config()], make_mock_entry())
    await manager2.async_start()
    # The restored buffer is what keeps a restart from pushing a flat two-point line.
    assert [v for _ts, v in manager2._tracked["ha-trend"].points_buffer] == [100.0, 120.0]
    await manager2.async_stop()


async def test_trend_seeds_from_recorder_when_history_configured(hass: HomeAssistant) -> None:
    api = _mock_api()
    hass.states.async_set("sensor.power", "130")
    config = _trend_config(**{CONF_HISTORY_PERIOD: 60})

    history = {"sensor.power": [{"timestamp": 1000 + i, "value": float(i)} for i in range(10)]}
    with patch(
        "custom_components.pushward.widget_manager.async_recorder_states",
        AsyncMock(return_value=history),
    ) as seed:
        manager = WidgetManager(hass, api, [config], make_mock_entry())
        await manager.async_start()

    seed.assert_awaited_once()
    points = api.create_widget.call_args.kwargs["content"]["points"]
    assert points[0] == 0.0
    assert points[-1] == 130.0
    await manager.async_stop()


async def test_trend_seed_skipped_when_buffer_restored(hass: HomeAssistant) -> None:
    """A restored cache already spans the window; re-seeding would re-merge aged-out points."""
    api = _mock_api()
    hass.states.async_set("sensor.power", "100")
    config = _trend_config(**{CONF_HISTORY_PERIOD: 60})

    with patch(
        "custom_components.pushward.widget_manager.async_recorder_states",
        AsyncMock(return_value={"sensor.power": [{"timestamp": 1, "value": 1.0}]}),
    ):
        manager = WidgetManager(hass, api, [config], make_mock_entry())
        await manager.async_start()
        hass.states.async_set("sensor.power", "120")
        await hass.async_block_till_done()
        await manager.async_stop()

        manager2 = WidgetManager(hass, api, [config], make_mock_entry())
        with patch("custom_components.pushward.widget_manager.async_recorder_states", AsyncMock()) as reseed:
            await manager2.async_start()
        reseed.assert_not_awaited()
        await manager2.async_stop()


# ----- subscription sets -----


async def test_battery_subscribes_to_row_and_charging_entities(hass: HomeAssistant) -> None:
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_BATTERY,
            CONF_BATTERY_DEVICES: [
                {"name": "Phone", "entity_id": "sensor.phone", "charging_entity": "binary_sensor.phone_charging"}
            ],
        }
    )
    assert _entity_ids_for_widget(config) == ["sensor.phone", "binary_sensor.phone_charging"]


async def test_flow_subscribes_to_rate_total_and_level_entities(hass: HomeAssistant) -> None:
    config = make_widget_config(
        **{
            CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_FLOW,
            CONF_FLOW_NODES: [
                {
                    "slot": "storage",
                    "entity_id": "sensor.batt",
                    "total_entity": "sensor.batt_total",
                    "level_entity": "sensor.batt_soc",
                }
            ],
        }
    )
    assert _entity_ids_for_widget(config) == ["sensor.batt", "sensor.batt_total", "sensor.batt_soc"]


async def test_single_entity_template_subscribes_to_subtitle_timer_entity(hass: HomeAssistant) -> None:
    config = make_widget_config(**{CONF_SUBTITLE_TIMER_ENTITY: "sensor.next_run"})
    assert _entity_ids_for_widget(config) == ["sensor.users", "sensor.next_run"]


async def test_battery_and_flow_skip_registry_icon(hass: HomeAssistant) -> None:
    """No anchoring entity means no registry icon lookup - only the static config icon applies."""
    api = _mock_api()
    manager = WidgetManager(hass, api, [], make_mock_entry())
    for template in (WIDGET_TEMPLATE_BATTERY, WIDGET_TEMPLATE_FLOW, WIDGET_TEMPLATE_STAT_LIST):
        assert manager._lookup_registry_icon(make_widget_config(**{CONF_WIDGET_TEMPLATE: template})) is None


async def test_group_row_sources_cover_every_group_template(hass: HomeAssistant) -> None:
    """Drift guard: a template added to WIDGET_GROUP_TEMPLATES needs a rows key here.

    Without it _entity_ids_for_widget silently falls through to the single-entity
    branch and the widget subscribes to nothing.
    """
    assert set(_GROUP_ROW_SOURCES) == set(WIDGET_GROUP_TEMPLATES)


# --- quota gate ---


async def test_quota_error_is_swallowed_quietly_and_resent_on_release(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    api = _mock_api()
    entry = make_mock_entry("quota_entry")
    hass.states.async_set("sensor.users", "42")
    manager = WidgetManager(hass, api, [make_widget_config()], entry)
    await manager.async_start()
    api.reset_mock()
    tracked = manager._tracked["ha-users"]

    api.patch_widget = AsyncMock(side_effect=make_quota_error("widget_updates", used=50, limit=50))
    with caplog.at_level(logging.DEBUG, logger="custom_components.pushward.widget_manager"):
        hass.states.async_set("sensor.users", "43")
        await hass.async_block_till_done()

    assert api.patch_widget.await_count == 1
    assert "ha-users" not in manager._failed_slugs
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # The cached payload still holds the last value that reached the server.
    assert tracked.last_content["value"] == 42.0

    api.patch_widget = AsyncMock()
    async_dispatcher_send(hass, quota_released_signal(entry.entry_id), "widget_updates")
    await hass.async_block_till_done()

    assert api.patch_widget.await_count == 1
    assert api.patch_widget.call_args.args[1]["content"]["value"] == 43.0
    await manager.async_stop()


async def test_quota_release_resends_nothing_when_content_unchanged(hass: HomeAssistant) -> None:
    api = _mock_api()
    entry = make_mock_entry("quota_entry")
    hass.states.async_set("sensor.users", "42")
    manager = WidgetManager(hass, api, [make_widget_config()], entry)
    await manager.async_start()
    # One live update after the create so the cached payload carries the trend field.
    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()
    api.reset_mock()

    async_dispatcher_send(hass, quota_released_signal(entry.entry_id), "widget_updates")
    async_dispatcher_send(hass, quota_released_signal(entry.entry_id), "live_activity_updates")
    await hass.async_block_till_done()

    api.patch_widget.assert_not_awaited()
    api.create_widget.assert_not_awaited()
    await manager.async_stop()


async def test_quota_release_subscription_dropped_on_stop(hass: HomeAssistant) -> None:
    api = _mock_api()
    entry = make_mock_entry("quota_entry")
    hass.states.async_set("sensor.users", "42")
    manager = WidgetManager(hass, api, [make_widget_config()], entry)
    await manager.async_start()
    await manager.async_reload([make_widget_config()])
    api.reset_mock()
    api.patch_widget = AsyncMock(side_effect=make_quota_error("widget_updates"))
    hass.states.async_set("sensor.users", "43")
    await hass.async_block_till_done()
    api.patch_widget = AsyncMock()

    async_dispatcher_send(hass, quota_released_signal(entry.entry_id), "widget_updates")
    await hass.async_block_till_done()
    assert api.patch_widget.await_count == 1

    await manager.async_stop()
    hass.states.async_set("sensor.users", "44")
    async_dispatcher_send(hass, quota_released_signal(entry.entry_id), "widget_updates")
    await hass.async_block_till_done()
    assert api.patch_widget.await_count == 1


# ----- heatmap -----


def _midnight(day: date) -> float:
    """The epoch the recorder's "day" period gives a local day's row as its start."""
    return dt_util.start_of_local_day(day).timestamp()


async def _daily(hass: HomeAssistant, rows: list[dict], days: int, stat_type: str = "mean"):
    """Run async_daily_values against mocked statistics rows; return (cells, the recorder call)."""
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={"sensor.energy": rows})
    with patch("homeassistant.helpers.recorder.get_instance", return_value=instance):
        cells = await async_daily_values(hass, "sensor.energy", days, stat_type)
    return cells, instance.async_add_executor_job.await_args.args[0]


@pytest.mark.parametrize("stat_type", ["change", "mean", "max", "min"])
async def test_daily_values_read_the_chosen_statistic_per_local_day(hass: HomeAssistant, freezer, stat_type) -> None:
    """One cell per local day ending today, from the day's row of the chosen type; a day
    with no row is null. Warsaw is UTC+1 here, so each row starts at 23:00 UTC the day
    before and must still land on its own local date."""
    await hass.config.async_set_time_zone("Europe/Warsaw")
    freezer.move_to("2026-11-11T14:00:00+00:00")
    today = date(2026, 11, 11)
    rows = [
        {"start": _midnight(today - timedelta(days=n)), "change": 10.0 + n, "mean": 20.0 + n, "max": 30.0 + n, "min": n}
        for n in range(7)
        if n != 3  # no statistics for 2026-11-08
    ]
    cells, call = await _daily(hass, rows, 7, stat_type)

    base = {"change": 10.0, "mean": 20.0, "max": 30.0, "min": 0.0}[stat_type]
    assert cells == [base + 6, base + 5, base + 4, None, base + 2, base + 1, base]
    _hass, start, end, ids, period, units, types = call.args
    assert start == dt_util.start_of_local_day(date(2026, 11, 5))
    assert start.isoformat() == "2026-11-05T00:00:00+01:00"
    assert (end, ids, period, units, types) == (None, {"sensor.energy"}, "day", None, {stat_type})


async def test_daily_values_skip_null_and_non_finite_statistics(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_set_time_zone("Europe/Warsaw")
    freezer.move_to("2026-11-11T14:00:00+00:00")
    today = date(2026, 11, 11)
    rows = [
        {"start": _midnight(today), "mean": None},
        {"start": _midnight(today - timedelta(days=1)), "mean": float("nan")},
        {"start": _midnight(today - timedelta(days=2)), "mean": 4.5},
    ]
    cells, _ = await _daily(hass, rows, 7)
    assert cells == [None, None, None, None, 4.5, None, None]


@pytest.mark.parametrize(
    ("now", "first_day"),
    [
        # Clocks went back on 2026-10-25 (a 25-hour day).
        pytest.param("2026-10-27T10:00:00+00:00", date(2026, 10, 21), id="autumn"),
        # Clocks go forward on 2027-03-28 (a 23-hour day).
        pytest.param("2027-03-30T10:00:00+00:00", date(2027, 3, 24), id="spring"),
    ],
)
async def test_daily_values_keep_one_cell_per_day_across_dst(hass: HomeAssistant, freezer, now, first_day) -> None:
    """The DST day is shorter or longer than 24 h; every row still lands on its own date."""
    await hass.config.async_set_time_zone("Europe/Warsaw")
    freezer.move_to(now)
    rows = [{"start": _midnight(first_day + timedelta(days=n)), "mean": float(n)} for n in range(7)]
    cells, call = await _daily(hass, rows, 7)

    assert cells == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert call.args[1] == dt_util.start_of_local_day(first_day)


async def test_daily_values_cover_22_weeks(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to("2026-11-11T03:00:00+00:00")  # still 2026-11-10 in New York
    today = date(2026, 11, 10)
    rows = [{"start": _midnight(today), "mean": 1.0}, {"start": _midnight(today - timedelta(days=153)), "mean": 2.0}]
    cells, call = await _daily(hass, rows, 154)

    assert len(cells) == 154
    assert cells[0] == 2.0 and cells[-1] == 1.0
    assert sum(cell is not None for cell in cells) == 2
    assert call.args[1] == dt_util.start_of_local_day(today - timedelta(days=153))


async def test_daily_values_none_when_the_recorder_fails(hass: HomeAssistant) -> None:
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(side_effect=RuntimeError("database is locked"))
    with patch("homeassistant.helpers.recorder.get_instance", return_value=instance):
        assert await async_daily_values(hass, "sensor.energy", 7, "change") is None


def _heatmap_config(**overrides) -> dict:
    base = {
        CONF_WIDGET_TEMPLATE: WIDGET_TEMPLATE_HEATMAP,
        CONF_SLUG: "ha-energy",
        CONF_ENTITY_ID: "sensor.energy",
        CONF_MIN_VALUE: None,
        CONF_MAX_VALUE: None,
        CONF_HEATMAP_WEEKS: 1,
        CONF_HEATMAP_AGGREGATE: "change",
    }
    base.update(overrides)
    return make_widget_config(**base)


_WEEK = [1.0, None, 3.0, 4.0, 5.0, 6.0, 7.5]


async def test_heatmap_creates_from_daily_statistics(hass: HomeAssistant) -> None:
    api = _mock_api()
    hass.states.async_set("sensor.energy", "120.5", {"friendly_name": "Energy", "unit_of_measurement": "kWh"})
    with patch("custom_components.pushward.widget_manager.async_daily_values", AsyncMock(return_value=_WEEK)) as read:
        manager = WidgetManager(hass, api, [_heatmap_config()], make_mock_entry())
        await manager.async_start()

    read.assert_awaited_once_with(hass, "sensor.energy", 7, "change")
    content = api.create_widget.call_args.kwargs["content"]
    assert content["cells"] == _WEEK
    assert content["value"] == 7.5
    assert (content["label"], content["unit"]) == ("Energy", "kWh")
    assert "end_date" not in content
    assert_valid_widget_content(content, WIDGET_TEMPLATE_HEATMAP)
    await manager.async_stop()


async def test_heatmap_state_changes_never_read_the_recorder(hass: HomeAssistant, freezer) -> None:
    """Statistics only move at the hourly compile, so however many state changes arrive, and
    however far apart, they reuse the last read; the hourly read is what picks up the next hour."""
    await hass.config.async_set_time_zone("Europe/Warsaw")
    freezer.move_to("2026-11-12T07:10:00+01:00")
    api = _mock_api()
    hass.states.async_set("sensor.energy", "1")
    with patch(
        "custom_components.pushward.widget_manager.async_daily_values", AsyncMock(return_value=list(_WEEK))
    ) as read:
        manager = WidgetManager(hass, api, [_heatmap_config()], make_mock_entry())
        await manager.async_start()
        read.return_value = [*_WEEK[:-1], 9.0]
        for n in range(9):
            # Six minutes apart, up to 08:04: never two in the same five minutes.
            freezer.move_to(datetime.fromisoformat("2026-11-12T07:16:00+01:00") + timedelta(minutes=6 * n))
            hass.states.async_set("sensor.energy", str(2 + n))
            await hass.async_block_till_done()
        assert read.await_count == 1
        api.patch_widget.assert_not_awaited()

        refresh = datetime.fromisoformat("2026-11-12T08:05:00+01:00")
        freezer.move_to(refresh)
        async_fire_time_changed(hass, refresh)
        await hass.async_block_till_done()

    assert read.await_count == 2
    api.patch_widget.assert_awaited_once()
    assert api.patch_widget.call_args.args[1]["content"]["cells"][-1] == 9.0
    await manager.async_stop()


async def test_heatmap_reads_again_after_local_midnight(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_set_time_zone("Europe/Warsaw")
    freezer.move_to("2026-11-11T22:30:00+00:00")  # 23:30 in Warsaw, past the 23:05 read
    api = _mock_api()
    hass.states.async_set("sensor.energy", "1")
    with patch(
        "custom_components.pushward.widget_manager.async_daily_values", AsyncMock(return_value=list(_WEEK))
    ) as read:
        manager = WidgetManager(hass, api, [_heatmap_config()], make_mock_entry())
        await manager.async_start()

        # Midnight itself does nothing: yesterday's last hour is not compiled yet.
        midnight = datetime.fromisoformat("2026-11-12T00:00:30+01:00")
        freezer.move_to(midnight)
        async_fire_time_changed(hass, midnight)
        await hass.async_block_till_done()
        assert read.await_count == 1

        read.return_value = [*_WEEK[1:], None]
        refresh = datetime.fromisoformat("2026-11-12T00:05:00+01:00")
        freezer.move_to(refresh)
        async_fire_time_changed(hass, refresh)
        await hass.async_block_till_done()

    assert read.await_count == 2
    content = api.patch_widget.call_args.args[1]["content"]
    assert content["cells"] == [*_WEEK[1:], None]
    assert content["value"] is None  # today has no statistics yet
    assert_valid_widget_content(content, WIDGET_TEMPLATE_HEATMAP)
    await manager.async_stop()


async def test_heatmap_never_reuses_a_read_from_yesterday(hass: HomeAssistant, freezer) -> None:
    """A read taken before midnight ends on yesterday, so the first state change after midnight
    reads again, before the 00:05 read."""
    await hass.config.async_set_time_zone("Europe/Warsaw")
    freezer.move_to("2026-11-11T22:58:00+00:00")  # 23:58 in Warsaw
    api = _mock_api()
    hass.states.async_set("sensor.energy", "1")
    with patch(
        "custom_components.pushward.widget_manager.async_daily_values", AsyncMock(return_value=list(_WEEK))
    ) as read:
        manager = WidgetManager(hass, api, [_heatmap_config()], make_mock_entry())
        await manager.async_start()
        assert read.await_count == 1

        read.return_value = [*_WEEK[1:], None]
        freezer.move_to("2026-11-11T23:02:00+00:00")  # 00:02, four minutes after the read
        hass.states.async_set("sensor.energy", "2")
        await hass.async_block_till_done()

    assert read.await_count == 2
    assert api.patch_widget.call_args.args[1]["content"]["cells"] == [*_WEEK[1:], None]
    await manager.async_stop()


async def test_heatmap_reads_each_hour_once_compiled_without_a_state_change(hass: HomeAssistant, freezer) -> None:
    """A meter that changed at 07:10 and then sits still: the 07:00-08:00 hour is compiled
    after 08:00, and the 08:05 read sends it without waiting for another state change."""
    await hass.config.async_set_time_zone("Europe/Warsaw")
    freezer.move_to("2026-11-12T07:10:00+01:00")
    api = _mock_api()
    hass.states.async_set("sensor.energy", "1")
    with patch(
        "custom_components.pushward.widget_manager.async_daily_values", AsyncMock(return_value=list(_WEEK))
    ) as read:
        manager = WidgetManager(hass, api, [_heatmap_config()], make_mock_entry())
        await manager.async_start()

        # Before the read minute nothing happens.
        early = datetime.fromisoformat("2026-11-12T08:00:30+01:00")
        freezer.move_to(early)
        async_fire_time_changed(hass, early)
        await hass.async_block_till_done()
        assert read.await_count == 1

        read.return_value = [*_WEEK[:-1], 9.0]
        refresh = datetime.fromisoformat("2026-11-12T08:05:00+01:00")
        freezer.move_to(refresh)
        async_fire_time_changed(hass, refresh)
        await hass.async_block_till_done()

        # The next hour reads again; unchanged values send nothing.
        later = datetime.fromisoformat("2026-11-12T09:05:00+01:00")
        freezer.move_to(later)
        async_fire_time_changed(hass, later)
        await hass.async_block_till_done()

    assert read.await_count == 3
    api.patch_widget.assert_awaited_once()
    content = api.patch_widget.call_args.args[1]["content"]
    assert (content["cells"][-1], content["value"]) == (9.0, 9.0)
    await manager.async_stop()


async def test_heatmap_without_history_sends_nothing_and_warns_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    api = _mock_api()
    hass.states.async_set("sensor.energy", "4242.5")

    def warnings() -> list[str]:
        return [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "ha-energy" in r.getMessage()
        ]

    with patch(
        "custom_components.pushward.widget_manager.async_daily_values", AsyncMock(return_value=[None] * 7)
    ) as read:
        manager = WidgetManager(hass, api, [_heatmap_config()], make_mock_entry())
        await manager.async_start()
        tracked = manager._tracked["ha-energy"]
        for n in range(3):
            tracked.cells_read_day = None
            hass.states.async_set("sensor.energy", f"{4243 + n}.5")
            await hass.async_block_till_done()
        assert read.await_count == 4
        api.create_widget.assert_not_awaited()
        assert len(warnings()) == 1
        # The entity's state values stay out of the log.
        assert not any(f"{4242 + n}.5" in caplog.text for n in range(4))

        # History comes back: the widget is created and the warning re-arms.
        read.return_value = list(_WEEK)
        tracked.cells_read_day = None
        hass.states.async_set("sensor.energy", "4246.5")
        await hass.async_block_till_done()
        api.create_widget.assert_awaited_once()

        # It goes away again later: nothing is sent, and it is reported once more.
        read.return_value = None
        tracked.cells_read_day = None
        hass.states.async_set("sensor.energy", "4247.5")
        await hass.async_block_till_done()

    api.patch_widget.assert_not_awaited()
    assert len(warnings()) == 2
    await manager.async_stop()


async def test_heatmap_manual_refresh_reads_again(hass: HomeAssistant) -> None:
    api = _mock_api()
    hass.states.async_set("sensor.energy", "1")
    with patch(
        "custom_components.pushward.widget_manager.async_daily_values", AsyncMock(return_value=list(_WEEK))
    ) as read:
        manager = WidgetManager(hass, api, [_heatmap_config(**{CONF_HEATMAP_WEEKS: 22})], make_mock_entry())
        await manager.async_start()
        await manager.async_refresh(slug="ha-energy")

    assert read.await_count == 2
    assert read.await_args.args[2] == 154
    api.patch_widget.assert_awaited_once()
    await manager.async_stop()
