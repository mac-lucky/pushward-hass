"""Activity lifecycle manager — tracks HA entities as PushWard Live Activities."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from .api import PushWardApiClient, PushWardApiError, PushWardAuthError
from .const import (
    CONF_ACTIVITY_NAME,
    CONF_END_STATES,
    CONF_ENDED_TTL,
    CONF_ENTITY_ID,
    CONF_PRIORITY,
    CONF_SLUG,
    CONF_STALE_TTL,
    CONF_START_STATES,
    CONF_UPDATE_INTERVAL,
    END_DELAY_SECONDS,
)
from .content_mapper import map_completion_content, map_content

_LOGGER = logging.getLogger(__name__)


@dataclass
class TrackedEntity:
    """State for a single tracked entity."""

    config: dict
    is_active: bool = False
    last_content: dict | None = None
    registry_icon: str | None = None
    unsub_state: Callable | None = None
    flush_unsub: Callable | None = None
    end_task: asyncio.Task | None = field(default=None, repr=False)
    generation: int = 0
    last_sent_at: float = 0.0


class ActivityManager:
    """Manages PushWard activities driven by HA entity state changes."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: PushWardApiClient,
        entities: list[dict],
        entry: ConfigEntry,
    ) -> None:
        self._hass = hass
        self._api = api
        self._entities = entities
        self._entry = entry
        self._tracked: dict[str, TrackedEntity] = {}
        self._reauth_triggered = False

    def _get_registry_icon(self, entity_id: str) -> str | None:
        """Look up entity icon from the HA entity registry."""
        registry = er.async_get(self._hass)
        entry = registry.async_get(entity_id)
        if entry is None:
            return None
        return entry.icon or entry.original_icon or None

    def _trigger_reauth(self) -> None:
        """Trigger reauth flow once on auth failure."""
        if not self._reauth_triggered:
            self._reauth_triggered = True
            _LOGGER.warning("PushWard auth failed — triggering reauthentication")
            self._entry.async_start_reauth(self._hass)

    async def async_start(self) -> None:
        """Subscribe to state changes and resume any active entities."""
        for entity_cfg in self._entities:
            entity_id = entity_cfg[CONF_ENTITY_ID]
            tracked = TrackedEntity(config=entity_cfg)

            tracked.unsub_state = async_track_state_change_event(
                self._hass,
                entity_id,
                partial(self._async_on_state_change, entity_id),
            )
            self._tracked[entity_id] = tracked

            # Resume if entity is currently in a start state (HA restart)
            current = self._hass.states.get(entity_id)
            if current and current.state in entity_cfg.get(CONF_START_STATES, []):
                await self._start_activity(entity_id)

    async def async_stop(self) -> None:
        """End all active activities and unsubscribe listeners."""
        for _entity_id, tracked in self._tracked.items():
            if tracked.end_task and not tracked.end_task.done():
                tracked.end_task.cancel()

            if tracked.flush_unsub:
                tracked.flush_unsub()

            if tracked.unsub_state:
                tracked.unsub_state()

            if tracked.is_active:
                slug = tracked.config[CONF_SLUG]
                try:
                    content = map_completion_content(tracked.config, tracked.last_content)
                    await self._api.update_activity(slug, "ENDED", content)
                except (PushWardApiError, aiohttp.ClientError):
                    _LOGGER.warning("Failed to end activity %s during shutdown", slug, exc_info=True)

        self._tracked.clear()

    async def async_reload(self, new_entities: list[dict]) -> None:
        """Reload with new entity configuration."""
        await self.async_stop()
        self._entities = new_entities
        await self.async_start()

    @callback
    def _async_on_state_change(self, entity_id: str, event: Event) -> None:
        """Handle an HA state change event."""
        new_state: State | None = event.data.get("new_state")
        if new_state is None or new_state.state in ("unavailable", "unknown"):
            return

        tracked = self._tracked.get(entity_id)
        if tracked is None:
            return

        config = tracked.config
        start_states = config.get(CONF_START_STATES, [])
        end_states = config.get(CONF_END_STATES, [])

        if new_state.state in start_states and not tracked.is_active:
            self._hass.async_create_task(self._start_activity(entity_id))
        elif new_state.state in end_states and tracked.is_active:
            self._schedule_end(entity_id)
        elif tracked.is_active:
            self._schedule_throttled_update(entity_id)

    async def _start_activity(self, entity_id: str) -> None:
        """Create and start a PushWard Live Activity."""
        tracked = self._tracked[entity_id]
        config = tracked.config
        slug = config[CONF_SLUG]

        # Cancel any pending end (handles rapid on/off)
        if tracked.end_task and not tracked.end_task.done():
            tracked.end_task.cancel()
            tracked.end_task = None

        tracked.generation += 1

        try:
            # Resolve activity name: configured name > friendly name > entity_id
            name = config.get(CONF_ACTIVITY_NAME) or ""
            if not name:
                current_state = self._hass.states.get(entity_id)
                name = current_state.attributes.get("friendly_name", entity_id) if current_state else entity_id

            await self._api.create_activity(
                slug,
                name,
                config.get(CONF_PRIORITY, 1),
                ended_ttl=config.get(CONF_ENDED_TTL),
                stale_ttl=config.get(CONF_STALE_TTL),
            )

            current_state = self._hass.states.get(entity_id)
            if current_state is None:
                return

            tracked.registry_icon = self._get_registry_icon(entity_id)
            content = map_content(current_state, config, registry_icon=tracked.registry_icon)
            _LOGGER.debug(
                "Activity %s icon resolution: state_attr=%s, registry=%s, resolved=%s",
                slug,
                current_state.attributes.get("icon"),
                tracked.registry_icon,
                content.get("icon"),
            )
            await self._api.update_activity(slug, "ONGOING", content)

            tracked.is_active = True
            tracked.last_content = content
            tracked.last_sent_at = time.monotonic()
        except PushWardAuthError:
            self._trigger_reauth()
        except (PushWardApiError, aiohttp.ClientError):
            _LOGGER.warning("Failed to start activity for %s", entity_id, exc_info=True)

    @callback
    def _schedule_throttled_update(self, entity_id: str) -> None:
        """Rate-limited update: send immediately if cooldown expired, else schedule."""
        tracked = self._tracked[entity_id]
        interval = tracked.config.get(CONF_UPDATE_INTERVAL, 5)
        now = time.monotonic()
        elapsed = now - tracked.last_sent_at

        if elapsed >= interval:
            self._hass.async_create_task(self._send_update(entity_id))
        elif tracked.flush_unsub is None:
            delay = interval - elapsed
            tracked.flush_unsub = async_call_later(
                self._hass,
                delay,
                partial(self._flush_update, entity_id),
            )

    async def _send_update(self, entity_id: str) -> None:
        """Send the latest content to PushWard."""
        tracked = self._tracked.get(entity_id)
        if tracked is None or not tracked.is_active:
            return

        current_state = self._hass.states.get(entity_id)
        if current_state is None:
            return

        content = map_content(current_state, tracked.config, registry_icon=tracked.registry_icon)
        if content == tracked.last_content:
            return

        slug = tracked.config[CONF_SLUG]
        try:
            await self._api.update_activity(slug, "ONGOING", content)
            tracked.last_content = content
            tracked.last_sent_at = time.monotonic()
        except PushWardAuthError:
            self._trigger_reauth()
        except (PushWardApiError, aiohttp.ClientError):
            _LOGGER.warning("Failed to update activity %s", slug, exc_info=True)

    @callback
    def _flush_update(self, entity_id: str, _now: datetime | None = None) -> None:
        """Fire a pending throttled update."""
        tracked = self._tracked.get(entity_id)
        if tracked is None:
            return
        tracked.flush_unsub = None
        if tracked.is_active:
            self._hass.async_create_task(self._send_update(entity_id))

    @callback
    def _schedule_end(self, entity_id: str) -> None:
        """Schedule a two-phase activity end."""
        tracked = self._tracked[entity_id]
        if tracked.end_task and not tracked.end_task.done():
            tracked.end_task.cancel()
        tracked.end_task = self._hass.async_create_task(self._async_end_activity(entity_id))

    async def _async_end_activity(self, entity_id: str) -> None:
        """Two-phase end: show completion, wait, then dismiss."""
        tracked = self._tracked[entity_id]
        config = tracked.config
        slug = config[CONF_SLUG]
        gen_at_start = tracked.generation

        try:
            # Phase 1: show completion content (preserves last progress/subtitle)
            completion = map_completion_content(config, tracked.last_content)
            await self._api.update_activity(slug, "ONGOING", completion)

            # Wait for user to see the completion state
            await asyncio.sleep(END_DELAY_SECONDS)

            # Abort if a new start happened during the sleep
            if tracked.generation != gen_at_start:
                return

            # Phase 2: end the activity
            await self._api.update_activity(slug, "ENDED", completion)
        except asyncio.CancelledError:
            raise
        except PushWardAuthError:
            self._trigger_reauth()
        except (PushWardApiError, aiohttp.ClientError):
            _LOGGER.warning("Failed to end activity %s", slug, exc_info=True)
        finally:
            task = asyncio.current_task()
            if task is None or not task.cancelled():
                tracked.is_active = False
                tracked.last_content = None
                if tracked.flush_unsub:
                    tracked.flush_unsub()
                    tracked.flush_unsub = None
