"""Tests for the PushWard widget manager."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
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
    CONF_HISTORY_PERIOD,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_SLUG,
    CONF_STAT_ROWS,
    CONF_SUBTITLE_TIMER_ENTITY,
    CONF_WIDGET_POLL_INTERVAL,
    CONF_WIDGET_STALE_AFTER,
    CONF_WIDGET_TEMPLATE,
    CONF_WIDGET_TRIGGER_MODE,
    WIDGET_GROUP_TEMPLATES,
    WIDGET_STALE_AFTER_MIN,
    WIDGET_TEMPLATE_BATTERY,
    WIDGET_TEMPLATE_FLOW,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_TREND,
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TRIGGER_POLL,
)
from custom_components.pushward.widget_manager import (
    _GROUP_ROW_SOURCES,
    _WIDGET_PERMISSION_NOTIFICATION,
    WidgetManager,
    _entity_ids_for_widget,
)

from .conftest import make_widget_config


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

    manager = WidgetManager(hass, api, [kept, removed], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [one, two], _mock_entry())
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
    manager = WidgetManager(hass, api, [config], _mock_entry())
    await manager.async_start()

    assert manager.slug_for_entity("sensor.users") == "ha-users"
    assert manager.slug_for_entity("sensor.nope") is None
    assert manager.slug_for_entity(None) is None

    await manager.async_stop()


def _mock_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.async_start_reauth = MagicMock()
    return entry


async def test_initial_post_on_start(hass: HomeAssistant) -> None:
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
    await manager.async_start()

    api.create_widget.assert_awaited_once()
    assert api.create_widget.call_args.kwargs["push_throttle"] == 30

    await manager.async_stop()


async def test_event_mode_omits_push_throttle(hass: HomeAssistant) -> None:
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], _mock_entry())
    await manager.async_start()

    assert api.create_widget.call_args.kwargs["push_throttle"] is None

    await manager.async_stop()


async def test_manual_refresh_force_patches_unchanged(hass: HomeAssistant) -> None:
    """async_refresh bypasses the diff cache."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], _mock_entry())
    await manager.async_start()
    api.reset_mock()

    await manager.async_refresh(slug="ha-users")
    assert api.patch_widget.await_count == 1

    await manager.async_stop()


async def test_manual_refresh_by_entity_id(hass: HomeAssistant) -> None:
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], _mock_entry())
    await manager.async_start()
    api.reset_mock()

    await manager.async_refresh(entity_id="sensor.users")
    assert api.patch_widget.await_count == 1

    await manager.async_stop()


async def test_manual_refresh_unknown_raises(hass: HomeAssistant) -> None:
    api = _mock_api()
    manager = WidgetManager(hass, api, [], _mock_entry())
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
        manager = WidgetManager(hass, api, [config], _mock_entry())
        await manager.async_start()
        assert create_notif.called
        assert create_notif.call_args.kwargs["notification_id"] == _WIDGET_PERMISSION_NOTIFICATION

    await manager.async_stop()


async def test_auth_error_triggers_reauth(hass: HomeAssistant) -> None:
    """401 / PushWardAuthError calls entry.async_start_reauth exactly once."""
    api = _mock_api()
    api.create_widget = AsyncMock(side_effect=PushWardAuthError("invalid key", status_code=401))
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    entry = _mock_entry()
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

    manager = WidgetManager(hass, api, [config_a], _mock_entry())
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
    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
    await manager.async_start()
    api.create_widget.assert_awaited_once()
    await manager.async_stop()

    # Same entry_id → same Store key. New manager should load cache and find
    # the content unchanged, so no PATCH on identical state.
    api2 = _mock_api()
    manager2 = WidgetManager(hass, api2, [config], _mock_entry())
    await manager2.async_start()
    api2.create_widget.assert_awaited_once()  # initial sync is idempotent upsert
    api2.reset_mock()

    hass.states.async_set("sensor.users", "42")
    await hass.async_block_till_done()
    api2.patch_widget.assert_not_called()

    await manager2.async_stop()


async def test_patch_404_recreate_rearms_on_redeletion(hass: HomeAssistant) -> None:
    """A recreate that sticks re-arms the guard so a later re-deletion self-heals again.

    Regression for the flag never resetting on the recreate path: without the reset a
    second out-of-band deletion (no clean PATCH between) would be skipped forever and the
    widget would stay dead.
    """
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())

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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [make_widget_config(**{CONF_WIDGET_STALE_AFTER: 5})], _mock_entry())
    await manager.async_start()
    assert api.create_widget.call_args.kwargs["stale_after"] == WIDGET_STALE_AFTER_MIN
    await manager.async_stop()


async def test_patch_clears_stale_after_when_unset(hass: HomeAssistant) -> None:
    """An explicit null on every PATCH converges a row created before the field existed."""
    api = _mock_api()
    config = make_widget_config()
    hass.states.async_set("sensor.users", "42")

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    plain = WidgetManager(hass, api, [make_widget_config()], _mock_entry())
    await plain.async_start()
    assert plain._tracked["ha-users"].unsub_heartbeat is None
    await plain.async_stop()

    ticking = WidgetManager(hass, api, [make_widget_config(**{CONF_WIDGET_STALE_AFTER: 3600})], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [config], _mock_entry())
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

    manager = WidgetManager(hass, api, [_trend_config()], _mock_entry())
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

    manager = WidgetManager(hass, api, [_trend_config()], _mock_entry())
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

    manager = WidgetManager(hass, api, [_trend_config()], _mock_entry())
    await manager.async_start()
    hass.states.async_set("sensor.power", "120")
    await hass.async_block_till_done()
    cached = manager._serialize_cache()["widgets"]["ha-trend"]
    assert cached["points"] == [[pytest.approx(ts), v] for ts, v in manager._tracked["ha-trend"].points_buffer]
    await manager.async_stop()

    manager2 = WidgetManager(hass, api, [_trend_config()], _mock_entry())
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
        manager = WidgetManager(hass, api, [config], _mock_entry())
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
        manager = WidgetManager(hass, api, [config], _mock_entry())
        await manager.async_start()
        hass.states.async_set("sensor.power", "120")
        await hass.async_block_till_done()
        await manager.async_stop()

        manager2 = WidgetManager(hass, api, [config], _mock_entry())
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
    manager = WidgetManager(hass, api, [], _mock_entry())
    for template in (WIDGET_TEMPLATE_BATTERY, WIDGET_TEMPLATE_FLOW, WIDGET_TEMPLATE_STAT_LIST):
        assert manager._lookup_registry_icon(make_widget_config(**{CONF_WIDGET_TEMPLATE: template})) is None


async def test_group_row_sources_cover_every_group_template(hass: HomeAssistant) -> None:
    """Drift guard: a template added to WIDGET_GROUP_TEMPLATES needs a rows key here.

    Without it _entity_ids_for_widget silently falls through to the single-entity
    branch and the widget subscribes to nothing.
    """
    assert set(_GROUP_ROW_SOURCES) == set(WIDGET_GROUP_TEMPLATES)
