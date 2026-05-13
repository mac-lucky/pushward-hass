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
from collections.abc import Callable
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
    PushWardWidgetPermissionError,
)
from .const import (
    CONF_ENTITY_ID,
    CONF_SLUG,
    CONF_STAT_ROWS,
    CONF_WIDGET_POLL_INTERVAL,
    CONF_WIDGET_TEMPLATE,
    CONF_WIDGET_TRIGGER_MODE,
    DEFAULT_WIDGET_POLL_INTERVAL,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TRIGGER_EVENT,
    WIDGET_TRIGGER_POLL,
)
from .content_mapper import lookup_registry_icon
from .widget_mapper import map_widget_content, widget_name_from_config

_LOGGER = logging.getLogger(__name__)

_WIDGET_STORAGE_VERSION = 1
_WIDGET_PERMISSION_NOTIFICATION = "pushward_widget_permission"


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
    registry_icon: str | None = None
    pending_task: asyncio.Task | None = field(default=None, repr=False)


def _entity_ids_for_widget(config: dict) -> list[str]:
    """Return all HA entity_ids the widget depends on for live updates."""
    template = config.get(CONF_WIDGET_TEMPLATE)
    if template == WIDGET_TEMPLATE_STAT_LIST:
        seen: list[str] = []
        for row in config.get(CONF_STAT_ROWS) or []:
            if not isinstance(row, dict):
                continue
            entity_id = row.get(CONF_ENTITY_ID)
            if entity_id and entity_id not in seen:
                seen.append(entity_id)
        return seen
    entity_id = config.get(CONF_ENTITY_ID)
    return [entity_id] if entity_id else []


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
            self._tracked[slug] = tracked

            self._subscribe(tracked)
            pending.append(tracked)

        # Idempotent server upsert per widget — fan out so HA boot isn't
        # serialized on N round-trips. The API client semaphore caps concurrency.
        if pending:
            await asyncio.gather(
                *(self._initial_sync(t) for t in pending),
                return_exceptions=True,
            )

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
        await self.async_stop()
        self._widgets = widgets
        await self.async_start()

    async def async_refresh(self, slug: str | None = None, entity_id: str | None = None) -> None:
        """Manual refresh: bypass diff cache and force a PATCH.

        Resolves the widget by slug or by primary entity_id. Raises ValueError
        if neither identifies a tracked widget.
        """
        target = self._resolve_target(slug=slug, entity_id=entity_id)
        if target is None:
            raise ValueError(f"No tracked widget for slug={slug!r} entity_id={entity_id!r}")
        await self._send_update(target, force=True)

    # ----- subscription setup -----

    def _subscribe(self, tracked: TrackedWidget) -> None:
        """Attach event-track or polling timer based on trigger mode."""
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
    def _schedule_update(self, tracked: TrackedWidget) -> None:
        """Coalesce burst events into a single update task per widget."""
        if tracked.pending_task is not None and not tracked.pending_task.done():
            return
        task = self._hass.async_create_task(self._send_update(tracked))
        tracked.pending_task = task
        task.add_done_callback(partial(self._clear_pending_task, tracked))

    @staticmethod
    def _clear_pending_task(tracked: TrackedWidget, task: asyncio.Task) -> None:
        if tracked.pending_task is task:
            tracked.pending_task = None

    # ----- core send -----

    async def _initial_sync(self, tracked: TrackedWidget) -> None:
        """POST /widgets once on setup so server config matches HA on every restart."""
        cfg = tracked.config
        slug = cfg[CONF_SLUG]
        template = cfg.get(CONF_WIDGET_TEMPLATE)
        if not template:
            _LOGGER.warning("Widget %s missing template; skipping create", slug)
            return

        content = map_widget_content(
            self._hass,
            cfg,
            prev_value=_extract_numeric(tracked.last_content),
            registry_icon=tracked.registry_icon,
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

        name = widget_name_from_config(cfg, self._hass)
        push_throttle = self._compute_push_throttle(cfg)

        async with self._api_error_guard(slug, "creating"):
            await self._api.create_widget(
                slug=slug,
                name=name,
                template=template,
                content=content,
                push_throttle=push_throttle,
            )
            tracked.created = True
            tracked.last_content = content
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
        )
        if content is None:
            return

        # Diff against the cached payload; skip identical pushes unless forced.
        if not force and tracked.created and content == tracked.last_content:
            return

        async with self._api_error_guard(slug, "updating"):
            if not tracked.created:
                # First successful render after a deferred initial POST.
                name = widget_name_from_config(cfg, self._hass)
                push_throttle = self._compute_push_throttle(cfg)
                await self._api.create_widget(
                    slug=slug,
                    name=name,
                    template=template,
                    content=content,
                    push_throttle=push_throttle,
                )
                tracked.created = True
            else:
                patch_body: dict = {"content": content}
                push_throttle = self._compute_push_throttle(cfg)
                if push_throttle is not None:
                    patch_body["push_throttle"] = push_throttle
                await self._api.patch_widget(slug, patch_body)

            tracked.last_content = content
            self._clear_forbidden_notification(slug)
            self._schedule_cache_save()

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
        # stat_list widgets have no single anchoring entity; the static icon
        # in cfg is the only icon path for that template.
        if cfg.get(CONF_WIDGET_TEMPLATE) == WIDGET_TEMPLATE_STAT_LIST:
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
                slug: {
                    "content": t.last_content,
                    "created": t.created,
                }
                for slug, t in self._tracked.items()
                if t.last_content is not None
            }
        }

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

    @contextlib.asynccontextmanager
    async def _api_error_guard(self, slug: str, context: str):
        try:
            yield
        except PushWardAuthError:
            self._trigger_reauth()
        except PushWardWidgetPermissionError as err:
            self._notify_widget_permission(str(err))
            _LOGGER.warning("PushWard widget permission denied while %s %s: %s", context, slug, err)
        except PushWardForbiddenError as err:
            persistent_notification.async_create(
                self._hass,
                f"PushWard widget: {err}",
                title=f"PushWard widget — {slug}",
                notification_id=_forbidden_notification_id(slug),
            )
            _LOGGER.warning("PushWard 403 while %s widget %s: %s", context, slug, err)
        except PushWardApiError as err:
            _LOGGER.warning("PushWard API error while %s widget %s: %s", context, slug, err)
        except aiohttp.ClientError:
            _LOGGER.warning("PushWard network error while %s widget %s", context, slug, exc_info=True)
