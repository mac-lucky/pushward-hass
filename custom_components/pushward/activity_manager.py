"""Activity lifecycle manager — tracks HA entities as PushWard Live Activities."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Literal

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store

from .api import PushWardApiClient, PushWardApiError, PushWardAuthError, PushWardForbiddenError
from .const import (
    CONF_ACTIVITY_NAME,
    CONF_END_STATES,
    CONF_ENDED_TTL,
    CONF_ENTITY_ID,
    CONF_HISTORY_PERIOD,
    CONF_PRIORITY,
    CONF_SERIES,
    CONF_SLUG,
    CONF_SOUND,
    CONF_STALE_TTL,
    CONF_START_STATES,
    CONF_TEMPLATE,
    CONF_UPDATE_INTERVAL,
    CONF_VALUE_ATTRIBUTE,
    END_DELAY_SECONDS,
)
from .content_mapper import _get_timeline_values, map_completion_content, map_content

_HISTORY_BUFFER_MAX = 300
_HISTORY_STORAGE_VERSION = 1
_HISTORY_SAVE_DELAY_S = 30
_HISTORY_SAMPLES_KEY = "samples"

_ACTIVITY_LIMIT_NOTIFICATION_ID = "pushward_activity_limit"


def _forbidden_notification_id(slug: str) -> str:
    return f"pushward_forbidden_{slug}"


def history_storage_key(entry_id: str) -> str:
    """Return the .storage key used for this entry's persisted history buffers."""
    return f"pushward.history.{entry_id}"


def build_history_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Construct the Store used for persisted history buffers."""
    return Store(hass, _HISTORY_STORAGE_VERSION, history_storage_key(entry_id), atomic_writes=True)


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
    # (ts_seconds, {label: value}) — sampled on every state change. HA 2024.8+
    # strips light attributes from the recorder DB, so recorder queries can't
    # rebuild brightness history. Keep our own ring buffer instead.
    history_buffer: deque = field(default_factory=lambda: deque(maxlen=_HISTORY_BUFFER_MAX))


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
        # History ring buffers are persisted so that sparklines survive restarts
        # (HA 2024.8+ no longer stores light attributes in the recorder DB, so
        # we cannot reconstruct history from HA itself).
        self._history_store: Store = build_history_store(hass, entry.entry_id)

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

    @contextlib.asynccontextmanager
    async def _api_error_guard(self, slug: str, context: Literal["starting", "updating", "ending"]):
        """Translate PushWard API/network errors to user-facing notifications or reauth."""
        try:
            yield
        except PushWardAuthError:
            self._trigger_reauth()
        except PushWardForbiddenError as err:
            persistent_notification.async_create(
                self._hass,
                f"PushWard: {err}",
                title=f"PushWard — {slug}",
                notification_id=_forbidden_notification_id(slug),
            )
            _LOGGER.warning("PushWard 403 while %s %s: %s", context, slug, err)
        except PushWardApiError as err:
            if err.status_code == 409:
                persistent_notification.async_create(
                    self._hass,
                    "PushWard activity limit reached — delete unused activities or upgrade your subscription.",
                    title="PushWard — Activity Limit",
                    notification_id=_ACTIVITY_LIMIT_NOTIFICATION_ID,
                )
                _LOGGER.warning("Activity limit reached while %s %s", context, slug)
            else:
                _LOGGER.warning("PushWard API error while %s %s: %s", context, slug, err)
        except aiohttp.ClientError:
            _LOGGER.warning("PushWard network error while %s %s", context, slug, exc_info=True)

    @callback
    def _clear_forbidden_notification(self, slug: str) -> None:
        """Dismiss the forbidden-notification (if any) after a successful call."""
        persistent_notification.async_dismiss(self._hass, _forbidden_notification_id(slug))

    async def async_start(self) -> None:
        """Subscribe to state changes and resume any active entities."""
        persisted = await self._async_load_history()

        for entity_cfg in self._entities:
            entity_id = entity_cfg[CONF_ENTITY_ID]
            tracked = TrackedEntity(config=entity_cfg)

            # Rehydrate the buffer from disk so sparklines survive restarts.
            for ts, values in persisted.get(entity_id, ()):
                tracked.history_buffer.append((ts, values))

            tracked.unsub_state = async_track_state_change_event(
                self._hass,
                entity_id,
                partial(self._async_on_state_change, entity_id),
            )
            self._tracked[entity_id] = tracked

            current = self._hass.states.get(entity_id)
            # Seed the history buffer with the current value so the first
            # activity start has at least one point.
            if current is not None:
                self._record_history_sample(tracked, current)

            # Resume if entity is currently in a start state (HA restart)
            if current and current.state in entity_cfg.get(CONF_START_STATES, []):
                await self._start_activity(entity_id)

    async def _async_load_history(self) -> dict[str, list[tuple[int, dict[str, float]]]]:
        """Read the persisted ring buffers from .storage."""
        try:
            raw = await self._history_store.async_load()
        except Exception:
            _LOGGER.debug("Failed to load persisted history, starting fresh", exc_info=True)
            return {}
        if not raw:
            return {}
        result: dict[str, list[tuple[int, dict[str, float]]]] = {}
        for entity_id, samples in raw.get(_HISTORY_SAMPLES_KEY, {}).items():
            rehydrated: list[tuple[int, dict[str, float]]] = []
            for entry in samples:
                # Tolerate JSON round-tripping: lists instead of tuples, stringified keys.
                try:
                    ts = int(entry[0])
                    values = {str(k): float(v) for k, v in entry[1].items()}
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                rehydrated.append((ts, values))
            if rehydrated:
                result[entity_id] = rehydrated
        return result

    @callback
    def _schedule_history_save(self) -> None:
        """Debounced write of all ring buffers to .storage."""
        self._history_store.async_delay_save(self._serialize_history, _HISTORY_SAVE_DELAY_S)

    @callback
    def _serialize_history(self) -> dict:
        """Build the JSON payload written by async_delay_save."""
        return {
            _HISTORY_SAMPLES_KEY: {
                entity_id: [[ts, values] for ts, values in tracked.history_buffer]
                for entity_id, tracked in self._tracked.items()
                if tracked.history_buffer
            }
        }

    async def async_stop(self) -> None:
        """End all active activities and unsubscribe listeners."""
        # Flush any pending debounced history write before tearing down. Store
        # also listens to EVENT_HOMEASSISTANT_FINAL_WRITE for full HA shutdown,
        # but config-entry reloads don't fire that event.
        if self._tracked:
            try:
                await self._history_store.async_save(self._serialize_history())
            except Exception:
                _LOGGER.warning("Failed to flush history buffer on stop", exc_info=True)

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

        # Always sample into the history buffer, even when the activity is not
        # active — that's exactly how we accumulate backfill material.
        self._record_history_sample(tracked, new_state)

        config = tracked.config
        start_states = config.get(CONF_START_STATES, [])
        end_states = config.get(CONF_END_STATES, [])

        if new_state.state in start_states and not tracked.is_active:
            self._hass.async_create_task(self._start_activity(entity_id))
        elif new_state.state in end_states and tracked.is_active:
            self._schedule_end(entity_id)
        elif tracked.is_active:
            self._schedule_throttled_update(entity_id)

    def _record_history_sample(self, tracked: TrackedEntity, state: State) -> None:
        """Append a timeline value sample to the per-entity history buffer."""
        config = tracked.config
        if config.get(CONF_TEMPLATE) != "timeline":
            return
        if not config.get(CONF_HISTORY_PERIOD, 0):
            return
        values = _get_timeline_values(state, config)
        if not values:
            return
        ts = int(state.last_updated.timestamp())
        tracked.history_buffer.append((ts, values))
        self._schedule_history_save()

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

        async with self._api_error_guard(slug, "starting"):
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

            # Seed timeline back-history from HA recorder if configured
            if config.get(CONF_TEMPLATE) == "timeline":
                history = await self._seed_timeline_history(entity_id, config, content)
                if history:
                    content["history"] = history

            sound = config.get(CONF_SOUND) or None
            await self._api.update_activity(slug, "ONGOING", content, sound=sound)
            self._clear_forbidden_notification(slug)

            tracked.is_active = True
            tracked.last_content = content
            tracked.last_sent_at = time.monotonic()

    async def _seed_timeline_history(
        self, entity_id: str, config: dict, _content: dict
    ) -> dict[str, list[dict]] | None:
        """Build back-history for the timeline sparkline.

        Primary source is the in-memory ring buffer, populated on every state
        change. HA 2024.8+ strips attributes like ``brightness`` from the
        recorder DB, so a recorder query cannot rebuild attribute history —
        only state history. We fall back to the recorder only for entities
        whose primary state is the numeric value (no series, no value
        attribute), e.g. plain numeric sensors.
        """
        period_minutes = config.get(CONF_HISTORY_PERIOD, 0)
        if not period_minutes:
            return None

        tracked = self._tracked.get(entity_id)
        cutoff = int(time.time()) - period_minutes * 60

        history: dict[str, list[dict]] = {}
        if tracked:
            for ts, values in tracked.history_buffer:
                if ts < cutoff:
                    continue
                for label, v in values.items():
                    history.setdefault(label, []).append({"t": ts, "v": v})

        if not history and not (config.get(CONF_SERIES) or config.get(CONF_VALUE_ATTRIBUTE)):
            history = await self._recorder_history_fallback(entity_id, period_minutes)

        for key in history:
            if len(history[key]) > _HISTORY_BUFFER_MAX:
                history[key] = history[key][-_HISTORY_BUFFER_MAX:]

        _LOGGER.debug(
            "History seed %s: period=%dm buffer=%d series=%d points=%d",
            entity_id,
            period_minutes,
            len(tracked.history_buffer) if tracked else 0,
            len(history),
            sum(len(v) for v in history.values()),
        )

        return history if history else None

    async def _recorder_history_fallback(self, entity_id: str, period_minutes: int) -> dict[str, list[dict]]:
        """Pull numeric primary-state history from the recorder (sensors etc.)."""
        try:
            from homeassistant.components.recorder.history import get_significant_states
        except ImportError:
            return {}

        from homeassistant.helpers.recorder import get_instance
        from homeassistant.util import dt as dt_util

        now = dt_util.utcnow()
        start = now - timedelta(minutes=period_minutes)

        try:
            states = await get_instance(self._hass).async_add_executor_job(
                partial(
                    get_significant_states,
                    self._hass,
                    start,
                    now,
                    [entity_id],
                    significant_changes_only=False,
                )
            )
        except Exception:
            _LOGGER.debug("Failed to query recorder for %s", entity_id, exc_info=True)
            return {}

        entity_states = states.get(entity_id, [])
        label = entity_id
        history: dict[str, list[dict]] = {}
        for state_obj in entity_states:
            if state_obj.state in ("unavailable", "unknown"):
                continue
            with contextlib.suppress(ValueError, TypeError):
                history.setdefault(label, []).append(
                    {"t": int(state_obj.last_updated.timestamp()), "v": float(state_obj.state)}
                )
        return history

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
        async with self._api_error_guard(slug, "updating"):
            sound = tracked.config.get(CONF_SOUND) or None
            await self._api.update_activity(slug, "ONGOING", content, sound=sound)
            self._clear_forbidden_notification(slug)
            tracked.last_content = content
            tracked.last_sent_at = time.monotonic()

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
            async with self._api_error_guard(slug, "ending"):
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
                self._clear_forbidden_notification(slug)
        finally:
            task = asyncio.current_task()
            if task is None or not task.cancelled():
                tracked.is_active = False
                tracked.last_content = None
                if tracked.flush_unsub:
                    tracked.flush_unsub()
                    tracked.flush_unsub = None
