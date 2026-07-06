"""Tests for the PushWard widget manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from custom_components.pushward.api import (
    PushWardApiError,
    PushWardAuthError,
    PushWardNotFoundError,
    PushWardWidgetPermissionError,
)
from custom_components.pushward.const import (
    CONF_ENTITY_ID,
    CONF_SLUG,
    CONF_STAT_ROWS,
    CONF_WIDGET_POLL_INTERVAL,
    CONF_WIDGET_TEMPLATE,
    CONF_WIDGET_TRIGGER_MODE,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TRIGGER_POLL,
)
from custom_components.pushward.widget_manager import (
    _WIDGET_PERMISSION_NOTIFICATION,
    WidgetManager,
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
