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
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store

from .api import PushWardApiClient, PushWardApiError, PushWardAuthError, PushWardForbiddenError
from .const import (
    ACTIVITY_STATE_ENDED,
    ACTIVITY_STATE_ONGOING,
    CONF_ACTIVITY_NAME,
    CONF_CURRENT_STEP_ENTITY,
    CONF_END_STATES,
    CONF_ENDED_TTL,
    CONF_ENTITY_ID,
    CONF_FIRED_AT_ENTITY,
    CONF_HISTORY_PERIOD,
    CONF_PRIORITY,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SERIES,
    CONF_SLUG,
    CONF_SOUND,
    CONF_STALE_TTL,
    CONF_START_STATES,
    CONF_SUBTITLE_ENTITY,
    CONF_TEMPLATE,
    CONF_TILES,
    CONF_UPDATE_INTERVAL,
    CONF_VALUE_ATTRIBUTE,
    CONF_VALUE_ENTITY,
    END_DELAY_SECONDS,
    LOG_MAX_LINES,
)
from .content_mapper import (
    _build_log_line,
    _get_timeline_values,
    lookup_registry_icon,
    map_completion_content,
    map_content,
)

_HISTORY_BUFFER_MAX = 300
_HISTORY_STORAGE_VERSION = 1
_HISTORY_SAVE_DELAY_S = 30
_HISTORY_SAMPLES_KEY = "samples"
# Persisted log ring buffers live in the same Store as timeline history so log
# templates survive restarts (the server backlog is the durable source of truth,
# but this avoids a visible gap before the first post-restart push).
_LOG_SAMPLES_KEY = "logs"

_ACTIVITY_LIMIT_NOTIFICATION_ID = "pushward_activity_limit"

# Config keys that may point a value at a SEPARATE companion entity. Changes to
# these entities should refresh the activity even though they don't drive
# start/end (the tracked entity owns lifecycle).
_COMPANION_ENTITY_KEYS = (
    CONF_REMAINING_TIME_ENTITY,
    CONF_PROGRESS_ENTITY,
    CONF_VALUE_ENTITY,
    CONF_CURRENT_STEP_ENTITY,
    CONF_FIRED_AT_ENTITY,
    CONF_SUBTITLE_ENTITY,
)


def _companion_entity_ids(config: dict) -> list[str]:
    """Return the order-preserving, deduped, non-empty companion entity_ids for an activity.

    For board templates the per-tile entities are also companions: a change to any
    tile entity refreshes the board even though the anchor entity owns start/end.
    """
    ids = [eid for key in _COMPANION_ENTITY_KEYS if (eid := config.get(key))]
    if config.get(CONF_TEMPLATE) == "board":
        for tile in config.get(CONF_TILES) or []:
            if isinstance(tile, dict) and (eid := tile.get(CONF_ENTITY_ID)):
                ids.append(eid)
    return list(dict.fromkeys(ids))


def _same_log_line(head: dict | None, line: dict) -> bool:
    """Two log lines are "the same event" when text+level match (``at`` ignored).

    Used to collapse consecutive identical log lines so a re-reported state (turn-on
    attribute churn, periodic re-reports, restart re-seed) doesn't spam duplicates.
    """
    return head is not None and head.get("text") == line.get("text") and head.get("level") == line.get("level")


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
    companion_unsubs: list[Callable] = field(default_factory=list)
    flush_unsub: Callable | None = None
    end_task: asyncio.Task | None = field(default=None, repr=False)
    generation: int = 0
    last_sent_at: float = 0.0
    # (ts_seconds, {label: value}) — sampled on every state change. HA 2024.8+
    # strips light attributes from the recorder DB, so recorder queries can't
    # rebuild brightness history. Keep our own ring buffer instead.
    history_buffer: deque = field(default_factory=lambda: deque(maxlen=_HISTORY_BUFFER_MAX))
    # Newest-first log lines for the log template. Appended on every state change
    # (and seeded at start) so the pushed snapshot accrues history up to the cap.
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=LOG_MAX_LINES))


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
        return lookup_registry_icon(self._hass, entity_id)

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
        persisted, persisted_logs = await self._async_load_history()

        for entity_cfg in self._entities:
            entity_id = entity_cfg[CONF_ENTITY_ID]
            tracked = TrackedEntity(config=entity_cfg)

            # Rehydrate the buffers from disk so sparklines / logs survive restarts.
            for ts, values in persisted.get(entity_id, ()):
                tracked.history_buffer.append((ts, values))
            for line in persisted_logs.get(entity_id, ()):
                # Collapse consecutive same-text lines persisted by older builds
                # (the buffer is newest-first, so [-1] is the previous in sequence).
                if _same_log_line(tracked.log_buffer[-1] if tracked.log_buffer else None, line):
                    continue
                tracked.log_buffer.append(line)

            tracked.unsub_state = async_track_state_change_event(
                self._hass,
                entity_id,
                partial(self._async_on_state_change, entity_id),
            )

            # Subscribe to companion value entities so their changes refresh the
            # activity. They never start/end it — the tracked entity owns that.
            # One batched subscription for all companions of this entity.
            companion_ids = [cid for cid in _companion_entity_ids(entity_cfg) if cid != entity_id]
            if companion_ids:
                tracked.companion_unsubs.append(
                    async_track_state_change_event(
                        self._hass,
                        companion_ids,
                        partial(self._async_on_companion_change, entity_id),
                    )
                )

            self._tracked[entity_id] = tracked

            current = self._hass.states.get(entity_id)
            # Seed the history / log buffers with the current value so the first
            # activity start has at least one point / line.
            if current is not None:
                self._record_history_sample(tracked, current)
                self._record_log_sample(tracked, current)

            # Resume if entity is currently in a start state (HA restart)
            if current and current.state in entity_cfg.get(CONF_START_STATES, []):
                await self._start_activity(entity_id)

    async def _async_load_history(
        self,
    ) -> tuple[dict[str, list[tuple[int, dict[str, float]]]], dict[str, list[dict]]]:
        """Read the persisted ring buffers from .storage.

        Returns ``(history, logs)``: history is per-entity timeline samples; logs
        is per-entity newest-first log-line dicts.
        """
        try:
            raw = await self._history_store.async_load()
        except Exception:
            _LOGGER.debug("Failed to load persisted history, starting fresh", exc_info=True)
            return {}, {}
        if not raw:
            return {}, {}
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

        logs: dict[str, list[dict]] = {}
        for entity_id, lines in raw.get(_LOG_SAMPLES_KEY, {}).items():
            rehydrated_lines = [line for line in lines if isinstance(line, dict) and line.get("text")]
            if rehydrated_lines:
                logs[entity_id] = rehydrated_lines[:LOG_MAX_LINES]
        return result, logs

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
            },
            _LOG_SAMPLES_KEY: {
                entity_id: list(tracked.log_buffer)
                for entity_id, tracked in self._tracked.items()
                if tracked.log_buffer
            },
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

            for unsub in tracked.companion_unsubs:
                unsub()
            tracked.companion_unsubs.clear()

            if tracked.is_active:
                slug = tracked.config[CONF_SLUG]
                try:
                    content = map_completion_content(tracked.config, tracked.last_content)
                    await self._api.update_activity(slug, ACTIVITY_STATE_ENDED, content)
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

        # Always sample into the history / log buffers, even when the activity is
        # not active — that's exactly how we accumulate backfill material.
        self._record_history_sample(tracked, new_state)
        self._record_log_sample(tracked, new_state)

        config = tracked.config
        start_states = config.get(CONF_START_STATES, [])
        end_states = config.get(CONF_END_STATES, [])

        if new_state.state in start_states and not tracked.is_active:
            self._hass.async_create_task(self._start_activity(entity_id))
        elif new_state.state in end_states and tracked.is_active:
            self._schedule_end(entity_id)
        elif tracked.is_active:
            self._schedule_throttled_update(entity_id)

    @callback
    def _async_on_companion_change(self, entity_id: str, event: Event) -> None:
        """Handle a state change on a companion value entity.

        Companions only supply values, so a change refreshes the activity (when
        active) but never starts or ends it. History is sampled against the
        tracked entity's current state so the timeline series label stays stable.
        """
        new_state: State | None = event.data.get("new_state")
        tracked = self._tracked.get(entity_id)
        if tracked is None:
            return

        is_board = tracked.config.get(CONF_TEMPLATE) == "board"

        if not tracked.is_active:
            # A board defers its start when no tile is renderable yet (every tile
            # entity unavailable, e.g. right after restart). Retry now that a tile
            # changed, provided the anchor is still in a start state.
            if is_board:
                anchor = self._hass.states.get(entity_id)
                if anchor is not None and anchor.state in tracked.config.get(CONF_START_STATES, []):
                    self._hass.async_create_task(self._start_activity(entity_id))
            return

        unavailable = new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        # A timeline value-companion going unavailable carries no value to plot —
        # ignore it. A board tile going unavailable IS a meaningful change (the
        # tile must drop from the grid), so still refresh and let _build_board_tiles
        # re-render without it.
        if unavailable and not is_board:
            return

        if not unavailable:
            primary_state = self._hass.states.get(entity_id)
            if primary_state is not None:
                # Stamp with the companion's update time: the tracked entity is
                # unchanged here, so its last_updated would collide on every tick.
                self._record_history_sample(tracked, primary_state, ts=int(new_state.last_updated.timestamp()))

        self._schedule_throttled_update(entity_id)

    def _record_history_sample(self, tracked: TrackedEntity, state: State, ts: int | None = None) -> None:
        """Append a timeline value sample to the per-entity history buffer.

        ``ts`` overrides the sample timestamp; companion-driven samples pass the
        companion's last_updated so points aren't all stamped with the unchanged
        tracked entity's time.
        """
        config = tracked.config
        if config.get(CONF_TEMPLATE) != "timeline":
            return
        if not config.get(CONF_HISTORY_PERIOD, 0):
            return
        values = _get_timeline_values(state, config, self._hass)
        if not values:
            return
        if ts is None:
            ts = int(state.last_updated.timestamp())
        tracked.history_buffer.append((ts, values))
        self._schedule_history_save()

    def _record_log_sample(self, tracked: TrackedEntity, state: State) -> None:
        """Prepend a log line (newest-first) to the per-entity log ring buffer.

        No-op for non-log templates. A log tracks state *changes*, so consecutive
        lines with the same text+level are collapsed: an entity that re-reports
        the same displayed state is not a new event. This covers three cases that
        would otherwise spam duplicates — attribute settling on turn-on (a light
        fires several state_changed events with state "on" while brightness/color
        converge), periodic re-reports, and the start-time re-seed after a restart
        or subentry reconfigure. The first occurrence's timestamp is kept (when
        the entity entered that state) and ``at`` is ignored in the compare, so a
        restart's regenerated ``last_updated`` can't slip a copy of the persisted
        newest line past the guard.
        """
        if tracked.config.get(CONF_TEMPLATE) != "log":
            return
        line = _build_log_line(state, tracked.config)
        if not line.get("text"):
            return
        if _same_log_line(tracked.log_buffer[0] if tracked.log_buffer else None, line):
            return
        tracked.log_buffer.appendleft(line)
        self._schedule_history_save()

    def _apply_log_lines(self, tracked: TrackedEntity, content: dict) -> None:
        """Override content['lines'] with the full newest-first log ring buffer.

        Falls back to whatever single line map_content produced when the buffer is
        empty (the seam mirrors _seed_timeline_history injecting content['history']).
        """
        if tracked.config.get(CONF_TEMPLATE) != "log":
            return
        lines = list(tracked.log_buffer)
        if lines:
            content["lines"] = lines

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
            current_state = self._hass.states.get(entity_id)
            if current_state is None:
                return

            tracked.registry_icon = self._get_registry_icon(entity_id)
            content = map_content(current_state, config, registry_icon=tracked.registry_icon, hass=self._hass)

            # A board needs >=1 renderable tile. If every tile entity is still
            # unavailable (e.g. the anchor resumes a start state right after
            # restart, before companion sensors initialize), defer the whole start
            # — don't create an activity the server would reject as tile-less. A
            # later companion change retries _start_activity once a tile appears.
            if config.get(CONF_TEMPLATE) == "board" and not content.get("tiles"):
                return

            _LOGGER.debug(
                "Activity %s icon resolution: state_attr=%s, registry=%s, resolved=%s",
                slug,
                current_state.attributes.get("icon"),
                tracked.registry_icon,
                content.get("icon"),
            )

            # Resolve activity name: configured name > friendly name > entity_id
            name = config.get(CONF_ACTIVITY_NAME) or ""
            if not name:
                name = current_state.attributes.get("friendly_name", entity_id)

            await self._api.create_activity(
                slug,
                name,
                config.get(CONF_PRIORITY, 1),
                ended_ttl=config.get(CONF_ENDED_TTL),
                stale_ttl=config.get(CONF_STALE_TTL),
            )

            # Seed timeline back-history from HA recorder if configured
            if config.get(CONF_TEMPLATE) == "timeline":
                history = await self._seed_timeline_history(entity_id, config, content)
                if history:
                    content["history"] = history

            # Replace the single current line with the accumulated log buffer.
            self._apply_log_lines(tracked, content)

            sound = config.get(CONF_SOUND) or None
            await self._api.update_activity(slug, ACTIVITY_STATE_ONGOING, content, sound=sound)
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
                    history.setdefault(label, []).append({"timestamp": ts, "value": v})

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
                    {"timestamp": int(state_obj.last_updated.timestamp()), "value": float(state_obj.state)}
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

        content = map_content(current_state, tracked.config, registry_icon=tracked.registry_icon, hass=self._hass)
        # Inject the full log buffer before the dedup check so a new line counts as a change.
        self._apply_log_lines(tracked, content)
        # A board whose tile entities are all unavailable renders no tiles; the
        # server rejects a tile-less board, so keep the last good frame rather
        # than pushing an empty one — a tile returning re-renders it.
        if tracked.config.get(CONF_TEMPLATE) == "board" and not content.get("tiles"):
            return
        if content == tracked.last_content:
            return

        slug = tracked.config[CONF_SLUG]
        async with self._api_error_guard(slug, "updating"):
            sound = tracked.config.get(CONF_SOUND) or None
            await self._api.update_activity(slug, ACTIVITY_STATE_ONGOING, content, sound=sound)
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
                await self._api.update_activity(slug, ACTIVITY_STATE_ONGOING, completion)

                # Wait for user to see the completion state
                await asyncio.sleep(END_DELAY_SECONDS)

                # Abort if a new start happened during the sleep
                if tracked.generation != gen_at_start:
                    return

                # Phase 2: end the activity
                await self._api.update_activity(slug, ACTIVITY_STATE_ENDED, completion)
                self._clear_forbidden_notification(slug)
        finally:
            task = asyncio.current_task()
            if task is None or not task.cancelled():
                tracked.is_active = False
                tracked.last_content = None
                if tracked.flush_unsub:
                    tracked.flush_unsub()
                    tracked.flush_unsub = None
