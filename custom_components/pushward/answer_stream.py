"""The answer stream: a pushward_notification_action event for every answer.

Each config entry reads GET /notifications/answers, the server's feed of the
answers to what this integration key sent, with a long-poll, and fires
EVENT_NOTIFICATION_ACTION for each one. It covers every way a notification goes
out (send_notification, the notify entity, the to-do Done button), because the
API client reports each answerable send here.

The long-poll runs only while something can still be answered: a send from the
last ANSWER_STREAM_PENDING_DAYS days that has no answer yet. Most installs never
send one, so they hold no connection. A scheduled send has no notification id
until it goes out, so its schedule is tracked instead: it starts the stream when
it comes due and is read then to learn the notification it became. A repeating
one does that at each send and keeps the stream on for the window after it.

The cursor and the pending sends live in .storage, so a restart picks up where
the stream stopped. Cursors are opaque strings that are decimal integers today:
the stream compares them as such to start far enough back, and keeps its own
when one is not a number.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import random
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import (
    PushWardApiClient,
    PushWardApiError,
    PushWardAuthError,
    PushWardForbiddenError,
    PushWardQuotaExceededError,
    PushWardRateLimitedError,
)
from .const import (
    ANSWER_MIN_POLL_INTERVAL,
    ANSWER_PLAIN_AFTER_WAIT_LIMIT_SECONDS,
    ANSWER_PLAIN_POLL_INTERVAL,
    ANSWER_STREAM_BACKOFF_BASE_SECONDS,
    ANSWER_STREAM_BACKOFF_MAX_SECONDS,
    ANSWER_STREAM_PAGE_LIMIT,
    ANSWER_STREAM_PENDING_DAYS,
    ANSWER_STREAM_PENDING_MAX,
    ANSWER_STREAM_SCHEDULE_RETRY_SECONDS,
    ANSWER_STREAM_WAIT_SECONDS,
    CURSOR_INVALID_CODE,
    DOMAIN,
    EVENT_NOTIFICATION_ACTION,
)
from .quota import block_delay_seconds, quota_released_signal

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1
_SAVE_DELAY = 5

# Schedule statuses that mean it has not finished sending (a series: is still running).
_SCHEDULE_LIVE = ("scheduled", "sending")
_SCHEDULE_TIMES = ("active_at", "check_at", "busy_until")


def build_answer_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, _STORAGE_VERSION, f"{DOMAIN}.answers.{entry_id}", atomic_writes=True)


def _key_hash(integration_key: str) -> str:
    return hashlib.sha256(integration_key.encode()).hexdigest()[:16]


def _iso(value: datetime) -> str:
    return dt_util.as_utc(value).isoformat()


def _from_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return dt_util.as_utc(parsed) if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)


def _cursor(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _seq(cursor: str | None) -> int | None:
    """The position a cursor names, while cursors are decimal integers; None otherwise."""
    if cursor is not None and cursor.isascii() and cursor.isdigit():
        return int(cursor)
    return None


def older_cursor(stored: str | None, other: str | None) -> str | None:
    """The cursor further back, so reading after it misses the answers of neither.

    Two cursors that do not both read as numbers cannot be ordered; the stream's
    own (``stored``) is kept then.
    """
    if other is None:
        return stored
    if stored is None:
        return other
    a, b = _seq(stored), _seq(other)
    if a is None or b is None:
        return stored
    return other if b < a else stored


def _event_data(entry_id: str, item: dict) -> dict:
    """The event's data, named after the Companion app's mobile_app_notification_action where it can be."""
    action_id = item.get("action_id")
    return {
        "config_entry_id": entry_id,
        "notification_id": item["notification_id"],
        # `action` is the Companion app's name for the tapped action; action_id is PushWard's.
        "action": action_id,
        "action_id": action_id,
        "reply_text": item.get("text"),
        "collapse_id": item.get("collapse_id"),
        "thread_id": item.get("thread_id"),
        "answered_at": item.get("answered_at"),
    }


class AnswerStream:
    """One entry's long-poll on the answer feed, run while an answer may still come."""

    def __init__(self, hass: HomeAssistant, api: PushWardApiClient, entry: ConfigEntry, integration_key: str) -> None:
        self._hass = hass
        self._api = api
        self._entry = entry
        self._key_hash = _key_hash(integration_key)
        self._store = build_answer_store(hass, entry.entry_id)
        self._cursor: str | None = None
        # notification id -> {"sent_at": iso, "cursor": the create response's answer_cursor}
        self._pending: dict[str, dict] = {}
        # schedule id -> {"active_at": its next send (None once it has none), "check_at": when
        # to read it next, "busy_until": the end of the answer window after its last send,
        # "recurring": bool}; isos.
        self._schedules: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopped = False
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._unsub_released: CALLBACK_TYPE | None = None
        self._unsub_ha_stop: CALLBACK_TYPE | None = None

    # --- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        data = await self._store.async_load()
        data = data if isinstance(data, dict) else {}
        # Another key's cursor and sends are not this one's to read: a key's feed
        # holds only the answers to what it sent itself.
        if data.get("key") == self._key_hash:
            self._cursor = _cursor(data.get("cursor"))
            self._pending = {
                nid: {"sent_at": rec["sent_at"], **({"cursor": c} if (c := _cursor(rec.get("cursor"))) else {})}
                for nid, rec in (data.get("pending") or {}).items()
                if isinstance(rec, dict) and _from_iso(rec.get("sent_at")) is not None
            }
            self._schedules = {
                sid: {key: rec.get(key) if _from_iso(rec.get(key)) else None for key in _SCHEDULE_TIMES}
                | {"recurring": bool(rec.get("recurring"))}
                for sid, rec in (data.get("schedules") or {}).items()
                if isinstance(rec, dict) and (_from_iso(rec.get("active_at")) or _from_iso(rec.get("busy_until")))
            }
        self._unsub_released = async_dispatcher_connect(
            self._hass, quota_released_signal(self._entry.entry_id), self._on_quota_released
        )
        self._unsub_ha_stop = self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_ha_stop)
        self._api.on_answerable_send = self.async_track_send
        self._kick()

    async def async_stop(self) -> None:
        if self._unsub_ha_stop is not None:
            self._unsub_ha_stop()
            self._unsub_ha_stop = None
        await self._async_shutdown()

    async def _async_shutdown(self) -> None:
        self._stopped = True
        if self._api.on_answerable_send == self.async_track_send:
            self._api.on_answerable_send = None
        for unsub in (self._unsub_released, self._unsub_timer):
            if unsub is not None:
                unsub()
        self._unsub_released = self._unsub_timer = None
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._store.async_save(self._serialize())

    async def _on_ha_stop(self, _event: Event) -> None:
        # Home Assistant does not unload entries when it stops; drop the long-poll
        # here instead of waiting for it to be canceled.
        self._unsub_ha_stop = None
        await self._async_shutdown()

    @callback
    def _on_quota_released(self, _kind: str) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        """For diagnostics: whether the stream is on and what it waits for, no ids."""
        return {
            "running": self.running,
            "pending": len(self._pending),
            "schedules": len(self._schedules),
            "has_cursor": self._cursor is not None,
        }

    # --- tracking ---------------------------------------------------------

    @callback
    def async_track_send(self, created: dict, scheduled: bool) -> None:
        """Listen for the answer to a send the server records it for (PushWardApiClient.on_answerable_send)."""
        if self._stopped:
            return
        send_id = created.get("id")
        if not isinstance(send_id, int) or isinstance(send_id, bool):
            return
        now = dt_util.utcnow()
        if scheduled:
            active_at = _iso(_from_iso(created.get("send_at")) or now)
            self._schedules[str(send_id)] = {
                "active_at": active_at,
                "check_at": active_at,
                "busy_until": None,
                "recurring": created.get("recurrence") is not None,
            }
        else:
            rec: dict = {"sent_at": _iso(now)}
            if (answer_cursor := _cursor(created.get("answer_cursor"))) is not None:
                rec["cursor"] = answer_cursor
                if self._cursor is None:
                    self._cursor = answer_cursor
            self._pending[str(send_id)] = rec
        self._cap()
        self._save()
        self._kick()

    def _cap(self) -> None:
        """Keep at most ANSWER_STREAM_PENDING_MAX sends, dropping the oldest."""
        excess = len(self._pending) + len(self._schedules) - ANSWER_STREAM_PENDING_MAX
        if excess <= 0:
            return
        epoch = datetime.min.replace(tzinfo=dt_util.UTC)
        entries = [(_from_iso(rec["sent_at"]) or epoch, self._pending, key) for key, rec in self._pending.items()]
        entries += [
            (_from_iso(rec["active_at"]) or _from_iso(rec["busy_until"]) or epoch, self._schedules, key)
            for key, rec in self._schedules.items()
        ]
        entries.sort(key=lambda entry: entry[0])
        for _, table, key in entries[:excess]:
            del table[key]

    def _prune(self, now: datetime) -> None:
        horizon = now - timedelta(days=ANSWER_STREAM_PENDING_DAYS)
        stale = [nid for nid, rec in self._pending.items() if (_from_iso(rec["sent_at"]) or horizon) <= horizon]
        for nid in stale:
            del self._pending[nid]
        ended = []
        for sid, rec in self._schedules.items():
            active_at, busy_until = _from_iso(rec["active_at"]), _from_iso(rec["busy_until"])
            if active_at is None and (busy_until is None or busy_until <= now):
                ended.append(sid)  # a series past the window after its last send
            elif not rec["recurring"] and active_at is not None and active_at <= horizon:
                ended.append(sid)  # never resolved into a notification
        for sid in ended:
            del self._schedules[sid]
        if stale or ended:
            self._save()

    def _active(self, now: datetime) -> bool:
        if self._pending:
            return True
        return any(self._schedule_active(rec, now) for rec in self._schedules.values())

    @staticmethod
    def _schedule_active(rec: dict, now: datetime) -> bool:
        active_at, busy_until = _from_iso(rec["active_at"]), _from_iso(rec["busy_until"])
        return (active_at is not None and active_at <= now) or (busy_until is not None and now < busy_until)

    @callback
    def _kick(self) -> None:
        """Start the long-poll when something can be answered, or wake it when a schedule comes due."""
        if self._stopped or self.running:
            return
        now = dt_util.utcnow()
        self._prune(now)
        if not self._active(now):
            self._arm_timer(now)
            return
        self._task = self._entry.async_create_background_task(
            self._hass, self._async_run(), name=f"{DOMAIN} notification answers"
        )

    def _arm_timer(self, now: datetime) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        upcoming = [t for rec in self._schedules.values() if (t := _from_iso(rec["active_at"])) and t > now]
        if not upcoming:
            return

        @callback
        def _due(_now: datetime) -> None:
            self._unsub_timer = None
            self._kick()

        self._unsub_timer = async_track_point_in_utc_time(self._hass, _due, min(upcoming))

    # --- the loop ---------------------------------------------------------

    async def _async_run(self) -> None:
        loop = asyncio.get_running_loop()
        failures = 0
        plain_until = 0.0
        # Starting (again): read from far enough back for every send still waiting.
        self._rewind()
        while not self._stopped:
            now = dt_util.utcnow()
            self._prune(now)
            if not await self._async_check_schedules(now):
                return
            if not self._active(now):
                self._save()
                self._arm_timer(now)
                return
            hold = 0 if loop.time() < plain_until else ANSWER_STREAM_WAIT_SECONDS
            started = loop.time()
            after = self._cursor
            try:
                if after is None:
                    await self._async_fresh_cursor()
                    continue
                feed = await self._api.list_notification_answers(after, wait=hold, limit=ANSWER_STREAM_PAGE_LIMIT)
            except PushWardQuotaExceededError as err:
                # The gate logs it; the coordinator's release wakes the stream early.
                delay = block_delay_seconds(err.reset_at, None)
            except PushWardRateLimitedError as err:
                # Too many waits held across the account: read without one for a while.
                failures += 1
                plain_until = loop.time() + ANSWER_PLAIN_AFTER_WAIT_LIMIT_SECONDS
                delay = max(err.retry_after, self._backoff(failures))
            except PushWardAuthError:
                _LOGGER.warning("PushWard refused the integration key while reading notification answers")
                self._entry.async_start_reauth(self._hass)
                return
            except PushWardForbiddenError as err:
                # The key lost the notifications permission; the next answerable send tries again.
                _LOGGER.warning("PushWard stopped reading notification answers: %s", err)
                return
            except PushWardApiError as err:
                failures += 1
                if err.code == CURSOR_INVALID_CODE:
                    _LOGGER.warning("PushWard refused the stored answer cursor %r; starting from a new one", after)
                    self._forget_cursor(after)
                    delay = 0 if failures == 1 else self._backoff(failures)
                else:
                    _LOGGER.debug("Reading PushWard notification answers failed: %s", err)
                    delay = self._backoff(failures)
            except Exception:
                _LOGGER.exception("Unexpected error reading PushWard notification answers")
                failures += 1
                delay = self._backoff(failures)
            else:
                failures = 0
                self._consume(after, feed)
                if len(feed["items"]) >= ANSWER_STREAM_PAGE_LIMIT:
                    continue  # a full page: the next one is already waiting
                if hold == 0:
                    delay = ANSWER_PLAIN_POLL_INTERVAL
                else:
                    # A wait that came back early (a server shutting it down) must not spin.
                    delay = max(0.0, ANSWER_MIN_POLL_INTERVAL - (loop.time() - started))
            await self._async_sleep(delay)

    async def _async_sleep(self, delay: float) -> None:
        if delay <= 0 or self._stopped:
            return
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), delay)

    @staticmethod
    def _backoff(failures: int) -> float:
        delay = min(ANSWER_STREAM_BACKOFF_BASE_SECONDS * 2 ** (failures - 1), ANSWER_STREAM_BACKOFF_MAX_SECONDS)
        # Jitter, so installs that lost the server together do not come back in step.
        return delay * (0.5 + random.random() * 0.5)

    def _rewind(self) -> None:
        """Move the cursor back to the oldest answer_cursor of a send still waiting.

        A pending send keeps its answer_cursor only until a read from at or before
        it succeeded, so this only reaches back past the stored cursor for one the
        stream has not covered yet (a fresh cursor taken after it was sent).
        """
        for rec in self._pending.values():
            self._cursor = older_cursor(self._cursor, rec.get("cursor"))

    async def _async_fresh_cursor(self) -> None:
        self._rewind()
        if self._cursor is None:
            feed = await self._api.list_notification_answers()
            self._cursor = feed["cursor"]
            # A send that went out while that was in flight may be older still.
            self._rewind()
        self._save()

    def _forget_cursor(self, refused: str | None) -> None:
        self._cursor = None
        for rec in self._pending.values():
            if rec.get("cursor") == refused:
                del rec["cursor"]
        self._save()

    def _consume(self, after: str, feed: dict) -> None:
        for item in feed["items"]:
            notification_id = item.get("notification_id")
            if not isinstance(notification_id, int) or isinstance(notification_id, bool):
                continue
            self._pending.pop(str(notification_id), None)
            self._hass.bus.async_fire(EVENT_NOTIFICATION_ACTION, _event_data(self._entry.entry_id, item))
        # Every send that was already waiting when this read started is covered from
        # here on; its own answer_cursor is no longer needed after a restart.
        floor = _seq(after)
        for rec in self._pending.values():
            seq = _seq(rec.get("cursor"))
            if "cursor" in rec and (seq is None or floor is None or seq >= floor):
                del rec["cursor"]
        self._cursor = feed["cursor"]
        self._save()

    # --- scheduled sends --------------------------------------------------

    async def _async_check_schedules(self, now: datetime) -> bool:
        """Read the schedules that came due; False when the key was refused."""
        for sid, rec in list(self._schedules.items()):
            active_at, check_at = _from_iso(rec["active_at"]), _from_iso(rec["check_at"])
            if active_at is None or active_at > now or (check_at is not None and check_at > now):
                continue  # not due, or a series that ended and only waits out its last answers
            try:
                row = await self._api.get_scheduled_notification(int(sid))
            except PushWardAuthError:
                _LOGGER.warning("PushWard refused the integration key while reading a scheduled notification")
                self._entry.async_start_reauth(self._hass)
                return False
            except PushWardApiError as err:
                _LOGGER.debug("Reading PushWard scheduled notification %s failed: %s", sid, err)
                rec["check_at"] = _iso(now + timedelta(seconds=ANSWER_STREAM_BACKOFF_MAX_SECONDS))
                continue
            if sid in self._schedules:
                self._apply_schedule(sid, rec, row if isinstance(row, dict) else None, now)
        self._save()
        return True

    def _apply_schedule(self, sid: str, rec: dict, row: dict | None, now: datetime) -> None:
        status = (row or {}).get("status")
        last_sent = _from_iso((row or {}).get("last_sent_at")) or _from_iso((row or {}).get("sent_at"))
        if rec["recurring"] and last_sent is not None:
            rec["busy_until"] = _iso(last_sent + timedelta(days=ANSWER_STREAM_PENDING_DAYS))
        if status in _SCHEDULE_LIVE:
            # Not sent yet (being sent, or late), or a series waiting for its next send:
            # read it again just after that send.
            next_at = _from_iso(row.get("send_at")) or now
            rec["active_at"] = _iso(next_at)
            retry = timedelta(seconds=ANSWER_STREAM_SCHEDULE_RETRY_SECONDS)
            rec["check_at"] = _iso(max(next_at, now) + retry)
            return
        notification_id = (row or {}).get("notification_id")
        if rec["recurring"] and last_sent is not None:
            # The series is over; its last sends can still be answered for a while.
            rec["active_at"] = rec["check_at"] = None
            return
        del self._schedules[sid]
        if status == "sent" and isinstance(notification_id, int) and last_sent is not None:
            # Now a plain pending send: its answer takes it off the list.
            self._pending[str(notification_id)] = {"sent_at": _iso(last_sent)}

    # --- storage ----------------------------------------------------------

    def _serialize(self) -> dict:
        return {
            "key": self._key_hash,
            "cursor": self._cursor,
            "pending": self._pending,
            "schedules": self._schedules,
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._serialize, _SAVE_DELAY)
