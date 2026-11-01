"""Tests for the answer stream behind the pushward_notification_action event."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.pushward.answer_stream import AnswerStream, _key_hash, older_cursor
from custom_components.pushward.api import (
    PushWardApiError,
    PushWardAuthError,
    PushWardForbiddenError,
    PushWardQuotaExceededError,
    PushWardRateLimitedError,
)
from custom_components.pushward.const import (
    ANSWER_STREAM_PENDING_MAX,
    ANSWER_STREAM_WAIT_SECONDS,
    DOMAIN,
    EVENT_NOTIFICATION_ACTION,
)
from custom_components.pushward.quota import quota_released_signal

from .server_contract import assert_valid_answer_feed

NOW = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
KEY = "test-key"
STORE_KEY = f"{DOMAIN}.answers.answers_entry"


class FakeFeed:
    """Stands in for list_notification_answers: each read after a cursor waits for the test to answer it."""

    def __init__(self, fresh: str = "100") -> None:
        self.fresh = fresh
        self.calls: list[tuple[str | None, int]] = []
        self._replies: asyncio.Queue = asyncio.Queue()

    async def __call__(self, after: str | None = None, *, wait: int = 0, limit: int = 100) -> dict:
        self.calls.append((after, wait))
        if after is None:
            return {"items": [], "cursor": self.fresh}
        reply = await self._replies.get()
        if isinstance(reply, BaseException):
            raise reply
        assert_valid_answer_feed(reply)
        return reply

    def reply(self, *items: dict, cursor: str) -> None:
        self._replies.put_nowait({"items": list(items), "cursor": cursor})

    def fail(self, err: BaseException) -> None:
        self._replies.put_nowait(err)


def _answer(notification_id: int = 42, **overrides) -> dict:
    item = {
        "notification_id": notification_id,
        "status": "answered",
        "action_id": "close",
        "text": "in 5 min",
        "answered_at": "2026-11-01T18:00:00Z",
        "collapse_id": "garage",
        "thread_id": "house",
    }
    item.update(overrides)
    return item


async def _until(predicate, what: str) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, entry_id="answers_entry", data={})


@pytest.fixture
def feed() -> FakeFeed:
    return FakeFeed()


@pytest.fixture
def api(feed: FakeFeed) -> MagicMock:
    api = MagicMock()
    api.on_answerable_send = None
    api.list_notification_answers = feed
    api.get_scheduled_notification = AsyncMock(return_value=None)
    return api


@pytest.fixture
async def streams(hass: HomeAssistant, api: MagicMock, entry: MockConfigEntry, freezer):
    """Start answer streams for the test; every one is stopped at teardown."""
    freezer.move_to(NOW)
    started: list[AnswerStream] = []

    async def start(key: str = KEY) -> AnswerStream:
        stream = AnswerStream(hass, api, entry, key)
        await stream.async_start()
        started.append(stream)
        return stream

    with patch("custom_components.pushward.answer_stream.ANSWER_MIN_POLL_INTERVAL", 0):
        yield start
        for stream in started:
            await stream.async_stop()


def _seed(hass_storage: dict, **data) -> None:
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {"key": _key_hash(KEY), **data},
    }


def _stored(hass_storage: dict) -> dict:
    return hass_storage[STORE_KEY]["data"]


# --- lifecycle ---------------------------------------------------------------


async def test_nothing_pending_holds_no_connection(streams, api, feed) -> None:
    stream = await streams()

    assert api.on_answerable_send == stream.async_track_send
    assert not stream.running
    assert feed.calls == []


async def test_answerable_send_starts_the_loop_and_fires_the_event(hass: HomeAssistant, streams, feed) -> None:
    events = async_capture_events(hass, EVENT_NOTIFICATION_ACTION)
    stream = await streams()

    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")
    assert feed.calls == [("57", ANSWER_STREAM_WAIT_SECONDS)]
    assert stream.running

    feed.reply(_answer(42), cursor="58")
    await _until(lambda: not stream.running, "the loop to go idle")

    assert [event.data for event in events] == [
        {
            "config_entry_id": "answers_entry",
            "notification_id": 42,
            "action": "close",
            "action_id": "close",
            "reply_text": "in 5 min",
            "collapse_id": "garage",
            "thread_id": "house",
            "answered_at": "2026-11-01T18:00:00Z",
        }
    ]
    assert stream.snapshot() == {"running": False, "pending": 0, "schedules": 0, "has_cursor": True}


async def test_event_fires_for_every_answer_in_the_feed(hass: HomeAssistant, streams, feed) -> None:
    """The feed holds answers to anything this key sent, tracked or not: each one is an event."""
    events = async_capture_events(hass, EVENT_NOTIFICATION_ACTION)
    stream = await streams()
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")

    feed.reply(_answer(41, action_id="pw_ack", text=None, collapse_id=None, thread_id=None), _answer(7), cursor="60")
    await _until(lambda: len(feed.calls) == 2, "the next read")

    assert [(e.data["notification_id"], e.data["action"], e.data["reply_text"]) for e in events] == [
        (41, "pw_ack", None),
        (7, "close", "in 5 min"),
    ]
    assert events[0].data["collapse_id"] is None
    assert feed.calls[1] == ("60", ANSWER_STREAM_WAIT_SECONDS)
    assert stream.running  # 42 is still unanswered


async def test_full_page_reads_the_next_one_at_once(hass: HomeAssistant, streams, feed) -> None:
    stream = await streams()
    stream._async_sleep = AsyncMock()
    stream.async_track_send({"id": 1000, "answerable": True, "answer_cursor": "1"}, False)
    await _until(lambda: feed.calls, "the first read")

    feed.reply(*(_answer(n) for n in range(1, 101)), cursor="101")
    await _until(lambda: len(feed.calls) == 2, "the next page")

    stream._async_sleep.assert_not_awaited()
    assert feed.calls[1][0] == "101"


async def test_unload_stops_the_loop_and_keeps_the_pending_send(
    hass: HomeAssistant, streams, feed, hass_storage
) -> None:
    stream = await streams()
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")

    await stream.async_stop()

    assert not stream.running
    stored = _stored(hass_storage)
    assert stored["cursor"] == "57"
    assert stored["pending"] == {"42": {"sent_at": NOW.isoformat(), "cursor": "57"}}
    # A send after the stop is not tracked.
    stream.async_track_send({"id": 43, "answerable": True}, False)
    assert "43" not in stream._pending


async def test_home_assistant_stop_ends_the_loop(hass: HomeAssistant, streams, feed) -> None:
    stream = await streams()
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert not stream.running


async def test_restart_resumes_from_the_stored_cursor(hass: HomeAssistant, streams, feed, hass_storage) -> None:
    _seed(hass_storage, cursor="60", pending={"42": {"sent_at": NOW.isoformat()}})

    stream = await streams()
    await _until(lambda: feed.calls, "the first read")

    assert feed.calls == [("60", ANSWER_STREAM_WAIT_SECONDS)]
    assert stream.running


async def test_restart_reaches_back_to_an_uncovered_answer_cursor(streams, feed, hass_storage) -> None:
    """A send whose answer_cursor no read has covered yet starts the stream there (min of the two)."""
    _seed(hass_storage, cursor="60", pending={"42": {"sent_at": NOW.isoformat(), "cursor": "57"}})

    await streams()
    await _until(lambda: feed.calls, "the first read")

    assert feed.calls[0] == ("57", ANSWER_STREAM_WAIT_SECONDS)


async def test_a_covered_answer_cursor_does_not_replay_after_a_restart(
    hass: HomeAssistant, streams, feed, hass_storage
) -> None:
    """Once a read from at or before a send's answer_cursor succeeded, a restart does not go back to it."""
    events = async_capture_events(hass, EVENT_NOTIFICATION_ACTION)
    stream = await streams()
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    stream.async_track_send({"id": 43, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")
    feed.reply(_answer(42), cursor="58")
    await _until(lambda: len(feed.calls) == 2, "the next read")
    await stream.async_stop()
    assert _stored(hass_storage)["pending"] == {"43": {"sent_at": NOW.isoformat()}}

    await streams()
    await _until(lambda: len(feed.calls) == 3, "the read after the restart")

    assert feed.calls[2] == ("58", ANSWER_STREAM_WAIT_SECONDS)
    assert len(events) == 1


async def test_no_stored_cursor_takes_the_answer_cursor(streams, feed) -> None:
    stream = await streams()
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")

    assert feed.calls == [("57", ANSWER_STREAM_WAIT_SECONDS)]


async def test_no_cursor_at_all_starts_from_a_fresh_one(streams, feed) -> None:
    """A server without answer_cursor: the first read (no after) gives the cursor to start from."""
    stream = await streams()
    stream.async_track_send({"id": 42, "answerable": True}, False)
    await _until(lambda: len(feed.calls) == 2, "the fresh cursor and the first read")

    assert feed.calls == [(None, 0), ("100", ANSWER_STREAM_WAIT_SECONDS)]


async def test_another_key_starts_over(streams, feed, hass_storage) -> None:
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {"key": _key_hash("old-key"), "cursor": "60", "pending": {"42": {"sent_at": NOW.isoformat()}}},
    }

    stream = await streams()

    assert not stream.running
    assert stream.snapshot() == {"running": False, "pending": 0, "schedules": 0, "has_cursor": False}
    assert feed.calls == []


# --- cursors -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "other", "expected"),
    [
        ("60", "57", "57"),
        ("57", "60", "57"),
        ("9", "10", "9"),  # numeric, not string, order
        (None, "57", "57"),
        ("60", None, "60"),
        ("opaque", "57", "opaque"),  # cannot be ordered: the stream's own wins
        ("60", "v2:abc", "60"),
        ("-1", "57", "-1"),
    ],
)
def test_older_cursor(stored, other, expected) -> None:
    assert older_cursor(stored, other) == expected


async def test_refused_cursor_starts_from_a_fresh_one(hass: HomeAssistant, streams, feed, caplog) -> None:
    stream = await streams()
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")

    feed.fail(PushWardApiError("malformed cursor", status_code=400, code="cursor.invalid"))
    await _until(lambda: len(feed.calls) == 3, "the fresh cursor and the read after it")

    assert feed.calls == [("57", ANSWER_STREAM_WAIT_SECONDS), (None, 0), ("100", ANSWER_STREAM_WAIT_SECONDS)]
    assert "refused the stored answer cursor" in caplog.text
    assert stream._pending["42"] == {"sent_at": NOW.isoformat()}


# --- errors, backoff and quota ------------------------------------------------


async def test_errors_back_off_exponentially_with_jitter(streams, feed) -> None:
    stream = await streams()
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    stream._async_sleep = record
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    for n in range(1, 5):
        feed.fail(PushWardApiError("boom", status_code=503))
        await _until(lambda n=n: len(delays) == n, f"backoff {n}")

    for n, delay in enumerate(delays, start=1):
        cap = 2 * 2 ** (n - 1)
        assert cap / 2 <= delay <= cap
    # A success resets the streak.
    feed.reply(cursor="57")
    await _until(lambda: len(delays) == 5, "the delay after a success")
    feed.fail(PushWardApiError("boom", status_code=503))
    await _until(lambda: len(delays) == 6, "the next backoff")
    assert 1 <= delays[5] <= 2


async def test_backoff_is_capped(streams) -> None:
    with patch("custom_components.pushward.answer_stream.random.random", return_value=1.0):
        assert AnswerStream._backoff(1) == 2
        assert AnswerStream._backoff(20) == 300


async def test_wait_limit_429_reads_without_waiting_for_a_while(streams, feed) -> None:
    stream = await streams()
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    stream._async_sleep = record
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")

    feed.fail(PushWardRateLimitedError("too many concurrent waits", retry_after=7))
    await _until(lambda: len(feed.calls) == 2, "the read after the 429")

    assert delays[0] >= 7
    assert feed.calls[1] == ("57", 0)  # a plain read


async def test_quota_refusal_pauses_until_the_gate_releases(hass: HomeAssistant, streams, feed, entry) -> None:
    stream = await streams()
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")

    feed.fail(PushWardQuotaExceededError("notifications", reset_at=NOW + timedelta(hours=6)))
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(feed.calls) == 1  # paused, not retrying

    async_dispatcher_send(hass, quota_released_signal(entry.entry_id), "notifications")
    await _until(lambda: len(feed.calls) == 2, "the read after the release")
    assert stream.running


async def test_auth_error_stops_and_asks_for_reauth(streams, feed, entry) -> None:
    with patch.object(entry, "async_start_reauth") as reauth:
        stream = await streams()
        stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
        await _until(lambda: feed.calls, "the first read")

        feed.fail(PushWardAuthError("Invalid integration key", status_code=401))
        await _until(lambda: not stream.running, "the loop to stop")

    reauth.assert_called_once()
    assert "42" in stream._pending


async def test_forbidden_stops_until_the_next_answerable_send(streams, feed) -> None:
    stream = await streams()
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")

    feed.fail(PushWardForbiddenError("no notifications permission", status_code=403))
    await _until(lambda: not stream.running, "the loop to stop")

    stream.async_track_send({"id": 43, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: len(feed.calls) == 2, "the read after the next send")
    assert stream.running


# --- the pending set ----------------------------------------------------------


async def test_sends_older_than_seven_days_are_pruned(streams, feed, hass_storage) -> None:
    _seed(
        hass_storage,
        cursor="60",
        pending={
            "41": {"sent_at": (NOW - timedelta(days=8)).isoformat()},
            "42": {"sent_at": (NOW - timedelta(days=7, seconds=1)).isoformat()},
        },
    )

    stream = await streams()

    assert not stream.running
    assert stream._pending == {}
    assert feed.calls == []


async def test_a_pending_send_expires_while_the_loop_runs(streams, feed, freezer) -> None:
    stream = await streams()
    stream.async_track_send({"id": 42, "answerable": True, "answer_cursor": "57"}, False)
    await _until(lambda: feed.calls, "the first read")

    freezer.move_to(NOW + timedelta(days=7, minutes=1))
    feed.reply(cursor="57")
    await _until(lambda: not stream.running, "the loop to go idle")

    assert stream._pending == {}
    assert len(feed.calls) == 1


async def test_pending_set_is_capped_dropping_the_oldest(streams, freezer) -> None:
    stream = await streams()
    stream._kick = MagicMock()  # count only, no loop
    for n in range(ANSWER_STREAM_PENDING_MAX + 5):
        freezer.move_to(NOW + timedelta(seconds=n))
        stream.async_track_send({"id": n, "answerable": True}, False)

    assert len(stream._pending) == ANSWER_STREAM_PENDING_MAX
    assert "0" not in stream._pending
    assert "4" not in stream._pending
    assert "5" in stream._pending


async def test_bad_ids_are_ignored(streams) -> None:
    stream = await streams()
    for created in ({"answerable": True}, {"id": "42", "answerable": True}, {"id": True, "answerable": True}):
        stream.async_track_send(created, False)

    assert stream._pending == {}
    assert not stream.running


# --- scheduled sends ------------------------------------------------------------


async def test_a_scheduled_send_starts_the_stream_when_it_comes_due(
    hass: HomeAssistant, streams, feed, api, freezer
) -> None:
    stream = await streams()
    send_at = NOW + timedelta(hours=1)
    stream.async_track_send({"id": 7, "status": "scheduled", "send_at": send_at.isoformat()}, True)

    assert not stream.running
    assert feed.calls == []

    api.get_scheduled_notification.return_value = {
        "id": 7,
        "status": "sent",
        "send_at": send_at.isoformat(),
        "sent_at": send_at.isoformat(),
        "notification_id": 99,
    }
    freezer.move_to(send_at)
    async_fire_time_changed(hass, send_at)
    await _until(lambda: len(feed.calls) == 2, "the fresh cursor and the first read")

    api.get_scheduled_notification.assert_awaited_once_with(7)
    assert stream._schedules == {}
    assert stream._pending == {"99": {"sent_at": send_at.isoformat()}}

    feed.reply(_answer(99), cursor="101")
    await _until(lambda: not stream.running, "the loop to go idle")


async def test_a_scheduled_send_still_being_sent_is_read_again(hass: HomeAssistant, streams, feed, api) -> None:
    stream = await streams()
    api.get_scheduled_notification.return_value = {"id": 7, "status": "sending", "send_at": NOW.isoformat()}
    stream.async_track_send({"id": 7, "status": "scheduled", "send_at": NOW.isoformat()}, True)
    await _until(lambda: len(feed.calls) == 2, "the first read")

    assert stream._schedules["7"]["check_at"] == (NOW + timedelta(seconds=60)).isoformat()
    assert stream.running


async def test_a_canceled_scheduled_send_is_dropped(hass: HomeAssistant, streams, feed, api) -> None:
    stream = await streams()
    api.get_scheduled_notification.return_value = {"id": 7, "status": "canceled", "send_at": NOW.isoformat()}
    stream.async_track_send({"id": 7, "status": "scheduled", "send_at": NOW.isoformat()}, True)
    await _until(lambda: not stream.running, "the loop to go idle")

    assert stream._schedules == {}
    assert feed.calls == []


async def test_a_series_listens_after_each_send_until_it_ends(hass: HomeAssistant, streams, feed, api, freezer) -> None:
    stream = await streams()
    stream._async_sleep = AsyncMock()
    first = NOW
    second = NOW + timedelta(days=30)
    api.get_scheduled_notification.return_value = {
        "id": 8,
        "status": "scheduled",
        "send_at": second.isoformat(),
        "last_sent_at": first.isoformat(),
        "notification_id": 99,
        "recurrence": {"cron": "0 18 1 * *"},
    }
    stream.async_track_send(
        {"id": 8, "status": "scheduled", "send_at": first.isoformat(), "recurrence": {"cron": "0 18 1 * *"}}, True
    )
    await _until(lambda: len(feed.calls) == 2, "the first read")
    rec = stream._schedules["8"]
    assert rec["active_at"] == second.isoformat()
    assert rec["busy_until"] == (first + timedelta(days=7)).isoformat()

    # A week after the send, nothing can be answered until the next one: idle.
    freezer.move_to(first + timedelta(days=7, minutes=1))
    feed.reply(cursor="100")
    await _until(lambda: not stream.running, "the loop to go idle")
    assert stream._unsub_timer is not None

    # The series ends: its last send stays answerable for a week, then the entry goes.
    api.get_scheduled_notification.return_value = {
        "id": 8,
        "status": "canceled",
        "send_at": second.isoformat(),
        "last_sent_at": second.isoformat(),
    }
    freezer.move_to(second + timedelta(minutes=2))
    async_fire_time_changed(hass, second + timedelta(minutes=2))
    await _until(lambda: len(feed.calls) == 3, "the read after the next send")
    assert stream._schedules["8"]["active_at"] is None
    assert stream._schedules["8"]["busy_until"] == (second + timedelta(days=7)).isoformat()

    freezer.move_to(second + timedelta(days=8))
    feed.reply(cursor="100")
    await _until(lambda: not stream.running, "the loop to go idle")
    assert stream._schedules == {}


async def test_a_schedule_read_error_is_retried_later(hass: HomeAssistant, streams, feed, api) -> None:
    stream = await streams()
    api.get_scheduled_notification.side_effect = PushWardApiError("boom", status_code=503)
    stream.async_track_send({"id": 7, "status": "scheduled", "send_at": NOW.isoformat()}, True)
    await _until(lambda: len(feed.calls) == 2, "the first read")

    assert stream._schedules["7"]["check_at"] == (NOW + timedelta(seconds=300)).isoformat()


async def test_schedules_survive_a_restart(streams, feed, hass_storage) -> None:
    later = NOW + timedelta(hours=2)
    _seed(
        hass_storage,
        cursor="60",
        schedules={"7": {"active_at": later.isoformat(), "check_at": later.isoformat(), "recurring": False}},
    )

    stream = await streams()

    assert not stream.running
    assert stream._schedules["7"] == {
        "active_at": later.isoformat(),
        "check_at": later.isoformat(),
        "busy_until": None,
        "recurring": False,
    }
    assert stream._unsub_timer is not None
