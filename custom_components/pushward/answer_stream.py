"""The answer stream: a pushward_notification_action event for every answer.

Each config entry reads GET /notifications/answers, the server's feed of the
answers to what this integration key sent, with a long-poll, and fires
EVENT_NOTIFICATION_ACTION for each one. It covers every way a notification goes
out (send_notification, the notify entity, the to-do Done button), because the
API client reports each answerable send here. Each answer also goes out on
notification_answer_signal; the to-do reminders take their Done taps from it.

The long-poll runs only while something can still be answered: a send from the
last ANSWER_STREAM_PENDING_DAYS days that has no answer yet. Most installs never
send one, so they hold no connection. A scheduled send has no notification id
until it goes out, so its schedule is tracked instead: it starts the stream when
it comes due and is read just after, to learn the notification it became, which
is then waited for like any other send. A repeating one does that at each send.

The cursor and the pending sends live in .storage, so a restart picks up where
the stream stopped. An answer read late that way fires only when it is to a send
still waited for, or was given in the last ANSWER_STREAM_FRESH_SECONDS: a tap from
last week must not run an automation now. Cursors are opaque strings that are
decimal integers today: the stream compares them as such to start far enough
back, and keeps its own when one is not a number.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import random
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
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
    ANSWER_STATUS_ANSWERED,
    ANSWER_STREAM_ANSWERED_MAX,
    ANSWER_STREAM_BACKOFF_BASE_SECONDS,
    ANSWER_STREAM_BACKOFF_MAX_SECONDS,
    ANSWER_STREAM_EARLY_RETURN_JITTER_SECONDS,
    ANSWER_STREAM_FRESH_SECONDS,
    ANSWER_STREAM_MIN_JITTER_SECONDS,
    ANSWER_STREAM_PAGE_LIMIT,
    ANSWER_STREAM_PENDING_DAYS,
    ANSWER_STREAM_PENDING_MAX,
    ANSWER_STREAM_PLAIN_MAX_SECONDS,
    ANSWER_STREAM_SCHEDULE_FIRST_READ_SECONDS,
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


def notification_answer_signal(entry_id: str) -> str:
    """Dispatcher signal for each answer the entry's stream reads.

    Sent with the feed item and the id of the schedule the notification came
    from, when the stream knows it (else None).
    """
    return f"{DOMAIN}_notification_answer_{entry_id}"


def _key_hash(integration_key: str) -> str:
    return hashlib.sha256(integration_key.encode()).hexdigest()[:16]


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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


def _load_pending(rec: dict) -> dict:
    loaded = {"sent_at": rec["sent_at"]}
    if (cursor := _cursor(rec.get("cursor"))) is not None:
        loaded["cursor"] = cursor
    if _is_id(rec.get("schedule_id")):
        loaded["schedule_id"] = rec["schedule_id"]
    return loaded


def _load_schedule(rec: dict) -> dict:
    loaded: dict = {key: rec.get(key) if _from_iso(rec.get(key)) else None for key in _SCHEDULE_TIMES}
    loaded["recurring"] = bool(rec.get("recurring"))
    if (cursor := _cursor(rec.get("cursor"))) is not None:
        loaded["cursor"] = cursor
    if _is_id(rec.get("last_nid")):
        loaded["last_nid"] = rec["last_nid"]
    return loaded


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
        # notification id -> {"sent_at": iso, "cursor": the create response's answer_cursor
        # (or its schedule's), "schedule_id": the schedule it came from}
        self._pending: dict[str, dict] = {}
        # schedule id -> {"active_at": its next send (None once it has none), "check_at": when
        # to read it next, "busy_until": the end of the answer window after a send whose
        # notification it could not learn, "recurring": bool, "cursor": the feed's position
        # when it was scheduled, if the stream had none, "last_nid": the latest notification
        # it became}; isos.
        self._schedules: dict[str, dict] = {}
        # notification id -> action id of the latest answers read, oldest first; persisted.
        self._answered: dict[str, str | None] = {}
        # notification id -> event data of the answers among those that were read late and
        # not fired, in case their schedule turns out to be one waited for.
        self._skipped: dict[str, dict] = {}
        # Schedules waiting for the cursor read that _take_schedule_cursor started.
        self._awaiting_cursor: set[str] = set()
        self._cursor_task: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopped = False
        # The loop gave up (the key was refused) until something starts it again.
        self._halted = False
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
                nid: _load_pending(rec)
                for nid, rec in (data.get("pending") or {}).items()
                if isinstance(rec, dict) and _from_iso(rec.get("sent_at")) is not None
            }
            self._schedules = {
                sid: _load_schedule(rec)
                for sid, rec in (data.get("schedules") or {}).items()
                if isinstance(rec, dict) and (_from_iso(rec.get("active_at")) or _from_iso(rec.get("busy_until")))
            }
            answered = data.get("answered")
            if isinstance(answered, dict):
                kept = [(nid, action) for nid, action in answered.items() if action is None or isinstance(action, str)]
                self._answered = dict(kept[-ANSWER_STREAM_ANSWERED_MAX:])
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
        for task in (self._task, self._cursor_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = self._cursor_task = None
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

    def delivers_answer(self, schedule_id: Any, notification_id: Any) -> bool:
        """Whether the answer to this send will come on notification_answer_signal.

        True while the stream waits for it: the schedule is tracked, or the
        notification is pending. A reader of its own can then skip it.
        """
        if self._stopped or self._halted:
            return False
        if _is_id(notification_id) and str(notification_id) in self._pending:
            return True
        if not _is_id(schedule_id):
            return False
        if str(schedule_id) in self._schedules:
            return True
        return any(rec.get("schedule_id") == schedule_id for rec in self._pending.values())

    # --- tracking ---------------------------------------------------------

    @callback
    def async_track_send(self, created: dict, scheduled: bool) -> None:
        """Listen for the answer to a send the server records it for (PushWardApiClient.on_answerable_send)."""
        if self._stopped:
            return
        send_id = created.get("id")
        if not _is_id(send_id):
            return
        now = dt_util.utcnow()
        if scheduled:
            send_at = _from_iso(created.get("send_at")) or now
            self._schedules[str(send_id)] = {
                "active_at": _iso(send_at),
                "check_at": _iso(send_at + timedelta(seconds=ANSWER_STREAM_SCHEDULE_FIRST_READ_SECONDS)),
                "busy_until": None,
                "recurring": created.get("recurrence") is not None,
            }
            if self._cursor is None:
                self._take_schedule_cursor(str(send_id))
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

    @callback
    def _take_schedule_cursor(self, schedule_id: str) -> None:
        """Note the feed's position now for a schedule made while the stream has no cursor.

        Its answers come after it goes out, so after this cursor: a stream that
        first runs once it went out (Home Assistant was down then) still reads
        them. A schedule response carries no answer_cursor of its own.
        """
        self._awaiting_cursor.add(schedule_id)
        if self._cursor_task is not None and not self._cursor_task.done():
            return  # the read under way is older than this schedule, so it covers it too
        self._cursor_task = self._entry.async_create_background_task(
            self._hass, self._async_schedule_cursor(), name=f"{DOMAIN} notification answer cursor"
        )

    async def _async_schedule_cursor(self) -> None:
        try:
            feed = await self._api.list_notification_answers()
        except Exception as err:  # best effort: the stream takes a cursor of its own when it runs
            _LOGGER.debug("Reading the PushWard answer cursor for a scheduled notification failed: %s", err)
            self._awaiting_cursor.clear()
            return
        cursor = feed["cursor"]
        for sid in self._awaiting_cursor:
            if (rec := self._schedules.get(sid)) is not None:
                rec.setdefault("cursor", cursor)
        self._awaiting_cursor.clear()
        if self._cursor is None:
            self._cursor = cursor
        self._save()

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
        self._halted = False
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
        # 429s on a wait since the last wait that went through.
        wait_refusals = 0
        plain_until = 0.0
        # Starting (again): read from far enough back for every send still waiting.
        self._rewind()
        while not self._stopped:
            now = dt_util.utcnow()
            self._prune(now)
            if not await self._async_check_schedules(now):
                self._halted = True
                return
            if not self._active(now):
                self._save()
                self._arm_timer(now)
                return
            hold = 0 if loop.time() < plain_until else self._hold(now)
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
                # Too many waits held across the account: read without one for a while,
                # longer each time the waits are refused again.
                failures += 1
                if hold:
                    wait_refusals += 1
                plain_until = loop.time() + self._plain_span(wait_refusals)
                delay = max(err.retry_after, self._backoff(failures))
            except PushWardAuthError:
                _LOGGER.warning("PushWard refused the integration key while reading notification answers")
                self._halted = True
                self._entry.async_start_reauth(self._hass)
                return
            except PushWardForbiddenError as err:
                # The key lost the notifications permission; the next answerable send tries again.
                _LOGGER.warning("PushWard stopped reading notification answers: %s", err)
                self._halted = True
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
                if hold:
                    wait_refusals = 0
                self._consume(after, feed)
                if len(feed["items"]) >= ANSWER_STREAM_PAGE_LIMIT:
                    continue  # a full page: the next one is already waiting
                if hold == 0:
                    delay = ANSWER_PLAIN_POLL_INTERVAL
                else:
                    # A wait that came back early (a server shutting it down) must not spin.
                    elapsed = loop.time() - started
                    delay = max(0.0, ANSWER_MIN_POLL_INTERVAL - elapsed)
                    if not feed["items"] and elapsed < hold - 1:
                        # Nor bring back every reader that server held at the same instant.
                        delay += random.random() * ANSWER_STREAM_EARLY_RETURN_JITTER_SECONDS
            await self._async_sleep(delay)

    def _hold(self, now: datetime) -> int:
        """The next read's wait: the longest allowed, cut short to read a schedule when it is due."""
        checks = [
            t
            for rec in self._schedules.values()
            if rec["active_at"] is not None and (t := _from_iso(rec["check_at"])) is not None and t > now
        ]
        if not checks:
            return ANSWER_STREAM_WAIT_SECONDS
        until = math.ceil((min(checks) - now).total_seconds())
        return max(1, min(ANSWER_STREAM_WAIT_SECONDS, until))

    async def _async_sleep(self, delay: float) -> None:
        if delay <= 0 or self._stopped:
            return
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), delay)

    @staticmethod
    def _backoff(failures: int) -> float:
        delay = min(ANSWER_STREAM_BACKOFF_BASE_SECONDS * 2 ** (failures - 1), ANSWER_STREAM_BACKOFF_MAX_SECONDS)
        # Jitter, so installs that lost the server together do not come back in step;
        # never narrower than ANSWER_STREAM_MIN_JITTER_SECONDS, even on the first retry.
        return delay / 2 + random.random() * max(delay / 2, ANSWER_STREAM_MIN_JITTER_SECONDS)

    @staticmethod
    def _plain_span(wait_refusals: int) -> float:
        """How long to read without waiting after a 429, the wait_refusals-th on a wait in a row."""
        span = ANSWER_PLAIN_AFTER_WAIT_LIMIT_SECONDS * 2 ** max(0, wait_refusals - 1)
        return min(span, ANSWER_STREAM_PLAIN_MAX_SECONDS)

    def _rewind(self) -> None:
        """Move the cursor back to the oldest cursor of a send or schedule still waiting.

        A pending send keeps its answer_cursor (a schedule, the cursor taken when
        it was made) only until a read from at or before it succeeded, so this
        only reaches back past the stored cursor for one the stream has not
        covered yet (a fresh cursor taken after it was sent).
        """
        for rec in (*self._pending.values(), *self._schedules.values()):
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
        for rec in (*self._pending.values(), *self._schedules.values()):
            if rec.get("cursor") == refused:
                del rec["cursor"]
        self._save()

    def _consume(self, after: str, feed: dict) -> None:
        fresh_since = dt_util.utcnow() - timedelta(seconds=ANSWER_STREAM_FRESH_SECONDS)
        for item in feed["items"]:
            notification_id = item.get("notification_id")
            if not _is_id(notification_id):
                continue
            key = str(notification_id)
            waited = self._pending.pop(key, None)
            self._remember_answer(key, item.get("action_id"))
            data = _event_data(self._entry.entry_id, item)
            answered_at = _from_iso(item.get("answered_at"))
            if waited is not None or (answered_at is not None and answered_at >= fresh_since):
                self._hass.bus.async_fire(EVENT_NOTIFICATION_ACTION, data)
            else:
                # Read late (the stream was off), to a send nothing waits for: firing it
                # now would run an automation at a moment nobody chose.
                self._skipped[key] = data
                _LOGGER.debug(
                    "Not firing the answer to PushWard notification %s given at %s: read late, no send waits for it",
                    notification_id,
                    item.get("answered_at"),
                )
            async_dispatcher_send(
                self._hass,
                notification_answer_signal(self._entry.entry_id),
                item,
                (waited or {}).get("schedule_id"),
            )
        # Every send that was already waiting when this read started is covered from
        # here on; its own cursor is no longer needed after a restart.
        changed = bool(feed["items"]) or feed["cursor"] != self._cursor
        floor = _seq(after)
        for rec in (*self._pending.values(), *self._schedules.values()):
            seq = _seq(rec.get("cursor"))
            if "cursor" in rec and (seq is None or floor is None or seq >= floor):
                del rec["cursor"]
                changed = True
        self._cursor = feed["cursor"]
        # A read that brought nothing new writes nothing: a long-poll comes back
        # every 25 seconds, and Home Assistant often runs from an SD card.
        if changed:
            self._save()

    def _remember_answer(self, key: str, action_id: Any) -> None:
        self._answered.pop(key, None)
        self._answered[key] = action_id if isinstance(action_id, str) else None
        while len(self._answered) > ANSWER_STREAM_ANSWERED_MAX:
            oldest = next(iter(self._answered))
            del self._answered[oldest]
            self._skipped.pop(oldest, None)

    # --- scheduled sends --------------------------------------------------

    async def _async_check_schedules(self, now: datetime) -> bool:
        """Read the schedules that came due; False when the key was refused."""
        read = False
        for sid, rec in list(self._schedules.items()):
            active_at, check_at = _from_iso(rec["active_at"]), _from_iso(rec["check_at"])
            if active_at is None or active_at > now or (check_at is not None and check_at > now):
                continue  # not due, or a series that ended and only waits out its last answers
            read = True
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
        if read:
            self._cap()
            self._save()
        return True

    def _apply_schedule(self, sid: str, rec: dict, row: dict | None, now: datetime) -> None:
        row = row or {}
        status = row.get("status")
        last_sent = _from_iso(row.get("last_sent_at")) or _from_iso(row.get("sent_at"))
        notification_id = row["notification_id"] if _is_id(row.get("notification_id")) else None
        if rec["recurring"] and last_sent is not None:
            if notification_id is not None:
                # Its latest send is waited for like a one-shot: answered, the stream can idle.
                self._track_sent(sid, rec, notification_id, last_sent, now)
            else:
                # A send the stream cannot name: listen for the window after it.
                rec["busy_until"] = _iso(last_sent + timedelta(days=ANSWER_STREAM_PENDING_DAYS))
        if status in _SCHEDULE_LIVE:
            next_at = _from_iso(row.get("send_at")) or now
            rec["active_at"] = _iso(next_at)
            if next_at > now:
                # A series waiting for its next send: read it just after that one.
                rec["check_at"] = _iso(next_at + timedelta(seconds=ANSWER_STREAM_SCHEDULE_FIRST_READ_SECONDS))
            else:
                # Not sent yet (being sent, or late): read it again in a while.
                rec["check_at"] = _iso(now + timedelta(seconds=ANSWER_STREAM_SCHEDULE_RETRY_SECONDS))
            return
        if rec["recurring"] and last_sent is not None:
            # The series is over; a send it could not name can still be answered for a while.
            busy_until = _from_iso(rec["busy_until"])
            if busy_until is not None and busy_until > now:
                rec["active_at"] = rec["check_at"] = None
            else:
                del self._schedules[sid]
            return
        del self._schedules[sid]
        if status == "sent" and notification_id is not None and last_sent is not None:
            # Now a plain pending send: its answer takes it off the list.
            self._track_sent(sid, rec, notification_id, last_sent, now)

    def _track_sent(self, sid: str, rec: dict, notification_id: int, sent_at: datetime, now: datetime) -> None:
        """Wait for the notification a schedule became, unless its answer was read already."""
        if rec.get("last_nid") == notification_id:
            return  # a series read again before its next send
        rec["last_nid"] = notification_id
        if sent_at <= now - timedelta(days=ANSWER_STREAM_PENDING_DAYS):
            return  # past the answer window
        key = str(notification_id)
        if key in self._answered:
            # Answered before the schedule read as sent (the server's sweep was slow).
            # The event fired then, unless the answer was read late; it is waited for now.
            if (data := self._skipped.pop(key, None)) is not None:
                self._hass.bus.async_fire(EVENT_NOTIFICATION_ACTION, data)
            action_id = self._answered[key]
            answer = {"notification_id": notification_id, "status": ANSWER_STATUS_ANSWERED, "action_id": action_id}
            async_dispatcher_send(self._hass, notification_answer_signal(self._entry.entry_id), answer, int(sid))
            return
        pending: dict = {"sent_at": _iso(sent_at), "schedule_id": int(sid)}
        if (cursor := rec.get("cursor")) is not None:
            pending["cursor"] = cursor
        self._pending[key] = pending

    # --- storage ----------------------------------------------------------

    def _serialize(self) -> dict:
        return {
            "key": self._key_hash,
            "cursor": self._cursor,
            "pending": self._pending,
            "schedules": self._schedules,
            "answered": self._answered,
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._serialize, _SAVE_DELAY)
