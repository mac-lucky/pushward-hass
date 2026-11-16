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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import (
    PushWardApiClient,
    PushWardApiError,
    PushWardAuthError,
    PushWardForbiddenError,
    PushWardNotFoundError,
    PushWardQuotaExceededError,
    PushWardWidgetPermissionError,
)
from .const import (
    CONF_BATTERY_DEVICES,
    CONF_CHARGING_ENTITY,
    CONF_ENTITY_ID,
    CONF_FLOW_NODES,
    CONF_HEATMAP_AGGREGATE,
    CONF_HEATMAP_WEEKS,
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
    DEFAULT_HEATMAP_WEEKS,
    DEFAULT_WIDGET_POLL_INTERVAL,
    DEFAULT_WIDGET_TREND_PERIOD,
    HEATMAP_AGGREGATE_MEAN,
    HEATMAP_AGGREGATES,
    QUOTA_KIND_WIDGET_UPDATES,
    WIDGET_GROUP_TEMPLATES,
    WIDGET_HEATMAP_REFRESH_MINUTE,
    WIDGET_HEATMAP_WEEKS_MAX,
    WIDGET_MAX_TREND_POINTS,
    WIDGET_MIN_TREND_POINTS,
    WIDGET_STALE_AFTER_MAX,
    WIDGET_STALE_AFTER_MIN,
    WIDGET_TEMPLATE_BATTERY,
    WIDGET_TEMPLATE_FLOW,
    WIDGET_TEMPLATE_HEATMAP,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_TREND,
    WIDGET_TREND_BUFFER_MAX,
    WIDGET_TRIGGER_EVENT,
    WIDGET_TRIGGER_POLL,
)
from .content_mapper import lookup_registry_icon
from .quota import quota_released_signal
from .recorder_history import async_daily_values, async_recorder_states, downsample_evenly
from .widget_mapper import map_widget_content, read_numeric_value, widget_name_from_config

_LOGGER = logging.getLogger(__name__)

_WIDGET_STORAGE_VERSION = 1
# Floor on the heartbeat tick. A stale_after at the 60 s minimum would otherwise
# ask for a push every 30 s, which is already more than the quota is worth.
_HEARTBEAT_MIN_INTERVAL = 30


def _widget_storage_key(entry_id: str) -> str:
    return f"pushward.widgets.{entry_id}"


def build_widget_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, _WIDGET_STORAGE_VERSION, _widget_storage_key(entry_id), atomic_writes=True)


async def async_load_widget_cache(store: Store) -> dict[str, dict]:
    """The cached widgets by slug; {} when the store is missing or unreadable."""
    try:
        raw = await store.async_load()
    except (HomeAssistantError, OSError, ValueError):
        _LOGGER.debug("Failed to load widget cache; starting fresh", exc_info=True)
        return {}
    if not raw or not isinstance(raw, dict):
        return {}
    widgets = raw.get("widgets")
    if not isinstance(widgets, dict):
        return {}
    return widgets


# Persistent notification ids carry the entry: each account has its own key and its own
# slugs, so one account's success must not dismiss another's notice.
def _forbidden_notification_id(entry_id: str, slug: str) -> str:
    return f"pushward_widget_forbidden_{entry_id}_{slug}"


def _permission_notification_id(entry_id: str) -> str:
    return f"pushward_widget_permission_{entry_id}"


@dataclass
class TrackedWidget:
    """In-memory state for a single tracked widget."""

    config: dict
    last_content: dict | None = None
    created: bool = False
    unsub_state: Callable[[], None] | None = None
    unsub_poll: Callable[[], None] | None = None
    unsub_heartbeat: Callable[[], None] | None = None
    unsub_hourly: Callable[[], None] | None = None
    registry_icon: str | None = None
    pending_task: asyncio.Task | None = field(default=None, repr=False)
    # A change landing while a send is in flight re-sends the newest state after.
    update_pending: bool = False
    # One recreate per 404 streak; reset on the next successful PATCH.
    recreate_attempted: bool = False
    # The last PATCH was refused by the quota gate. Widgets cannot rely on the
    # content check alone at release time: `trend` is computed against the
    # previous value, so re-rendering an unchanged widget still differs.
    quota_refused: bool = False
    # monotonic() of the last create/PATCH that reached the server. Drives the
    # heartbeat, which exists only to keep a stale_after widget from expiring.
    last_synced: float = 0.0
    # (epoch_seconds, value) samples backing the trend sparkline.
    points_buffer: deque = field(default_factory=lambda: deque(maxlen=WIDGET_TREND_BUFFER_MAX))
    # Heatmap: the last daily read, the local date it was taken on (None = read on
    # the next send), and whether the warning for an empty read has been logged.
    cells: list | None = None
    cells_read_day: Any = None
    cells_missing_warned: bool = False


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


def heatmap_read(config: dict) -> tuple[str, int, str]:
    """The entity, day count and daily statistic a heatmap reads, clamped to what the server takes."""
    try:
        weeks = int(config.get(CONF_HEATMAP_WEEKS) or DEFAULT_HEATMAP_WEEKS)
    except (TypeError, ValueError):
        weeks = DEFAULT_HEATMAP_WEEKS
    aggregate = config.get(CONF_HEATMAP_AGGREGATE)
    if aggregate not in HEATMAP_AGGREGATES:
        aggregate = HEATMAP_AGGREGATE_MEAN
    return config.get(CONF_ENTITY_ID) or "", max(1, min(WIDGET_HEATMAP_WEEKS_MAX, weeks)) * 7, aggregate


def _merge_patch(content: dict, last: dict) -> dict:
    """content as a merge patch over last: every key last had and content lacks becomes null.

    The server merges a widget PATCH with RFC 7396, which keeps a key the patch leaves
    out, so a subtitle whose attribute went or a dropped look would stay on the phone.
    Nested objects (flow nodes, tap actions) are merged the same way, so recurse.
    """
    patch = dict(content)
    for key, old in last.items():
        if key not in content:
            patch[key] = None
        elif isinstance(old, dict) and isinstance(content[key], dict):
            patch[key] = _merge_patch(content[key], old)
    return patch


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
        self._unsub_quota: Callable[[], None] | None = None
        # Cache entries of widgets no subentry owns whose server delete has not landed yet.
        self._leftover: dict[str, dict] = {}

    # ----- public lifecycle -----

    async def async_start(self) -> None:
        """Set up listeners, restore cached state, and POST initial widgets."""
        persisted = await async_load_widget_cache(self._store)

        if self._unsub_quota is None:
            self._unsub_quota = async_dispatcher_connect(
                self._hass, quota_released_signal(self._entry.entry_id), self._on_quota_released
            )

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

        # A cached widget no subentry owns any more (removed while this entry was not
        # loaded, or its delete failed) is still on the server with its phone
        # registrations. It stays cached until a delete lands so the next start tries
        # again; set before the initial sync, whose debounced save can fire mid-delete.
        self._leftover = {slug: cached for slug, cached in persisted.items() if slug not in self._tracked}

        # Idempotent server upsert per widget — fan out so HA boot isn't
        # serialized on N round-trips. The API client semaphore caps concurrency.
        # Each sync claims the widget's single-flight slot: a state change landing
        # mid-boot coalesces via the dirty flag instead of racing a second create
        # for the same slug (the listener above is already attached).
        if pending:
            tasks = [self._claim_slot(tracked, self._initial_sync(tracked)) for tracked in pending]
            await asyncio.gather(*tasks, return_exceptions=True)

        leftover = list(self._leftover)
        deleted = await asyncio.gather(*(self._delete_widget(slug) for slug in leftover), return_exceptions=True)
        for slug, ok in zip(leftover, deleted, strict=True):
            if ok is True:
                self._leftover.pop(slug, None)

        # Rewrite the cache only when its key set differs from the new tracked
        # set — otherwise this startup-time save is a redundant disk write.
        if set(persisted) != set(self._tracked) | set(self._leftover):
            try:
                await self._store.async_save(self._serialize_cache())
            except (HomeAssistantError, OSError, ValueError):
                _LOGGER.debug("Failed to persist widget cache on start", exc_info=True)

    async def async_stop(self) -> None:
        """Detach all listeners and flush cache to disk."""
        if self._unsub_quota is not None:
            self._unsub_quota()
            self._unsub_quota = None
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
        # A removed subentry's widget is still in the cache async_stop writes, so the
        # async_start below deletes it from the server.
        await self.async_stop()
        self._widgets = widgets
        await self.async_start()

    @callback
    def _on_quota_released(self, kind: str) -> None:
        """Re-send, through the single-flight scheduler, the widgets the quota refused."""
        if kind != QUOTA_KIND_WIDGET_UPDATES:
            return
        for tracked in self._tracked.values():
            if tracked.quota_refused:
                tracked.quota_refused = False
                self._schedule_update(tracked)

    async def _delete_widget(self, slug: str) -> bool:
        """Delete a server-side widget whose subentry was removed.

        True once a retry cannot help: it is gone (404 included), or the key may not
        delete it (403), which every later start would only hit again.
        """
        async with self._api_error_guard(slug, "deleting"):
            try:
                await self._api.delete_widget(slug)
            except PushWardForbiddenError as err:
                _LOGGER.warning("PushWard widget %s stays on the server, this key may not delete it: %s", slug, err)
            return True
        return False

    def slugs_for_entity(self, entity_id: str | None) -> list[str]:
        """Slugs of the tracked widgets whose own entity is entity_id.

        A group row, a companion or a subtitle timer on that entity does not count:
        deleting by entity must not take a whole stat list with it.
        """
        return [
            slug
            for slug, tracked in self._tracked.items()
            if entity_id
            and tracked.config.get(CONF_WIDGET_TEMPLATE) not in WIDGET_GROUP_TEMPLATES
            and tracked.config.get(CONF_ENTITY_ID) == entity_id
        ]

    async def async_refresh(self, slug: str | None = None, entity_id: str | None = None) -> None:
        """Manual refresh: bypass diff cache and force a PATCH.

        Refreshes the widget with that slug, or every widget that shows entity_id
        (rows and companions included). Raises ValueError if none is tracked.
        """
        if slug:
            targets = [target] if (target := self._tracked.get(slug)) else []
        else:
            targets = [t for t in self._tracked.values() if entity_id and entity_id in _entity_ids_for_widget(t.config)]
        if not targets:
            raise ValueError(f"No tracked widget for slug={slug!r} entity_id={entity_id!r}")
        await asyncio.gather(*(self._force_refresh(target) for target in targets))

    async def _force_refresh(self, target: TrackedWidget) -> None:
        # Wait out any in-flight send first -- two _send_update calls for the same
        # widget must never interleave (they race last_content and the 404
        # recreate flag). The forced send then claims the single-flight slot like
        # any other update.
        while (prev := target.pending_task) is not None and not prev.done():
            await asyncio.wait([prev])
        target.cells_read_day = None  # a manual refresh reads the recorder again
        task = self._spawn_send(target, force=True)
        await asyncio.wait([task])
        if not task.cancelled() and (exc := task.exception()) is not None:
            raise exc

    # ----- subscription setup -----

    def _subscribe(self, tracked: TrackedWidget) -> None:
        """Attach event-track or polling timer based on trigger mode."""
        self._arm_heartbeat(tracked)
        if tracked.config.get(CONF_WIDGET_TEMPLATE) == WIDGET_TEMPLATE_HEATMAP:
            # Statistics land once an hour, after any state change that went into
            # them, so each hour is read again once compiled. The 00:05 read starts
            # the new day's cell (local time: the grid's last cell is today).
            tracked.unsub_hourly = async_track_time_change(
                self._hass,
                partial(self._on_hourly_read, tracked.config[CONF_SLUG]),
                minute=WIDGET_HEATMAP_REFRESH_MINUTE,
                second=0,
            )
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
        if tracked.unsub_hourly:
            tracked.unsub_hourly()
            tracked.unsub_hourly = None

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
    def _on_hourly_read(self, slug: str, _now: Any) -> None:
        tracked = self._tracked.get(slug)
        if tracked is None:
            return
        tracked.cells_read_day = None
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
            cells=await self._heatmap_cells(tracked),
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
            cells=await self._heatmap_cells(tracked),
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
                # Keys dropped since the last push go out as null once; last_content
                # keeps the frame without them, so the next diff compares like with like.
                patch_content = _merge_patch(content, tracked.last_content or {})
                # stale_after rides every PATCH, null included: the merge patch
                # null-clears, so a widget created before the field existed (or
                # one whose config lost it) converges instead of keeping a value
                # HA no longer knows about.
                patch_body: dict = {"content": patch_content, "stale_after": _stale_after(cfg)}
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

    async def _heatmap_cells(self, tracked: TrackedWidget) -> list | None:
        """The heatmap's daily cells, read from the recorder only when a read is due.

        A read is due at setup, after the hourly read or a manual refresh cleared
        the last one, and on the first send after local midnight. A state change
        reuses the last read: long-term statistics only move at the hourly
        compile, which the hourly read picks up, so re-reading on a change could
        not return anything new.
        """
        cfg = tracked.config
        if cfg.get(CONF_WIDGET_TEMPLATE) != WIDGET_TEMPLATE_HEATMAP:
            return None
        today = dt_util.now().date()
        # A read from yesterday is never reused: its last cell is the wrong day.
        if tracked.cells_read_day == today:
            return tracked.cells
        # Stamped before the read: an hourly reset landing during it must win.
        tracked.cells_read_day = today
        entity_id, days, aggregate = heatmap_read(cfg)
        tracked.cells = await async_daily_values(self._hass, entity_id, days, aggregate)
        if tracked.cells and any(cell is not None for cell in tracked.cells):
            tracked.cells_missing_warned = False
        elif not tracked.cells_missing_warned:
            tracked.cells_missing_warned = True
            _LOGGER.warning(
                "Heatmap widget %s has no daily %s statistics for %s; not sending it until there are some",
                cfg[CONF_SLUG],
                aggregate,
                entity_id,
            )
        return tracked.cells

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

    @callback
    def _schedule_cache_save(self) -> None:
        # Debounced so a burst of state changes doesn't hammer disk.
        self._store.async_delay_save(self._serialize_cache, 30)

    @callback
    def _serialize_cache(self) -> dict:
        # A widget with no content yet is cached too: its create may have reached the
        # server, so removing its subentry must still delete it on the next start.
        return {"widgets": {**self._leftover, **{slug: self._serialize_widget(t) for slug, t in self._tracked.items()}}}

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
        # One persistent notification covers the entry since the cause
        # (missing widgets:true flag) is global to its integration key.
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
            title=f"{self._entry.title} — Widget permission required",
            notification_id=_permission_notification_id(self._entry.entry_id),
        )

    @callback
    def _clear_forbidden_notification(self, slug: str) -> None:
        """Bookkeeping after a push reached the server."""
        if (tracked := self._tracked.get(slug)) is not None:
            tracked.quota_refused = False
        if self._permission_notified:
            self._permission_notified = False
            persistent_notification.async_dismiss(self._hass, _permission_notification_id(self._entry.entry_id))
        persistent_notification.async_dismiss(self._hass, _forbidden_notification_id(self._entry.entry_id, slug))
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
                title=f"{self._entry.title} widget — {slug}",
                notification_id=_forbidden_notification_id(self._entry.entry_id, slug),
            )
            self._log_push_failure(slug, "PushWard 403 while %s widget %s: %s", context, slug, err)
        except PushWardQuotaExceededError as err:
            # The quota gate warned once when it armed; just remember who to re-send.
            _LOGGER.debug("PushWard quota exhausted while %s widget %s: %s", context, slug, err)
            if (tracked := self._tracked.get(slug)) is not None:
                tracked.quota_refused = True
        except PushWardApiError as err:
            self._log_push_failure(slug, "PushWard API error while %s widget %s: %s", context, slug, err)
        except aiohttp.ClientError:
            self._log_push_failure(slug, "PushWard network error while %s widget %s", context, slug, exc_info=True)
