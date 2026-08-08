"""Widget lifecycle manager — drives PushWard widgets from HA entity state.

Each widget binds an HA entity (or, for stat_list, multiple entities) to a
server-side widget identified by `slug`. Events or polling re-evaluate the
mapping and, only when the rendered content differs from the last-sent payload,
issue a PATCH to the PushWard server. State is persisted to .storage so the
diff cache survives HA restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Any

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .api import (
    PushWardApiClient,
    PushWardApiError,
    PushWardAuthError,
    PushWardForbiddenError,
    PushWardNotFoundError,
    PushWardWidgetPermissionError,
)
from .const import (
    CONF_BATTERY_DEVICES,
    CONF_CHARGING_ENTITY,
    CONF_ENTITY_ID,
    CONF_FLOW_NODES,
    CONF_HISTORY_PERIOD,
    CONF_LEVEL_ENTITY,
    CONF_SLUG,
    CONF_STAT_ROWS,
    CONF_SUBTITLE_TIMER_ENTITY,
    CONF_TOTAL_ENTITY,
    CONF_WIDGET_POLL_INTERVAL,
    CONF_WIDGET_STALE_AFTER,
    CONF_WIDGET_TEMPLATE,
    CONF_WIDGET_TRIGGER_MODE,
    DEFAULT_WIDGET_POLL_INTERVAL,
    DEFAULT_WIDGET_TREND_PERIOD,
    WIDGET_GROUP_TEMPLATES,
    WIDGET_MAX_TREND_POINTS,
    WIDGET_MIN_TREND_POINTS,
    WIDGET_STALE_AFTER_MAX,
    WIDGET_STALE_AFTER_MIN,
    WIDGET_TEMPLATE_BATTERY,
    WIDGET_TEMPLATE_FLOW,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_TREND,
    WIDGET_TREND_BUFFER_MAX,
    WIDGET_TRIGGER_EVENT,
    WIDGET_TRIGGER_POLL,
)
from .content_mapper import lookup_registry_icon
from .recorder_history import async_recorder_states, downsample_evenly
from .widget_mapper import map_widget_content, read_numeric_value, widget_name_from_config

_LOGGER = logging.getLogger(__name__)

_WIDGET_STORAGE_VERSION = 1
_WIDGET_PERMISSION_NOTIFICATION = "pushward_widget_permission"
# Floor on the heartbeat tick. A stale_after at the 60 s minimum would otherwise
# ask for a push every 30 s, which is already more than the quota is worth.
_HEARTBEAT_MIN_INTERVAL = 30


def _widget_storage_key(entry_id: str) -> str:
    return f"pushward.widgets.{entry_id}"


def build_widget_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, _WIDGET_STORAGE_VERSION, _widget_storage_key(entry_id), atomic_writes=True)


def _forbidden_notification_id(slug: str) -> str:
    return f"pushward_widget_forbidden_{slug}"


@dataclass
class TrackedWidget:
    """In-memory state for a single tracked widget."""

    config: dict
    last_content: dict | None = None
    created: bool = False
    unsub_state: Callable[[], None] | None = None
    unsub_poll: Callable[[], None] | None = None
    unsub_heartbeat: Callable[[], None] | None = None
    registry_icon: str | None = None
    pending_task: asyncio.Task | None = field(default=None, repr=False)
    # A change landing while a send is in flight re-sends the newest state after.
    update_pending: bool = False
    # One recreate per 404 streak; reset on the next successful PATCH.
    recreate_attempted: bool = False
    # monotonic() of the last create/PATCH that reached the server. Drives the
    # heartbeat, which exists only to keep a stale_after widget from expiring.
    last_synced: float = 0.0
    # (epoch_seconds, value) samples backing the trend sparkline.
    points_buffer: deque = field(default_factory=lambda: deque(maxlen=WIDGET_TREND_BUFFER_MAX))


# Where each group template keeps its rows, and which per-row keys carry a
# companion entity on top of the row's own entity_id. Keyed by
# WIDGET_GROUP_TEMPLATES; the drift guard in the tests keeps the two aligned.
_GROUP_ROW_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    WIDGET_TEMPLATE_STAT_LIST: (CONF_STAT_ROWS, ()),
    WIDGET_TEMPLATE_BATTERY: (CONF_BATTERY_DEVICES, (CONF_CHARGING_ENTITY,)),
    WIDGET_TEMPLATE_FLOW: (CONF_FLOW_NODES, (CONF_TOTAL_ENTITY, CONF_LEVEL_ENTITY)),
}


def _entity_ids_for_widget(config: dict) -> list[str]:
    """Return all HA entity_ids the widget depends on for live updates."""
    template = config.get(CONF_WIDGET_TEMPLATE)
    seen: list[str] = []

    def add(entity_id: object) -> None:
        if isinstance(entity_id, str) and entity_id and entity_id not in seen:
            seen.append(entity_id)

    if template in _GROUP_ROW_SOURCES:
        rows_key, companion_keys = _GROUP_ROW_SOURCES[template]
        for row in config.get(rows_key) or []:
            if not isinstance(row, dict):
                continue
            add(row.get(CONF_ENTITY_ID))
            for key in companion_keys:
                add(row.get(key))
        return seen

    add(config.get(CONF_ENTITY_ID))
    # A subtitle timer can be anchored on a different entity than the widget.
    add(config.get(CONF_SUBTITLE_TIMER_ENTITY))
    return seen


def _stale_after(config: dict) -> int | None:
    """Read the configured stale_after, clamped to the server's bounds."""
    raw = config.get(CONF_WIDGET_STALE_AFTER)
    if raw in (None, ""):
        return None
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        return None
    return max(WIDGET_STALE_AFTER_MIN, min(WIDGET_STALE_AFTER_MAX, seconds))


def _trend_min_gap(config: dict) -> float:
    """Seconds between two samples of an unchanged trend value.

    Sized off the configured history window so a full buffer still spans it:
    at the 48-point wire cap, one sample per window/48 is the coarsest useful
    rate, and 60 s is the floor for a short window.
    """
    try:
        period = int(config.get(CONF_HISTORY_PERIOD) or 0)
    except (TypeError, ValueError):
        period = 0
    if period <= 0:
        period = DEFAULT_WIDGET_TREND_PERIOD
    return max(60.0, period * 60 / WIDGET_MAX_TREND_POINTS)


def _extract_numeric(content: dict | None) -> float | None:
    """Pull the trend-relevant numeric value out of a rendered payload."""
    if not content:
        return None
    value = content.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class WidgetManager:
    """Manages PushWard widgets driven by HA entity state."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: PushWardApiClient,
        widgets: list[dict],
        entry: ConfigEntry,
    ) -> None:
        self._hass = hass
        self._api = api
        self._widgets = widgets
        self._entry = entry
        self._tracked: dict[str, TrackedWidget] = {}
        self._store = build_widget_store(hass, entry.entry_id)
        self._reauth_triggered = False
        self._permission_notified = False
        # Slugs currently in a push-failure streak: WARN once on entry, DEBUG after.
        self._failed_slugs: set[str] = set()

    # ----- public lifecycle -----

    async def async_start(self) -> None:
        """Set up listeners, restore cached state, and POST initial widgets."""
        persisted = await self._async_load_cache()

        pending: list[TrackedWidget] = []
        for cfg in self._widgets:
            slug = cfg.get(CONF_SLUG)
            if not slug:
                _LOGGER.warning("Widget config missing slug; skipping: %s", cfg)
                continue
            tracked = TrackedWidget(config=cfg)
            cached = persisted.get(slug) or {}
            tracked.last_content = cached.get("content")
            tracked.created = bool(cached.get("created"))
            tracked.registry_icon = self._lookup_registry_icon(cfg)
            self._restore_points(tracked, cached.get("points"))
            self._tracked[slug] = tracked

            self._subscribe(tracked)
            pending.append(tracked)

        # Idempotent server upsert per widget — fan out so HA boot isn't
        # serialized on N round-trips. The API client semaphore caps concurrency.
        # Each sync claims the widget's single-flight slot: a state change landing
        # mid-boot coalesces via the dirty flag instead of racing a second create
        # for the same slug (the listener above is already attached).
        if pending:
            tasks = [self._claim_slot(tracked, self._initial_sync(tracked)) for tracked in pending]
            await asyncio.gather(*tasks, return_exceptions=True)

        # Rewrite the cache only when its key set differs from the new tracked
        # set — otherwise this startup-time save is a redundant disk write.
        if set(persisted.keys()) != set(self._tracked.keys()):
            try:
                await self._store.async_save(self._serialize_cache())
            except (HomeAssistantError, OSError, ValueError):
                _LOGGER.debug("Failed to persist widget cache on start", exc_info=True)

    async def async_stop(self) -> None:
        """Detach all listeners and flush cache to disk."""
        cancelled: list[asyncio.Task] = []
        for tracked in self._tracked.values():
            self._detach(tracked)
            if tracked.pending_task and not tracked.pending_task.done():
                tracked.pending_task.cancel()
                cancelled.append(tracked.pending_task)
        # Wait for the cancellations to settle so they can't fire against the
        # (about-to-be-replaced) tracker after async_start runs again.
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        try:
            await self._store.async_save(self._serialize_cache())
        except (HomeAssistantError, OSError, ValueError):
            _LOGGER.debug("Failed to persist widget cache on stop", exc_info=True)
        self._tracked.clear()

    async def async_reload(self, widgets: list[dict]) -> None:
        # A removed tracked_widget subentry must delete its server-side widget, otherwise the
        # row + device widget-push token leak forever (HA just stops driving it). Diff against
        # the previous config set BEFORE reassigning self._widgets below.
        removed = self._slug_set(self._widgets) - self._slug_set(widgets)
        await self.async_stop()
        self._widgets = widgets
        await self.async_start()
        # _delete_widget swallows expected API errors per slug; return_exceptions keeps an
        # unexpected one from stranding the rest.
        await asyncio.gather(*(self._delete_widget(slug) for slug in removed), return_exceptions=True)

    @staticmethod
    def _slug_set(widgets: list[dict]) -> set[str]:
        return {cfg[CONF_SLUG] for cfg in widgets if cfg.get(CONF_SLUG)}

    async def _delete_widget(self, slug: str) -> None:
        """Delete a server-side widget whose subentry was removed (best-effort, 404-safe)."""
        async with self._api_error_guard(slug, "deleting"):
            await self._api.delete_widget(slug)

    def slug_for_entity(self, entity_id: str | None) -> str | None:
        """Return the slug of the tracked widget bound to entity_id, if any."""
        target = self._resolve_target(entity_id=entity_id)
        return target.config.get(CONF_SLUG) if target else None

    async def async_refresh(self, slug: str | None = None, entity_id: str | None = None) -> None:
        """Manual refresh: bypass diff cache and force a PATCH.

        Resolves the widget by slug or by primary entity_id. Raises ValueError
        if neither identifies a tracked widget.
        """
        target = self._resolve_target(slug=slug, entity_id=entity_id)
        if target is None:
            raise ValueError(f"No tracked widget for slug={slug!r} entity_id={entity_id!r}")
        # Wait out any in-flight send first -- two _send_update calls for the same
        # widget must never interleave (they race last_content and the 404
        # recreate flag). The forced send then claims the single-flight slot like
        # any other update.
        while (prev := target.pending_task) is not None and not prev.done():
            await asyncio.wait([prev])
        task = self._spawn_send(target, force=True)
        await asyncio.wait([task])
        if not task.cancelled() and (exc := task.exception()) is not None:
            raise exc

    # ----- subscription setup -----

    def _subscribe(self, tracked: TrackedWidget) -> None:
        """Attach event-track or polling timer based on trigger mode."""
        self._arm_heartbeat(tracked)
        mode = tracked.config.get(CONF_WIDGET_TRIGGER_MODE) or WIDGET_TRIGGER_EVENT
        if mode == WIDGET_TRIGGER_POLL:
            interval = max(
                10,
                int(tracked.config.get(CONF_WIDGET_POLL_INTERVAL, DEFAULT_WIDGET_POLL_INTERVAL)),
            )
            tracked.unsub_poll = async_track_time_interval(
                self._hass,
                partial(self._on_poll_tick, tracked.config[CONF_SLUG]),
                interval=timedelta(seconds=interval),
            )
            _LOGGER.debug("Widget %s polling every %ss", tracked.config[CONF_SLUG], interval)
            return

        entity_ids = _entity_ids_for_widget(tracked.config)
        if not entity_ids:
            _LOGGER.debug(
                "Widget %s has no entity bindings; no state subscription",
                tracked.config.get(CONF_SLUG),
            )
            return
        tracked.unsub_state = async_track_state_change_event(
            self._hass,
            entity_ids,
            partial(self._on_state_change, tracked.config[CONF_SLUG]),
        )

    def _detach(self, tracked: TrackedWidget) -> None:
        if tracked.unsub_state:
            tracked.unsub_state()
            tracked.unsub_state = None
        if tracked.unsub_poll:
            tracked.unsub_poll()
            tracked.unsub_poll = None
        if tracked.unsub_heartbeat:
            tracked.unsub_heartbeat()
            tracked.unsub_heartbeat = None

    def _arm_heartbeat(self, tracked: TrackedWidget) -> None:
        """Keep a stale_after widget alive when its entity sits still.

        Without this a widget bound to a rarely-changing entity crosses its own
        stale threshold and iOS greys it out even though HA is perfectly healthy.
        The tick re-PATCHes identical content, which the server treats as a no-op
        that still re-stamps updated_at without sending a push.
        """
        stale_after = _stale_after(tracked.config)
        if stale_after is None:
            return
        interval = max(_HEARTBEAT_MIN_INTERVAL, stale_after // 2)
        tracked.unsub_heartbeat = async_track_time_interval(
            self._hass,
            partial(self._on_heartbeat, tracked.config[CONF_SLUG], interval),
            interval=timedelta(seconds=interval),
        )

    # ----- event/poll callbacks -----

    @callback
    def _on_state_change(self, slug: str, event: Event) -> None:
        tracked = self._tracked.get(slug)
        if tracked is None:
            return
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        self._schedule_update(tracked)

    @callback
    def _on_poll_tick(self, slug: str, _now: Any) -> None:
        tracked = self._tracked.get(slug)
        if tracked is None:
            return
        self._schedule_update(tracked)

    @callback
    def _on_heartbeat(self, slug: str, interval: int, _now: Any) -> None:
        tracked = self._tracked.get(slug)
        if tracked is None or not tracked.created:
            return
        if tracked.pending_task is not None and not tracked.pending_task.done():
            return
        if time.monotonic() - tracked.last_synced < interval:
            # A real update already refreshed updated_at inside this window.
            return
        self._spawn_send(tracked, force=True)

    @callback
    def _schedule_update(self, tracked: TrackedWidget) -> None:
        """Coalesce burst events into a single update task per widget.

        A change landing mid-send marks the widget dirty so the newest state is
        re-sent once the in-flight task finishes, instead of being dropped.
        """
        if tracked.pending_task is not None and not tracked.pending_task.done():
            tracked.update_pending = True
            return
        self._spawn_send(tracked)

    @callback
    def _claim_slot(self, tracked: TrackedWidget, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        """Run coro as the widget's single in-flight task.

        Sets pending_task and wires the done-callback so a change landing mid-send
        coalesces via the dirty flag instead of racing a second send for the same slug.
        Shared by _spawn_send and the async_start initial-sync fan-out.
        """
        task = self._hass.async_create_task(coro)
        tracked.pending_task = task
        task.add_done_callback(partial(self._on_send_done, tracked))
        return task

    @callback
    def _spawn_send(self, tracked: TrackedWidget, *, force: bool = False) -> asyncio.Task:
        """Run _send_update as the widget's single in-flight task."""
        return self._claim_slot(tracked, self._send_update(tracked, force=force))

    def _on_send_done(self, tracked: TrackedWidget, task: asyncio.Task) -> None:
        if tracked.pending_task is not task:
            return
        tracked.pending_task = None
        resend = tracked.update_pending and not task.cancelled()
        tracked.update_pending = False
        if resend and self._tracked.get(tracked.config[CONF_SLUG]) is tracked:
            self._schedule_update(tracked)

    # ----- core send -----

    async def _create_widget(self, tracked: TrackedWidget, content: dict) -> None:
        """POST /widgets (create-or-upsert) and mark the widget as created.

        Shared by the initial sync, the deferred-create branch, and the 404
        recovery path so the name/push_throttle resolution lives in one place.
        """
        cfg = tracked.config
        await self._api.create_widget(
            slug=cfg[CONF_SLUG],
            name=widget_name_from_config(cfg, self._hass),
            template=cfg[CONF_WIDGET_TEMPLATE],
            content=content,
            push_throttle=self._compute_push_throttle(cfg),
            stale_after=_stale_after(cfg),
        )
        tracked.created = True

    async def _initial_sync(self, tracked: TrackedWidget) -> None:
        """POST /widgets once on setup so server config matches HA on every restart."""
        cfg = tracked.config
        slug = cfg[CONF_SLUG]
        template = cfg.get(CONF_WIDGET_TEMPLATE)
        if not template:
            _LOGGER.warning("Widget %s missing template; skipping create", slug)
            return

        await self._seed_points(tracked)
        content = map_widget_content(
            self._hass,
            cfg,
            prev_value=_extract_numeric(tracked.last_content),
            registry_icon=tracked.registry_icon,
            points=self._sample_points(tracked),
        )

        if content is None:
            # progress / gauge / stat_list cannot create without valid data.
            # Defer until first valid state arrives via the subscribed trigger.
            _LOGGER.debug(
                "Widget %s: skipping initial POST — current state insufficient for template %r",
                slug,
                template,
            )
            return

        async with self._api_error_guard(slug, "creating"):
            await self._create_widget(tracked, content)
            tracked.last_content = content
            tracked.last_synced = time.monotonic()
            self._clear_forbidden_notification(slug)
            self._schedule_cache_save()

    async def _send_update(self, tracked: TrackedWidget, *, force: bool = False) -> None:
        cfg = tracked.config
        slug = cfg[CONF_SLUG]
        template = cfg.get(CONF_WIDGET_TEMPLATE)
        if not template:
            return

        content = map_widget_content(
            self._hass,
            cfg,
            prev_value=_extract_numeric(tracked.last_content),
            registry_icon=tracked.registry_icon,
            points=self._sample_points(tracked),
        )
        if content is None:
            return

        # Diff against the cached payload; skip identical pushes unless forced.
        if not force and tracked.created and content == tracked.last_content:
            return

        async with self._api_error_guard(slug, "updating"):
            if not tracked.created:
                # First successful render after a deferred initial POST.
                await self._create_widget(tracked, content)
            else:
                # stale_after rides every PATCH, null included: the merge patch
                # null-clears, so a widget created before the field existed (or
                # one whose config lost it) converges instead of keeping a value
                # HA no longer knows about.
                patch_body: dict = {"content": content, "stale_after": _stale_after(cfg)}
                push_throttle = self._compute_push_throttle(cfg)
                if push_throttle is not None:
                    patch_body["push_throttle"] = push_throttle
                try:
                    await self._api.patch_widget(slug, patch_body)
                except PushWardNotFoundError:
                    # The server has no row for this slug (deleted out-of-band, or our
                    # cached created=True outlived the server state). Reconcile by
                    # recreating it so updates self-heal instead of 404ing forever.
                    # One recreate per 404 streak: if the recreate did not stick,
                    # don't hammer the server on every state change.
                    if tracked.recreate_attempted:
                        _LOGGER.debug("Widget %s still missing server-side; skipping recreate", slug)
                        return
                    tracked.recreate_attempted = True
                    _LOGGER.debug("Widget %s missing server-side on update; recreating", slug)
                    await self._create_widget(tracked, content)
                    # The recreate push landed, so the 404 streak is over: re-arm the
                    # guard (reset on the next successful push) so a later re-deletion
                    # self-heals again instead of 404ing forever. If _create_widget had
                    # raised, the error guard would swallow it and leave the flag True.
                    tracked.recreate_attempted = False
                else:
                    tracked.recreate_attempted = False

            tracked.last_content = content
            tracked.last_synced = time.monotonic()
            self._clear_forbidden_notification(slug)
            self._schedule_cache_save()

    # ----- trend points -----

    @staticmethod
    def _restore_points(tracked: TrackedWidget, raw: object) -> None:
        """Rehydrate the trend buffer from the persisted [[ts, value], ...] list."""
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            try:
                ts, value = float(item[0]), float(item[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(ts) and math.isfinite(value):
                tracked.points_buffer.append((ts, value))

    async def _seed_points(self, tracked: TrackedWidget) -> None:
        """Backfill a fresh trend buffer from the recorder.

        Only on a cold buffer: a restored cache already spans the window, and
        re-seeding would re-merge points the buffer has since aged out.
        """
        cfg = tracked.config
        if cfg.get(CONF_WIDGET_TEMPLATE) != WIDGET_TEMPLATE_TREND or tracked.points_buffer:
            return
        entity_id = cfg.get(CONF_ENTITY_ID)
        try:
            period = int(cfg.get(CONF_HISTORY_PERIOD) or 0)
        except (TypeError, ValueError):
            period = 0
        if not entity_id or period <= 0:
            return
        history = await async_recorder_states(self._hass, [entity_id], period)
        points = history.get(entity_id) or []
        for point in downsample_evenly(points, WIDGET_TREND_BUFFER_MAX):
            tracked.points_buffer.append((float(point["timestamp"]), float(point["value"])))
        _LOGGER.debug("Widget %s seeded %d trend point(s)", cfg[CONF_SLUG], len(tracked.points_buffer))

    def _sample_points(self, tracked: TrackedWidget) -> list[float] | None:
        """Feed the trend buffer from live state and return the points to send."""
        cfg = tracked.config
        if cfg.get(CONF_WIDGET_TEMPLATE) != WIDGET_TEMPLATE_TREND:
            return None

        state = self._hass.states.get(cfg.get(CONF_ENTITY_ID) or "")
        if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            value = read_numeric_value(state, cfg)
            if value is not None and math.isfinite(value):
                self._append_point(tracked, time.time(), value)

        if len(tracked.points_buffer) < WIDGET_MIN_TREND_POINTS:
            return None
        return downsample_evenly([value for _ts, value in tracked.points_buffer], WIDGET_MAX_TREND_POINTS)

    @staticmethod
    def _append_point(tracked: TrackedWidget, ts: float, value: float) -> None:
        """Record a sample, skipping a repeat of the same value inside the gap.

        Without the dedup a poll-mode widget would fill all 300 slots with the
        same number in minutes and the sparkline would flatline to a single dot.
        """
        buffer = tracked.points_buffer
        if buffer:
            last_ts, last_value = buffer[-1]
            if value == last_value and ts - last_ts < _trend_min_gap(tracked.config):
                return
        buffer.append((ts, value))

    # ----- helpers -----

    def _resolve_target(self, *, slug: str | None = None, entity_id: str | None = None) -> TrackedWidget | None:
        if slug:
            return self._tracked.get(slug)
        if not entity_id:
            return None
        for tracked in self._tracked.values():
            if entity_id in _entity_ids_for_widget(tracked.config):
                return tracked
        return None

    def _lookup_registry_icon(self, cfg: dict) -> str | None:
        # stat_list / battery / flow have no single anchoring entity; the static
        # icon in cfg is the only icon path for those templates.
        if cfg.get(CONF_WIDGET_TEMPLATE) in WIDGET_GROUP_TEMPLATES:
            return None
        return lookup_registry_icon(self._hass, cfg.get(CONF_ENTITY_ID))

    @staticmethod
    def _compute_push_throttle(cfg: dict) -> int | None:
        """Couple server push_throttle to poll interval in poll mode."""
        mode = cfg.get(CONF_WIDGET_TRIGGER_MODE) or WIDGET_TRIGGER_EVENT
        if mode != WIDGET_TRIGGER_POLL:
            return None
        try:
            return max(1, int(cfg.get(CONF_WIDGET_POLL_INTERVAL, DEFAULT_WIDGET_POLL_INTERVAL)))
        except (TypeError, ValueError):
            return DEFAULT_WIDGET_POLL_INTERVAL

    # ----- cache persistence -----

    async def _async_load_cache(self) -> dict[str, dict]:
        try:
            raw = await self._store.async_load()
        except (HomeAssistantError, OSError, ValueError):
            _LOGGER.debug("Failed to load widget cache; starting fresh", exc_info=True)
            return {}
        if not raw or not isinstance(raw, dict):
            return {}
        widgets = raw.get("widgets")
        if not isinstance(widgets, dict):
            return {}
        return widgets

    @callback
    def _schedule_cache_save(self) -> None:
        # Debounced so a burst of state changes doesn't hammer disk.
        self._store.async_delay_save(self._serialize_cache, 30)

    @callback
    def _serialize_cache(self) -> dict:
        return {
            "widgets": {
                slug: self._serialize_widget(t) for slug, t in self._tracked.items() if t.last_content is not None
            }
        }

    @staticmethod
    def _serialize_widget(tracked: TrackedWidget) -> dict:
        entry: dict = {"content": tracked.last_content, "created": tracked.created}
        if tracked.points_buffer:
            # A trend restarting with an empty buffer would push a flat two-point
            # line until it re-accumulates, so the sparkline survives a restart.
            entry["points"] = [[ts, value] for ts, value in tracked.points_buffer]
        return entry

    # ----- error handling -----

    def _trigger_reauth(self) -> None:
        if not self._reauth_triggered:
            self._reauth_triggered = True
            _LOGGER.warning("PushWard widget auth failed — triggering reauthentication")
            self._entry.async_start_reauth(self._hass)

    def _notify_widget_permission(self, message: str) -> None:
        # One persistent notification covers the entire integration since the
        # cause (missing widgets:true flag) is global to the integration key.
        if self._permission_notified:
            return
        self._permission_notified = True
        persistent_notification.async_create(
            self._hass,
            (
                "PushWard widgets are disabled for this integration key. "
                f"Enable the `widgets` permission on the key, then reload "
                f"the integration. Server said: {message}"
            ),
            title="PushWard — Widget permission required",
            notification_id=_WIDGET_PERMISSION_NOTIFICATION,
        )

    @callback
    def _clear_forbidden_notification(self, slug: str) -> None:
        if self._permission_notified:
            self._permission_notified = False
            persistent_notification.async_dismiss(self._hass, _WIDGET_PERMISSION_NOTIFICATION)
        persistent_notification.async_dismiss(self._hass, _forbidden_notification_id(slug))
        if slug in self._failed_slugs:
            self._failed_slugs.discard(slug)
            _LOGGER.info("PushWard widget %s: pushes succeeding again", slug)

    def _log_push_failure(self, slug: str, msg: str, *args: Any, exc_info: bool = False) -> None:
        """WARN on entering the failure state per slug; DEBUG while it persists."""
        if slug in self._failed_slugs:
            _LOGGER.debug(msg, *args, exc_info=exc_info)
        else:
            self._failed_slugs.add(slug)
            _LOGGER.warning(msg, *args, exc_info=exc_info)

    @contextlib.asynccontextmanager
    async def _api_error_guard(self, slug: str, context: str):
        try:
            yield
        except PushWardAuthError:
            self._trigger_reauth()
        except PushWardWidgetPermissionError as err:
            self._notify_widget_permission(str(err))
            self._log_push_failure(slug, "PushWard widget permission denied while %s %s: %s", context, slug, err)
        except PushWardForbiddenError as err:
            persistent_notification.async_create(
                self._hass,
                f"PushWard widget: {err}",
                title=f"PushWard widget — {slug}",
                notification_id=_forbidden_notification_id(slug),
            )
            self._log_push_failure(slug, "PushWard 403 while %s widget %s: %s", context, slug, err)
        except PushWardApiError as err:
            self._log_push_failure(slug, "PushWard API error while %s widget %s: %s", context, slug, err)
        except aiohttp.ClientError:
            self._log_push_failure(slug, "PushWard network error while %s widget %s", context, slug, exc_info=True)
