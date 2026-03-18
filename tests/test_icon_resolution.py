"""Integration tests for icon resolution using real HA entity registry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.pushward.activity_manager import ActivityManager
from custom_components.pushward.content_mapper import map_content

from .conftest import make_entity_config as _entity_config


def _register_entity(
    registry: er.EntityRegistry,
    domain: str = "sensor",
    unique_id: str = "test_1",
    *,
    suggested_object_id: str = "test",
    original_icon: str | None = None,
) -> er.RegistryEntry:
    """Register an entity in the real HA entity registry."""
    return registry.async_get_or_create(
        domain,
        "test",
        unique_id,
        suggested_object_id=suggested_object_id,
        original_icon=original_icon,
    )


# -------------------------------------------------------------------
# 1. state.attributes["icon"] is picked up (step 3)
# -------------------------------------------------------------------
async def test_state_attr_icon_is_used(hass: HomeAssistant) -> None:
    """Icon from state.attributes['icon'] is resolved (step 3)."""
    registry = er.async_get(hass)
    entry = _register_entity(registry, unique_id="attr_icon")

    hass.states.async_set(entry.entity_id, "42", {"icon": "mdi:flask"})
    state = hass.states.get(entry.entity_id)

    config = _entity_config(entity_id=entry.entity_id, icon="")
    content = map_content(state, config)

    assert content["icon"] == "mdi:flask"


# -------------------------------------------------------------------
# 2. Registry original_icon is picked up (step 4)
# -------------------------------------------------------------------
async def test_registry_original_icon(hass: HomeAssistant) -> None:
    """Platform-provided original_icon is used when no state attr icon."""
    registry = er.async_get(hass)
    entry = _register_entity(
        registry,
        unique_id="orig_icon",
        original_icon="mdi:thermometer",
    )

    hass.states.async_set(entry.entity_id, "72")
    state = hass.states.get(entry.entity_id)

    config = _entity_config(entity_id=entry.entity_id, icon="")
    content = map_content(state, config, registry_icon=entry.original_icon)

    assert content["icon"] == "mdi:thermometer"


# -------------------------------------------------------------------
# 3. Registry user-override icon beats original_icon (step 4)
# -------------------------------------------------------------------
async def test_registry_user_override_beats_original(hass: HomeAssistant) -> None:
    """User-customized icon in registry takes precedence over original_icon."""
    registry = er.async_get(hass)
    entry = _register_entity(
        registry,
        unique_id="override_icon",
        original_icon="mdi:thermometer",
    )
    registry.async_update_entity(entry.entity_id, icon="mdi:snowflake")
    entry = registry.async_get(entry.entity_id)

    hass.states.async_set(entry.entity_id, "32")
    state = hass.states.get(entry.entity_id)

    # Simulate what _get_registry_icon returns: entry.icon or entry.original_icon
    registry_icon = entry.icon or entry.original_icon or None

    config = _entity_config(entity_id=entry.entity_id, icon="")
    content = map_content(state, config, registry_icon=registry_icon)

    assert content["icon"] == "mdi:snowflake"


# -------------------------------------------------------------------
# 4. state.attributes["icon"] beats registry icon (step 3 before step 4)
# -------------------------------------------------------------------
async def test_state_attr_icon_beats_registry(hass: HomeAssistant) -> None:
    """state.attributes['icon'] (step 3) takes priority over registry icon (step 4)."""
    registry = er.async_get(hass)
    entry = _register_entity(
        registry,
        unique_id="attr_beats_reg",
        original_icon="mdi:thermometer",
    )
    registry.async_update_entity(entry.entity_id, icon="mdi:snowflake")

    hass.states.async_set(entry.entity_id, "50", {"icon": "mdi:fire"})
    state = hass.states.get(entry.entity_id)

    registry_icon = "mdi:snowflake"
    config = _entity_config(entity_id=entry.entity_id, icon="")
    content = map_content(state, config, registry_icon=registry_icon)

    assert content["icon"] == "mdi:fire"


# -------------------------------------------------------------------
# 5. Device class icon resolves (step 5)
# -------------------------------------------------------------------
async def test_device_class_icon(hass: HomeAssistant) -> None:
    """Device class icon is resolved when no state/registry icon exists."""
    registry = er.async_get(hass)
    entry = _register_entity(
        registry,
        unique_id="dc_icon",
        suggested_object_id="temp_dc",
    )

    hass.states.async_set(entry.entity_id, "72", {"device_class": "temperature"})
    state = hass.states.get(entry.entity_id)

    config = _entity_config(entity_id=entry.entity_id, icon="")
    content = map_content(state, config, registry_icon=None)

    assert content["icon"] == "mdi:thermometer"


# -------------------------------------------------------------------
# 6. Domain default fallback (step 6)
# -------------------------------------------------------------------
async def test_domain_default_fallback(hass: HomeAssistant) -> None:
    """Domain default icon is used when nothing else matches."""
    registry = er.async_get(hass)
    entry = _register_entity(
        registry,
        domain="light",
        unique_id="domain_fallback",
        suggested_object_id="fallback",
    )

    hass.states.async_set(entry.entity_id, "on")
    state = hass.states.get(entry.entity_id)

    config = _entity_config(entity_id=entry.entity_id, icon="")
    content = map_content(state, config, registry_icon=None)

    assert content["icon"] == "mdi:lightbulb"


# -------------------------------------------------------------------
# 7. Full ActivityManager flow — _get_registry_icon with real registry
# -------------------------------------------------------------------
async def test_activity_manager_resolves_registry_icon(
    hass: HomeAssistant,
) -> None:
    """ActivityManager._get_registry_icon reads from real entity registry."""
    registry = er.async_get(hass)
    entry = _register_entity(
        registry,
        domain="binary_sensor",
        unique_id="manager_icon",
        suggested_object_id="washer_icon",
        original_icon="mdi:washing-machine",
    )

    api = AsyncMock()
    api.create_activity = AsyncMock()
    api.update_activity = AsyncMock()
    mock_entry = MagicMock()
    mock_entry.async_start_reauth = MagicMock()

    config = _entity_config(
        entity_id=entry.entity_id,
        slug="ha-washer-icon",
        icon="",  # no static icon — force registry lookup
    )
    manager = ActivityManager(hass, api, [config], mock_entry)

    hass.states.async_set(entry.entity_id, "on")
    await manager.async_start()
    await hass.async_block_till_done()

    api.update_activity.assert_awaited_once()
    content = api.update_activity.call_args[0][2]
    assert content["icon"] == "mdi:washing-machine"

    await manager.async_stop()


async def test_activity_manager_no_icon_anywhere(
    hass: HomeAssistant,
) -> None:
    """ActivityManager falls back to domain default when no icon exists anywhere.

    Simulates real-world entities like ESPHome light.led_column where:
    - registry icon = None, original_icon = None
    - state.attributes["icon"] absent
    - no device_class
    """
    registry = er.async_get(hass)
    entry = _register_entity(
        registry,
        domain="light",
        unique_id="manager_no_icon",
        suggested_object_id="no_icon_light",
        # No original_icon — like most ESPHome/modern entities
    )

    api = AsyncMock()
    api.create_activity = AsyncMock()
    api.update_activity = AsyncMock()
    mock_entry = MagicMock()
    mock_entry.async_start_reauth = MagicMock()

    config = _entity_config(
        entity_id=entry.entity_id,
        slug="ha-no-icon-light",
        icon="",  # user didn't choose an icon
    )
    manager = ActivityManager(hass, api, [config], mock_entry)

    hass.states.async_set(entry.entity_id, "on")
    await manager.async_start()
    await hass.async_block_till_done()

    api.update_activity.assert_awaited_once()
    content = api.update_activity.call_args[0][2]
    assert content["icon"] == "mdi:lightbulb"

    await manager.async_stop()


async def test_activity_manager_user_override_icon(
    hass: HomeAssistant,
) -> None:
    """ActivityManager picks up user-customized icon from registry."""
    registry = er.async_get(hass)
    entry = _register_entity(
        registry,
        domain="binary_sensor",
        unique_id="manager_override",
        suggested_object_id="washer_override",
        original_icon="mdi:washing-machine",
    )
    registry.async_update_entity(entry.entity_id, icon="mdi:dishwasher")

    api = AsyncMock()
    api.create_activity = AsyncMock()
    api.update_activity = AsyncMock()
    mock_entry = MagicMock()
    mock_entry.async_start_reauth = MagicMock()

    config = _entity_config(
        entity_id=entry.entity_id,
        slug="ha-washer-override",
        icon="",
    )
    manager = ActivityManager(hass, api, [config], mock_entry)

    hass.states.async_set(entry.entity_id, "on")
    await manager.async_start()
    await hass.async_block_till_done()

    api.update_activity.assert_awaited_once()
    content = api.update_activity.call_args[0][2]
    assert content["icon"] == "mdi:dishwasher"

    await manager.async_stop()
