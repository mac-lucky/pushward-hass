"""Activity lifecycle manager — tracks HA entities as PushWard Live Activities."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Any, Callable

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .api import PushWardApiClient
from .const import (
    CONF_END_STATES,
    CONF_ENTITY_ID,
    CONF_SLUG,
    CONF_ACTIVITY_NAME,
    CONF_PRIORITY,
    CONF_START_STATES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ENDED_TTL,
    DEFAULT_STALE_TTL,
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
    unsub_state: Callable | None = None
    unsub_timer: Callable | None = None
    end_task: asyncio.Task | None = field(default=None, repr=False)


class ActivityManager:
    """Manages PushWard activities driven by HA entity state changes."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: PushWardApiClient,
        entities: list[dict],
    ) -> None:
        self._hass = hass
        self._api = api
        self._entities = entities
        self._tracked: dict[str, TrackedEntity] = {}

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
            if current and current.state in entity_cfg.get(
                CONF_START_STATES, []
            ):
                await self._start_activity(entity_id)

    async def async_stop(self) -> None:
        """End all active activities and unsubscribe listeners."""
        for entity_id, tracked in self._tracked.items():
            if tracked.end_task and not tracked.end_task.done():
                tracked.end_task.cancel()

            if tracked.unsub_timer:
                tracked.unsub_timer()

            if tracked.unsub_state:
                tracked.unsub_state()

            if tracked.is_active:
                slug = tracked.config[CONF_SLUG]
                try:
                    await self._api.update_activity(slug, "ENDED", {})
                except Exception:
                    _LOGGER.warning(
                        "Failed to end activity %s during shutdown", slug
                    )

        self._tracked.clear()

    async def async_reload(self, new_entities: list[dict]) -> None:
        """Reload with new entity configuration."""
        await self.async_stop()
        self._entities = new_entities
        await self.async_start()

    @callback
    def _async_on_state_change(
        self, entity_id: str, event: Event
    ) -> None:
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

    async def _start_activity(self, entity_id: str) -> None:
        """Create and start a PushWard Live Activity."""
        tracked = self._tracked[entity_id]
        config = tracked.config
        slug = config[CONF_SLUG]

        # Cancel any pending end (handles rapid on/off)
        if tracked.end_task and not tracked.end_task.done():
            tracked.end_task.cancel()
            tracked.end_task = None

        try:
            await self._api.create_activity(
                slug,
                config.get(CONF_ACTIVITY_NAME, entity_id),
                config.get(CONF_PRIORITY, 1),
                DEFAULT_ENDED_TTL,
                DEFAULT_STALE_TTL,
            )

            current_state = self._hass.states.get(entity_id)
            if current_state is None:
                return

            content = map_content(current_state, config)
            await self._api.update_activity(slug, "ONGOING", content)

            tracked.is_active = True
            tracked.last_content = content

            interval = timedelta(
                seconds=config.get(CONF_UPDATE_INTERVAL, 5)
            )
            tracked.unsub_timer = async_track_time_interval(
                self._hass,
                partial(self._async_periodic_update, entity_id),
                interval,
            )
        except Exception:
            _LOGGER.warning(
                "Failed to start activity for %s", entity_id, exc_info=True
            )

    async def _async_periodic_update(
        self, entity_id: str, _now: Any = None
    ) -> None:
        """Send periodic updates while the activity is active."""
        tracked = self._tracked.get(entity_id)
        if tracked is None or not tracked.is_active:
            return

        current_state = self._hass.states.get(entity_id)
        if current_state is None:
            return

        content = map_content(current_state, tracked.config)
        if content == tracked.last_content:
            return

        slug = tracked.config[CONF_SLUG]
        try:
            await self._api.update_activity(slug, "ONGOING", content)
            tracked.last_content = content
        except Exception:
            _LOGGER.warning(
                "Failed to update activity %s", slug, exc_info=True
            )

    def _schedule_end(self, entity_id: str) -> None:
        """Schedule a two-phase activity end."""
        tracked = self._tracked[entity_id]
        if tracked.end_task and not tracked.end_task.done():
            tracked.end_task.cancel()
        tracked.end_task = self._hass.async_create_task(
            self._async_end_activity(entity_id)
        )

    async def _async_end_activity(self, entity_id: str) -> None:
        """Two-phase end: show completion, wait, then dismiss."""
        tracked = self._tracked[entity_id]
        config = tracked.config
        slug = config[CONF_SLUG]

        try:
            # Phase 1: show completion content
            completion = map_completion_content(config)
            await self._api.update_activity(slug, "ONGOING", completion)

            # Wait for user to see the completion state
            await asyncio.sleep(END_DELAY_SECONDS)

            # Phase 2: end the activity
            await self._api.update_activity(slug, "ENDED", completion)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning(
                "Failed to end activity %s", slug, exc_info=True
            )
        finally:
            tracked.is_active = False
            tracked.last_content = None
            if tracked.unsub_timer:
                tracked.unsub_timer()
                tracked.unsub_timer = None
