"""Push reminders for Home Assistant to-do items that have a due date.

A tracked to-do list (a ``tracked_todo`` subentry) mirrors every open item with a
due date into one PushWard scheduled notification. Home Assistant stays the source
of truth: whenever the to-do entity writes its state (every edit does; one that
keeps the open-item count fires state_reported instead of state_changed), just
after each reminder's time, hourly and at start, the manager reads the open items
and reconciles them with the schedules it made. That pairing lives in .storage,
never in the item, whose description can sync to other apps (Apple Reminders
notes).

- A reminder goes out at the item's due time minus the list's offset; an item
  with only a due date uses the list's all-day time on that day.
- Editing an item replaces its schedule (purged, so no canceled record shows in
  the app); completing or removing the item cancels it.
- A schedule canceled elsewhere (the PushWard app) stays canceled: the item stays
  open and gets no new reminder until its due date or time changes. The same goes
  for a reminder that already went out: renaming the item does not send it again.
- With the Done button on, the reminder carries a url-less "done" action. The
  server records the tap; the manager watches for it and completes the item.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .api import (
    PushWardApiClient,
    PushWardApiError,
    PushWardAuthError,
    PushWardForbiddenError,
    PushWardNotFoundError,
    PushWardQuotaExceededError,
    PushWardRateLimitedError,
)
from .const import (
    ANSWER_LONG_POLL_SECONDS,
    ANSWER_MIN_POLL_INTERVAL,
    ANSWER_STATUS_ANSWERED,
    CONF_ENTITY_ID,
    CONF_SUBENTRY_ID,
    CONF_TODO_ALL_DAY_TIME,
    CONF_TODO_DONE_BUTTON,
    CONF_TODO_LEVEL,
    CONF_TODO_MAX_SCHEDULED,
    CONF_TODO_OFFSET_MINUTES,
    DEFAULT_TODO_ALL_DAY_TIME,
    DEFAULT_TODO_DONE_BUTTON,
    DEFAULT_TODO_LEVEL,
    DEFAULT_TODO_MAX_SCHEDULED,
    DEFAULT_TODO_OFFSET_MINUTES,
    DOMAIN,
    SUBENTRY_TYPE_TODO,
    TODO_BODY_MAX,
    TODO_DONE_ACTION_ID,
    TODO_LATE_GRACE_MINUTES,
    TODO_METADATA_LIST,
    TODO_METADATA_UID,
    TODO_NEAR_DUE_DELAY_SECONDS,
    TODO_RECONCILE_DEBOUNCE_SECONDS,
    TODO_RECONCILE_INTERVAL_MINUTES,
    TODO_SCHEDULE_HORIZON_DAYS,
    TODO_SCHEDULE_HORIZON_MARGIN_MINUTES,
    TODO_SEND_RECHECK_SECONDS,
    TODO_TITLE_MAX,
    TODO_WATCH_ACTIVE_MINUTES,
    TODO_WATCH_ERROR_BACKOFF_SECONDS,
    TODO_WATCH_HOURS,
    TODO_WATCH_IDLE_SECONDS,
)
from .e2e import E2EError

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1
_SAVE_DELAY = 5

# Record states. scheduled: pending on the server. sent: its time came (the Done
# button may still be watched). stopped: canceled before its time by someone else.
# sent and stopped records stay until their item is past the late-grace window, so
# the same item version never gets a second reminder, even if the item briefly
# leaves the open list (completed, then undone).
_SCHEDULED = "scheduled"
_SENT = "sent"
_STOPPED = "stopped"

# Server-side schedule statuses the manager reads.
_SERVER_SENT = "sent"
_SERVER_CANCELED = "canceled"
_SERVER_PENDING = ("scheduled", "sending")

# Pause after a watch round that did not hold a long-poll, so a reminder still
# being sent does not spin the loop.
_WATCH_SHORT_PAUSE_SECONDS = 10
# Tries at completing an item for a Done tap before the tap is given up.
_COMPLETE_ATTEMPTS = 3

# The server refuses every encrypted notification from an organization's key.
_E2E_UNAVAILABLE_CODE = "notification.encryption_unavailable"


def build_todo_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, _STORAGE_VERSION, f"{DOMAIN}.todo.{entry_id}", atomic_writes=True)


def todo_configs(entry: ConfigEntry) -> list[dict]:
    """The tracked to-do list configs, each tagged with the subentry that owns it."""
    return [
        {**sub.data, CONF_SUBENTRY_ID: sub.subentry_id}
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_TODO
    ]


def _key_hash(integration_key: str) -> str:
    return hashlib.sha256(integration_key.encode()).hexdigest()[:16]


async def async_cancel_stored_schedules(hass: HomeAssistant, api: PushWardApiClient, entry_id: str) -> None:
    """Cancel every pending reminder a removed config entry left, then drop its store."""
    store = build_todo_store(hass, entry_id)
    data = await store.async_load() or {}
    schedule_ids = [
        rec["schedule_id"]
        for lst in (data.get("lists") or {}).values()
        for rec in (lst.get("items") or {}).values()
        if rec.get("status") == _SCHEDULED and isinstance(rec.get("schedule_id"), int)
    ]
    schedule_ids += [sid for sid in data.get("orphans") or [] if isinstance(sid, int)]
    # cancel is 404-safe; isolate failures so one bad id cannot strand the rest.
    await asyncio.gather(*(api.cancel_scheduled_notification(sid) for sid in schedule_ids), return_exceptions=True)
    await store.async_remove()


@dataclass(frozen=True)
class _Reminder:
    uid: str
    version: str
    due: str
    fingerprint: str
    due_at: datetime
    send_at: datetime
    title: str
    body: str


def _is_date_only(due: Any) -> bool:
    return (isinstance(due, date) and not isinstance(due, datetime)) or (isinstance(due, str) and len(due) == 10)


def _parse_due(due: Any, all_day_time: time) -> datetime | None:
    """The moment an item is due, in UTC. A date-only due uses the all-day time that day."""
    if _is_date_only(due):
        day = due if isinstance(due, date) else dt_util.parse_date(due)
        if day is None:
            return None
        local = datetime.combine(day, all_day_time, tzinfo=dt_util.get_default_time_zone())
        return local.astimezone(dt_util.UTC)
    parsed = due if isinstance(due, datetime) else dt_util.parse_datetime(due) if isinstance(due, str) else None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    # UTC, so a lead time is real minutes even across a DST change.
    return parsed.astimezone(dt_util.UTC)


def _parse_time(value: Any) -> time:
    parsed = dt_util.parse_time(str(value or "")) if not isinstance(value, time) else value
    return parsed or dt_util.parse_time(DEFAULT_TODO_ALL_DAY_TIME) or time(9, 0)


def _iso(value: datetime) -> str:
    return value.astimezone(dt_util.UTC).isoformat()


def _from_iso(value: Any) -> datetime | None:
    parsed = dt_util.parse_datetime(str(value or ""))
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


def _due_key(item: dict) -> str:
    return str(item.get("due") or "")


def _same_occurrence(rec: dict, item: dict, version: str) -> bool:
    """Whether a sent or stopped record still stands for the item's current due time.

    Only a new due date or time is a new occurrence; changing the wording of an item
    whose reminder already went out (or was stopped) does not send it again.
    """
    if "due" in rec:
        return rec["due"] == _due_key(item)
    return rec.get("version") == version


def _version(item: dict) -> str:
    """One version of an item: any edit to a pending reminder replaces it."""
    version = [item.get("summary"), item.get("description"), str(item.get("due") or "")]
    return hashlib.sha256(json.dumps(version, default=str).encode()).hexdigest()[:16]


def _fingerprint(version: str, cfg: dict, date_only: bool) -> str:
    """An item version under the list's settings: a change replaces a pending reminder.

    Settings are not part of the version, so changing them never sends again a
    reminder that already went out. The all-day time only counts for date-only items.
    """
    parts = [
        version,
        cfg.get(CONF_TODO_OFFSET_MINUTES, DEFAULT_TODO_OFFSET_MINUTES),
        str(cfg.get(CONF_TODO_ALL_DAY_TIME, DEFAULT_TODO_ALL_DAY_TIME)) if date_only else "",
        cfg.get(CONF_TODO_LEVEL, DEFAULT_TODO_LEVEL),
        bool(cfg.get(CONF_TODO_DONE_BUTTON, DEFAULT_TODO_DONE_BUTTON)),
    ]
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:16]


def e2e_unavailable_issue_id(entry_id: str) -> str:
    """Repair issue for an entry whose reminders are refused because its key cannot encrypt."""
    return f"todo_e2e_unavailable_{entry_id}"


def _collapse_id(sub_id: str, uid: str) -> str:
    """One collapse id per item, so a duplicate send replaces the banner instead of stacking."""
    return "ha-todo-" + hashlib.sha256(f"{sub_id}/{uid}".encode()).hexdigest()[:32]


class TodoReminderManager:
    """Keeps PushWard reminders in step with the tracked to-do lists of one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: PushWardApiClient,
        configs: list[dict],
        entry: ConfigEntry,
        integration_key: str,
    ) -> None:
        self._hass = hass
        self._api = api
        self._entry = entry
        self._key_hash = _key_hash(integration_key)
        self._configs: dict[str, dict] = {cfg[CONF_SUBENTRY_ID]: cfg for cfg in configs}
        self._store = build_todo_store(hass, entry.entry_id)
        # subentry id -> {"entity_id": str, "items": {uid: record}}; persisted.
        self._lists: dict[str, dict] = {}
        # Schedules of removed lists whose cancel failed; retried on each full pass.
        self._orphans: list[int] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._debouncers: dict[str, Debouncer] = {}
        self._unsubs: dict[str, list[CALLBACK_TYPE]] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        # Lists written while a pass was reading them: that pass runs once more.
        self._dirty: set[str] = set()
        # Lists that ran into the account's pending cap, the quota or a forbidden
        # key: no creates until the next full pass. Items the server refused, by
        # the item version refused, so they are not re-sent on every edit-driven pass.
        self._blocked: set[str] = set()
        self._refused: dict[tuple[str, str], str] = {}
        self._e2e_issue_raised = False
        self._unsub_interval: CALLBACK_TYPE | None = None
        self._unsub_started: CALLBACK_TYPE | None = None
        self._watch_task: asyncio.Task | None = None
        self._watch_wakeup = asyncio.Event()
        self._done_label = "Done"
        self._stopped = False

    async def async_start(self) -> None:
        data = await self._store.async_load()
        data = data if isinstance(data, dict) else {}
        lists = data.get("lists")
        self._lists = lists if isinstance(lists, dict) else {}
        self._orphans = [sid for sid in data.get("orphans") or [] if isinstance(sid, int)]
        if data.get("key") not in (None, self._key_hash):
            # Another integration key made these; this one can neither see nor
            # cancel them (revoking the old key deletes them server-side). Forget
            # the pending ones so their items are scheduled again under this key.
            for lst in self._lists.values():
                items = lst.get("items") or {}
                for uid in [uid for uid, rec in items.items() if rec.get("status") == _SCHEDULED]:
                    del items[uid]
            self._orphans = []
        self._done_label = await self._async_done_label()
        for sub_id in self._configs:
            self._track(sub_id)
        self._unsub_interval = async_track_time_interval(
            self._hass, self._async_interval_reconcile, timedelta(minutes=TODO_RECONCILE_INTERVAL_MINUTES)
        )
        self._unsub_started = async_at_started(self._hass, self._async_on_started)
        self._watch_task = self._entry.async_create_background_task(
            self._hass, self._async_watch_loop(), name=f"{DOMAIN} to-do reminder answers"
        )

    async def async_stop(self) -> None:
        self._stopped = True
        for sub_id in list(self._unsubs):
            self._untrack(sub_id)
        for unsub in (self._unsub_interval, self._unsub_started):
            if unsub is not None:
                unsub()
        self._unsub_interval = self._unsub_started = None
        if self._watch_task is not None:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None
        # Let passes already under way finish (they stop before their next create).
        for lock in list(self._locks.values()):
            async with lock:
                pass
        # A new integration key reloads the entry; the issue comes back on the
        # next refusal if that key belongs to an organization too.
        self._clear_e2e_issue()
        await self._store.async_save(self._serialize())

    @callback
    def async_retry_refused(self) -> None:
        """Forget the refused reminders and look at every list again.

        For an encryption key change: a reminder refused under the old setting
        may go out now, without waiting for its item to be edited.
        """
        self._refused.clear()
        self._clear_e2e_issue()
        if not self._stopped:
            self._entry.async_create_background_task(
                self._hass, self._async_reconcile_all(), name=f"{DOMAIN} to-do reminders key change"
            )

    async def async_reload(self, configs: list[dict]) -> None:
        """Apply added, changed and removed tracked lists."""
        new = {cfg[CONF_SUBENTRY_ID]: cfg for cfg in configs}
        for sub_id in set(self._configs) - set(new):
            self._untrack(sub_id)
            self._configs.pop(sub_id)
            await self._async_drop_list(sub_id)
        for sub_id, cfg in new.items():
            old = self._configs.get(sub_id)
            if old == cfg:
                continue
            if old is not None:
                self._untrack(sub_id)
                if old.get(CONF_ENTITY_ID) != cfg.get(CONF_ENTITY_ID):
                    await self._async_drop_list(sub_id)
            self._configs[sub_id] = cfg
            self._blocked.discard(sub_id)
            self._refused = {key: v for key, v in self._refused.items() if key[0] != sub_id}
            self._track(sub_id)
            self._entry.async_create_background_task(
                self._hass, self._async_reconcile(sub_id, full=True), name=f"{DOMAIN} to-do reminders reload"
            )
        self._save()

    # --- tracking ---------------------------------------------------------

    def _track(self, sub_id: str) -> None:
        entity_id = self._configs[sub_id][CONF_ENTITY_ID]
        debouncer = Debouncer(
            self._hass,
            _LOGGER,
            cooldown=TODO_RECONCILE_DEBOUNCE_SECONDS,
            immediate=False,
            function=partial(self._async_reconcile, sub_id),
        )
        self._debouncers[sub_id] = debouncer

        @callback
        def _written(_event: Event) -> None:
            # The debouncer drops a call that comes due while one is still running,
            # so the flag makes a running pass go round once more instead.
            self._dirty.add(sub_id)
            debouncer.async_schedule_call()

        self._unsubs[sub_id] = [
            async_track_state_change_event(self._hass, [entity_id], _written),
            async_track_state_report_event(self._hass, [entity_id], _written),
        ]

    def _untrack(self, sub_id: str) -> None:
        for unsub in self._unsubs.pop(sub_id, []):
            unsub()
        debouncer = self._debouncers.pop(sub_id, None)
        if debouncer is not None:
            debouncer.async_cancel()
        timer = self._timers.pop(sub_id, None)
        if timer is not None:
            timer()

    def _arm_timer(self, sub_id: str) -> None:
        """Reconcile just after the list's earliest pending reminder goes out."""
        timer = self._timers.pop(sub_id, None)
        if timer is not None:
            timer()
        if self._stopped or sub_id not in self._configs:
            return
        records = (self._lists.get(sub_id) or {}).get("items", {})
        times = [t for rec in records.values() if rec.get("status") == _SCHEDULED and (t := _from_iso(rec["send_at"]))]
        if not times:
            return

        @callback
        def _due(_now: datetime) -> None:
            self._timers.pop(sub_id, None)
            self._entry.async_create_background_task(
                self._hass, self._async_reconcile(sub_id), name=f"{DOMAIN} to-do reminder sent"
            )

        # Never in the past: a reminder whose time passed while the list could not
        # be read would otherwise re-arm an immediate timer on every pass.
        when = max(min(times), dt_util.utcnow()) + timedelta(seconds=TODO_SEND_RECHECK_SECONDS)
        self._timers[sub_id] = async_track_point_in_utc_time(self._hass, _due, when)

    @callback
    def _async_on_started(self, _hass: HomeAssistant) -> None:
        self._entry.async_create_background_task(self._hass, self._async_reconcile_all(), name=f"{DOMAIN} to-do start")

    async def _async_interval_reconcile(self, _now: datetime) -> None:
        await self._async_reconcile_all()

    async def _async_reconcile_all(self) -> None:
        # A list removed while the entry was not loaded still has its reminders pending.
        for sub_id in set(self._lists) - set(self._configs):
            await self._async_drop_list(sub_id)
        await self._async_cancel_orphans()
        for sub_id in list(self._configs):
            await self._async_reconcile(sub_id, full=True)

    async def _async_drop_list(self, sub_id: str) -> None:
        """Forget a list that is no longer tracked, canceling what it still has pending."""
        async with self._locks.setdefault(sub_id, asyncio.Lock()):
            lst = self._lists.pop(sub_id, None) or {}
            self._blocked.discard(sub_id)
            self._dirty.discard(sub_id)
            for rec in (lst.get("items") or {}).values():
                if rec.get("status") != _SCHEDULED:
                    continue
                try:
                    await self._api.cancel_scheduled_notification(rec["schedule_id"])
                except PushWardApiError:
                    self._orphans.append(rec["schedule_id"])
        self._save()

    async def _async_cancel_orphans(self) -> None:
        for schedule_id in list(self._orphans):
            try:
                await self._api.cancel_scheduled_notification(schedule_id)
            except PushWardApiError:
                continue
            if schedule_id in self._orphans:
                self._orphans.remove(schedule_id)
        self._save()

    # --- reconcile --------------------------------------------------------

    async def _async_reconcile(self, sub_id: str, *, full: bool = False) -> None:
        """Bring one list's reminders in step with its open items.

        A full pass (start, hourly, a config change) also asks the server what
        became of pending reminders and removes this list's schedules the store
        does not know; an edit-driven pass does not, because a polled to-do
        integration writes its state on every poll.
        """
        lock = self._locks.setdefault(sub_id, asyncio.Lock())
        async with lock:
            while True:
                cfg = self._configs.get(sub_id)
                if cfg is None or self._stopped:
                    return
                self._dirty.discard(sub_id)
                if full:
                    self._blocked.discard(sub_id)
                try:
                    await self._async_reconcile_locked(sub_id, cfg, full=full)
                except PushWardAuthError:
                    _LOGGER.warning("PushWard rejected the integration key; to-do reminders are paused")
                    self._entry.async_start_reauth(self._hass)
                    return
                except PushWardApiError as err:
                    _LOGGER.warning("Could not sync PushWard reminders for %s: %s", cfg[CONF_ENTITY_ID], err)
                finally:
                    self._save()
                    self._arm_timer(sub_id)
                if sub_id not in self._dirty:
                    return
                full = False

    async def _async_reconcile_locked(self, sub_id: str, cfg: dict, *, full: bool) -> None:
        entity_id = cfg[CONF_ENTITY_ID]
        lst = self._lists.setdefault(sub_id, {"entity_id": entity_id, "items": {}})
        records: dict[str, dict] = lst["items"]
        # Reminders whose time came are sent, whether or not the list can be read now.
        now = dt_util.utcnow()
        for rec in records.values():
            if rec["status"] == _SCHEDULED and (_from_iso(rec["send_at"]) or now) <= now:
                self._mark_sent(rec, cfg)
                self._blocked.discard(sub_id)  # a pending slot just freed up

        items = await self._async_open_items(entity_id)
        if items is None:
            # Unavailable or not loaded yet: never read that as "every item is gone".
            return
        now = dt_util.utcnow()

        if full:
            await self._async_check_pending(sub_id, cfg, records)

        open_items = {item["uid"]: item for item in items if item.get("uid")}
        versions = {uid: _version(item) for uid, item in open_items.items()}
        self._refused = {key: v for key, v in self._refused.items() if key[0] != sub_id or versions.get(key[1]) == v}
        wanted = self._wanted(sub_id, cfg, open_items, versions, records, now)

        for uid, rec in list(records.items()):
            if rec["status"] == _SCHEDULED:
                reminder = wanted.get(uid)
                if reminder is not None and reminder.fingerprint == rec["fingerprint"]:
                    del wanted[uid]
                    continue
                if self._stopped:
                    return
                # Still open: an edit (or a sooner item taking its slot) replaces it,
                # which leaves no canceled record. Completed or removed: a real cancel.
                # Either way a pending slot is free again.
                await self._api.cancel_scheduled_notification(rec["schedule_id"], purge=uid in open_items)
                del records[uid]
                self._blocked.discard(sub_id)
            elif uid in open_items and not _same_occurrence(rec, open_items[uid], versions[uid]):
                del records[uid]  # moved to a new due time: that one gets a reminder of its own
            elif self._finished(rec, now):
                del records[uid]
            elif uid not in open_items:
                rec["watch_until"] = None  # completed or removed: no Done tap to wait for

        if sub_id not in self._blocked:
            await self._async_create(sub_id, cfg, records, wanted)

    async def _async_check_pending(self, sub_id: str, cfg: dict, records: dict[str, dict]) -> None:
        """Find out what became of pending reminders, and purge this list's unknown schedules."""
        pending = {
            row["id"]: row for row in await self._api.list_scheduled_notifications() if isinstance(row.get("id"), int)
        }
        for uid, rec in list(records.items()):
            if rec["status"] != _SCHEDULED or rec["schedule_id"] in pending:
                continue
            row = await self._api.get_scheduled_notification(rec["schedule_id"])
            status = (row or {}).get("status")
            if status == _SERVER_SENT:
                self._mark_sent(rec, cfg, row.get("notification_id"))
            elif status == _SERVER_CANCELED:
                rec["status"] = _STOPPED  # stopped by the owner: leave the item be
            elif status in _SERVER_PENDING:
                continue
            else:
                # Gone (a revoked key, a restored backup) or failed: look at the item
                # again as if it had never been scheduled.
                del records[uid]
        # Schedules tagged with this list that the store does not know: a create
        # that went through although its response was lost, or a lost store.
        known = {rec["schedule_id"] for rec in records.values()}
        for schedule_id, row in pending.items():
            if (row.get("metadata") or {}).get(TODO_METADATA_LIST) == sub_id and schedule_id not in known:
                await self._api.cancel_scheduled_notification(schedule_id, purge=True)

    def _wanted(
        self,
        sub_id: str,
        cfg: dict,
        open_items: dict[str, dict],
        versions: dict[str, str],
        records: dict[str, dict],
        now: datetime,
    ) -> dict[str, _Reminder]:
        """The reminders the list should have pending now: the soonest, up to the list's cap."""
        all_day_time = _parse_time(cfg.get(CONF_TODO_ALL_DAY_TIME, DEFAULT_TODO_ALL_DAY_TIME))
        offset = timedelta(minutes=int(cfg.get(CONF_TODO_OFFSET_MINUTES, DEFAULT_TODO_OFFSET_MINUTES)))
        late_limit = now - timedelta(minutes=TODO_LATE_GRACE_MINUTES)
        horizon = now + timedelta(days=TODO_SCHEDULE_HORIZON_DAYS, minutes=-TODO_SCHEDULE_HORIZON_MARGIN_MINUTES)
        list_name = self._list_name(cfg[CONF_ENTITY_ID])
        candidates: list[_Reminder] = []
        for uid, item in open_items.items():
            due = item.get("due")
            due_at = _parse_due(due, all_day_time)
            if due_at is None:
                continue
            rec = records.get(uid)
            if rec is not None and rec["status"] != _SCHEDULED and _same_occurrence(rec, item, versions[uid]):
                continue  # this due time was already reminded, or its reminder stopped
            if self._refused.get((sub_id, uid)) == versions[uid]:
                continue
            send_at = due_at - offset
            if send_at <= now:
                if due_at <= late_limit:
                    continue  # long overdue before it ever got a reminder
                send_at = now + timedelta(seconds=TODO_NEAR_DUE_DELAY_SECONDS)
            if send_at > horizon:
                continue  # picked up by a later reconcile once in range
            candidates.append(
                _Reminder(
                    uid=uid,
                    version=versions[uid],
                    due=_due_key(item),
                    fingerprint=_fingerprint(versions[uid], cfg, _is_date_only(due)),
                    due_at=due_at,
                    send_at=send_at,
                    title=str(item.get("summary") or list_name)[:TODO_TITLE_MAX],
                    body=str(item.get("description") or list_name)[:TODO_BODY_MAX],
                )
            )
        candidates.sort(key=lambda r: r.send_at)
        cap = int(cfg.get(CONF_TODO_MAX_SCHEDULED, DEFAULT_TODO_MAX_SCHEDULED))
        return {r.uid: r for r in candidates[:cap]}

    async def _async_create(
        self, sub_id: str, cfg: dict, records: dict[str, dict], wanted: dict[str, _Reminder]
    ) -> None:
        entity_id = cfg[CONF_ENTITY_ID]
        done_button = bool(cfg.get(CONF_TODO_DONE_BUTTON, DEFAULT_TODO_DONE_BUTTON))
        for reminder in sorted(wanted.values(), key=lambda r: r.send_at):
            if self._stopped or self._configs.get(sub_id) is not cfg:
                return  # stopping, or the list was removed or changed meanwhile
            # A catch-up time computed before earlier creates (and their retries)
            # may have passed by now; the server refuses a send_at in the past.
            send_at = max(reminder.send_at, dt_util.utcnow() + timedelta(seconds=TODO_NEAR_DUE_DELAY_SECONDS))
            try:
                created = await self._api.create_notification(
                    reminder.title,
                    reminder.body,
                    level=cfg.get(CONF_TODO_LEVEL, DEFAULT_TODO_LEVEL),
                    thread_id=f"ha-todo-{entity_id.split('.', 1)[-1]}"[:64],
                    collapse_id=_collapse_id(sub_id, reminder.uid),
                    source="home-assistant",
                    source_display_name=self._list_name(entity_id),
                    metadata={TODO_METADATA_LIST: sub_id, TODO_METADATA_UID: reminder.uid},
                    actions=[{"id": TODO_DONE_ACTION_ID, "title": self._done_label}] if done_button else None,
                    send_at=send_at,
                    # An encrypted reminder is shortened to fit rather than dropped.
                    trim_to_fit=True,
                )
            except E2EError as err:
                # Text that cannot be encrypted at all (invalid Unicode); nothing goes out in the clear.
                _LOGGER.warning("PushWard cannot encrypt the reminder for a to-do item in %s: %s", entity_id, err)
                self._refused[(sub_id, reminder.uid)] = reminder.version
                continue
            except (PushWardQuotaExceededError, PushWardForbiddenError) as err:
                _LOGGER.warning("PushWard reminders for %s are on hold: %s", entity_id, err)
                self._blocked.add(sub_id)
                return
            except PushWardAuthError:
                raise
            except PushWardApiError as err:
                if err.status_code is None or err.status_code >= 500 or err.status_code == 429:
                    raise  # transient: the next pass tries again
                if err.status_code == 409:
                    # 25 pending per account, shared with everything else on it.
                    _LOGGER.warning("PushWard has no room for more scheduled notifications: %s", err)
                    self._blocked.add(sub_id)
                    return
                _LOGGER.warning("PushWard refused the reminder for a to-do item in %s: %s", entity_id, err)
                self._refused[(sub_id, reminder.uid)] = reminder.version
                if err.code == _E2E_UNAVAILABLE_CODE:
                    self._raise_e2e_issue()
                continue
            schedule_id = (created or {}).get("id")
            if not isinstance(schedule_id, int):
                continue
            if self._e2e_issue_raised:
                # Accepted, so the key no longer meets the refusal (a pass that
                # was under way when the key changed may have raised it again).
                self._clear_e2e_issue()
            records[reminder.uid] = {
                "schedule_id": schedule_id,
                "version": reminder.version,
                "due": reminder.due,
                "fingerprint": reminder.fingerprint,
                "send_at": _iso(send_at),
                "due_at": _iso(reminder.due_at),
                "status": _SCHEDULED,
                "notification_id": None,
                "watch_until": None,
            }

    @callback
    def _raise_e2e_issue(self) -> None:
        if self._e2e_issue_raised:
            return
        self._e2e_issue_raised = True
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            e2e_unavailable_issue_id(self._entry.entry_id),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="todo_e2e_unavailable",
            translation_placeholders={"account": self._entry.title},
        )

    @callback
    def _clear_e2e_issue(self) -> None:
        self._e2e_issue_raised = False
        ir.async_delete_issue(self._hass, DOMAIN, e2e_unavailable_issue_id(self._entry.entry_id))

    def _mark_sent(self, rec: dict, cfg: dict, notification_id: Any = None) -> None:
        rec["status"] = _SENT
        if isinstance(notification_id, int):
            rec["notification_id"] = notification_id
        if cfg.get(CONF_TODO_DONE_BUTTON, DEFAULT_TODO_DONE_BUTTON):
            send_at = _from_iso(rec["send_at"]) or dt_util.utcnow()
            rec["watch_until"] = _iso(send_at + timedelta(hours=TODO_WATCH_HOURS))
            self._watch_wakeup.set()

    @staticmethod
    def _finished(rec: dict, now: datetime) -> bool:
        """A sent or stopped record no longer needed: past the late-grace window and unwatched."""
        due_at = _from_iso(rec.get("due_at"))
        watch_until = _from_iso(rec.get("watch_until"))
        late_limit = now - timedelta(minutes=TODO_LATE_GRACE_MINUTES)
        return (due_at is None or due_at <= late_limit) and (watch_until is None or watch_until <= now)

    async def _async_open_items(self, entity_id: str) -> list[dict] | None:
        state = self._hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            response = await self._hass.services.async_call(
                "todo",
                "get_items",
                {"status": ["needs_action"]},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.debug("Could not read %s: %s", entity_id, err)
            return None
        items = ((response or {}).get(entity_id) or {}).get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _list_name(self, entity_id: str) -> str:
        state = self._hass.states.get(entity_id)
        return str((state.attributes.get("friendly_name") if state else None) or entity_id)

    async def _async_done_label(self) -> str:
        try:
            strings = await async_get_translations(self._hass, self._hass.config.language, "selector", {DOMAIN})
        except HomeAssistantError:
            return "Done"
        return strings.get(f"component.{DOMAIN}.selector.todo_reminder_action.options.done") or "Done"

    # --- Done button ------------------------------------------------------

    def _watches(self, now: datetime) -> list[tuple[str, str, dict]]:
        watches = []
        for sub_id in self._configs:
            for uid, rec in (self._lists.get(sub_id) or {}).get("items", {}).items():
                watch_until = _from_iso(rec.get("watch_until"))
                if rec.get("status") == _SENT and watch_until is not None and watch_until > now:
                    watches.append((sub_id, uid, rec))
        return watches

    async def _async_watch_loop(self) -> None:
        """Watch sent reminders for a Done tap, one read at a time.

        Rotates over every watched reminder. While any was sent within the last
        TODO_WATCH_ACTIVE_MINUTES each round is a server long-poll (through the
        install's shared wait slots); after that a round is a plain read every
        TODO_WATCH_IDLE_SECONDS, until the watch ends TODO_WATCH_HOURS after the
        send.
        """
        turn = 0
        while not self._stopped:
            now = dt_util.utcnow()
            watches = self._watches(now)
            if not watches:
                self._watch_wakeup.clear()
                await self._watch_wakeup.wait()
                continue
            active_since = now - timedelta(minutes=TODO_WATCH_ACTIVE_MINUTES)
            active = any((_from_iso(w[2]["send_at"]) or now) > active_since for w in watches)
            sub_id, uid, rec = watches[turn % len(watches)]
            turn += 1
            started = asyncio.get_running_loop().time()
            pause: float = 0 if active else TODO_WATCH_IDLE_SECONDS
            try:
                held = await self._async_watch_once(sub_id, uid, rec, hold=ANSWER_LONG_POLL_SECONDS if active else 0)
                if active and not held:
                    pause = _WATCH_SHORT_PAUSE_SECONDS
            except PushWardRateLimitedError as err:
                pause = max(pause, err.retry_after, TODO_WATCH_ERROR_BACKOFF_SECONDS)
            except PushWardNotFoundError:
                rec["watch_until"] = None  # nothing to read, e.g. no answer was recorded
                self._save()
            except PushWardApiError as err:
                _LOGGER.debug("Watching a to-do reminder for its answer failed: %s", err)
                pause = max(pause, TODO_WATCH_ERROR_BACKOFF_SECONDS)
            except Exception:
                _LOGGER.exception("Unexpected error watching a to-do reminder for its answer")
                pause = max(pause, TODO_WATCH_ERROR_BACKOFF_SECONDS)
            # A round that came back early (a server shutting down answers waits
            # with one plain read) must not turn into a tight loop.
            elapsed = asyncio.get_running_loop().time() - started
            pause = max(pause, ANSWER_MIN_POLL_INTERVAL - elapsed)
            if pause > 0:
                self._watch_wakeup.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._watch_wakeup.wait(), pause)

    async def _async_watch_once(self, sub_id: str, uid: str, rec: dict, *, hold: int) -> bool:
        """One look at one reminder's answer; True when it held a long-poll."""
        notification_id = rec.get("notification_id")
        if not isinstance(notification_id, int):
            row = await self._api.get_scheduled_notification(rec["schedule_id"])
            status = (row or {}).get("status")
            if status in _SERVER_PENDING:
                return False  # still being sent
            if status != _SERVER_SENT or not isinstance(row.get("notification_id"), int):
                if status == "failed":
                    # Not sent (the account was out of quota at its time, say).
                    _LOGGER.warning("A PushWard to-do reminder could not be sent: %s", row.get("failure_reason"))
                rec["watch_until"] = None  # failed, canceled or gone: no tap will come
                self._save()
                return False
            notification_id = rec["notification_id"] = row["notification_id"]
            self._save()
        answer, held = await self._api.poll_notification_answer(notification_id, hold=hold)
        if answer.get("status") != ANSWER_STATUS_ANSWERED:
            return held
        cfg = self._configs.get(sub_id)
        done = cfg is not None and answer.get("action_id") == TODO_DONE_ACTION_ID
        if done and not await self._async_complete(cfg[CONF_ENTITY_ID], uid):
            # Keep watching: the answer stays readable, so a later round tries again.
            rec["complete_failures"] = rec.get("complete_failures", 0) + 1
            if rec["complete_failures"] < _COMPLETE_ATTEMPTS:
                self._save()
                return False
        rec["watch_until"] = None
        self._save()
        return held

    async def _async_complete(self, entity_id: str, uid: str) -> bool:
        """Complete the item a Done tap was for; True when done (or nothing is left to do)."""
        state = self._hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning("Could not complete a to-do item for a Done tap: %s is unavailable", entity_id)
            return False
        try:
            await self._hass.services.async_call(
                "todo",
                "update_item",
                {"item": uid, "status": "completed"},
                target={"entity_id": entity_id},
                blocking=True,
            )
        except ServiceValidationError:
            return True  # the item is gone: nothing left to complete
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.warning("Could not complete a to-do item in %s for a Done tap: %s", entity_id, err)
            return False
        return True

    # --- storage ----------------------------------------------------------

    def _serialize(self) -> dict:
        return {"key": self._key_hash, "lists": self._lists, "orphans": self._orphans}

    def _save(self) -> None:
        self._store.async_delay_save(self._serialize, _SAVE_DELAY)
