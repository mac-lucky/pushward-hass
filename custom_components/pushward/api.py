"""Async PushWard API client."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import aiohttp
from homeassistant.util import dt as dt_util

from .const import (
    ANSWER_FAILURE_BUDGET,
    ANSWER_LONG_POLL_SECONDS,
    ANSWER_MAX_CONCURRENT_WAITS,
    ANSWER_MIN_POLL_INTERVAL,
    ANSWER_PLAIN_AFTER_WAIT_LIMIT_SECONDS,
    ANSWER_PLAIN_POLL_INTERVAL,
    ANSWER_REQUEST_MARGIN_SECONDS,
    ANSWER_STATUS_ANSWERED,
    ANSWER_STATUS_PENDING,
    MAX_CONCURRENT_REQUESTS,
    MAX_RETRIES,
    QUOTA_KIND_EMAILS,
    QUOTA_KIND_LIVE_ACTIVITY_UPDATES,
    QUOTA_KIND_NOTIFICATIONS,
    QUOTA_KIND_WIDGET_UPDATES,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    SCHEDULED_LIST_MAX_PAGES,
    SCHEDULED_LIST_PAGE_SIZE,
)
from .e2e import seal

if TYPE_CHECKING:
    from .quota import QuotaGate

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Problem `code` the server sends when a metered quota is exhausted (as opposed to
# `rate_limit.exceeded`, the per-client request limiter, which is worth retrying).
QUOTA_EXCEEDED_CODE = "quota.exceeded"


def parse_http_date(header: str | None) -> datetime | None:
    """Parse an RFC 7231 date header to an aware datetime (None when absent/invalid)."""
    if not header:
        return None
    try:
        parsed = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    # A `-0000` zone parses to a naive datetime; treat it as UTC like the rest.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt_util.UTC)


def _aware_isoformat(value: datetime, field: str) -> str:
    """RFC 3339 for a timezone-aware datetime; a naive one would be read as UTC server-side."""
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.isoformat()


def _recurrence_payload(recurrence: dict) -> dict:
    """The wire form of a recurrence rule: None fields dropped, until as RFC 3339."""
    payload = {key: val for key, val in recurrence.items() if val is not None}
    if isinstance(payload.get("until"), datetime):
        payload["until"] = _aware_isoformat(payload["until"], "recurrence.until")
    return payload


def _answer_result(notification_id: int, answer: dict, *, answered: bool) -> dict:
    """get_notification_answer's response: every key present, so templates never hit a missing one."""
    return {
        "answered": answered,
        "notification_id": answer.get("notification_id", notification_id),
        "status": answer.get("status"),
        "action_id": answer.get("action_id"),
        "text": answer.get("text"),
        "answered_at": answer.get("answered_at"),
    }


class PushWardApiError(Exception):
    """PushWard API error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PushWardAuthError(PushWardApiError):
    """PushWard authentication error — 401, bad/expired integration key."""


class PushWardNotFoundError(PushWardApiError):
    """PushWard 404 - the targeted resource does not exist server-side.

    Raised for a PATCH/GET against a slug the server has no row for (e.g. a
    widget that was never created, or was deleted). Callers that opt into
    404 tolerance (allow_404) never see this; others can catch it to recreate
    the resource. Subclasses PushWardApiError so existing handlers still work."""


class PushWardForbiddenError(PushWardApiError):
    """PushWard 403 — server-side policy rejection (subscription lapsed,
    slug scope, shared-activity, etc.). Not an auth failure — do not reauth."""


class PushWardWidgetPermissionError(PushWardForbiddenError):
    """403 specifically for missing `widgets:true` flag on the integration key.

    Server returns this for any widget endpoint call when the integration key
    doesn't have widget permission. Treated like PushWardForbiddenError but
    surfaced with widget-specific guidance.
    """


class PushWardEmailPermissionError(PushWardForbiddenError):
    """403 on POST /emails — missing `emails` capability on the key OR the
    recipient isn't a verified address for the account.

    An integration key can't verify recipients; that's done in the PushWard iOS
    app. The server's `detail` distinguishes the two cases and is surfaced to
    the HA user.
    """


class PushWardRateLimitedError(PushWardApiError):
    """429 on a request that is not retried in place (answer reads): the caller backs off.

    ``retry_after`` is the server's Retry-After in seconds, clamped, 0 when absent.
    """

    def __init__(self, message: str, *, retry_after: float = 0) -> None:
        super().__init__(message, status_code=HTTPStatus.TOO_MANY_REQUESTS)
        self.retry_after = retry_after


class PushWardQuotaExceededError(PushWardApiError):
    """429 with code `quota.exceeded`: nothing to retry until `reset_at` (see quota.QuotaGate)."""

    def __init__(
        self,
        kind: str,
        *,
        used: int | None = None,
        limit: int | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        self.kind = kind
        self.used = used
        self.limit = limit
        self.reset_at = reset_at
        super().__init__(self._describe(), status_code=HTTPStatus.TOO_MANY_REQUESTS)

    def _describe(self) -> str:
        text = f"PushWard {self.kind} quota exhausted"
        if self.used is not None and self.limit is not None:
            text += f" ({self.used}/{self.limit})"
        if self.reset_at is not None:
            text += f", resets {self.reset_at.astimezone(dt_util.UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        return text


class PushWardApiClient:
    """Async client for the PushWard REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        integration_key: str,
        quota_gate: QuotaGate | None = None,
        e2e_key: bytes | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._integration_key = integration_key
        self._request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        # Answer long-polls hold their connection for up to ANSWER_LONG_POLL_SECONDS,
        # so they stay out of the shared request semaphore (they would stall pushes)
        # and get their own, small enough to leave the account's other clients room
        # under the server's per-user wait cap.
        self._answer_wait_semaphore = asyncio.Semaphore(ANSWER_MAX_CONCURRENT_WAITS)
        self._headers = {"Authorization": f"Bearer {self._integration_key}"}
        # Optional: remembers exhausted quotas so metered requests are refused
        # locally instead of being sent (and rejected) until the period resets.
        self._quota_gate = quota_gate
        # The end-to-end encryption key from the entry options; None sends notification
        # text in the clear. Swapped in place when the options change.
        self.e2e_key = e2e_key

    async def validate_connection(self) -> bool:
        """Validate the connection and integration key via GET /auth/me."""
        await self.get_me()
        return True

    async def get_me(self) -> dict[str, Any]:
        """Fetch the account profile + usage counters via GET /auth/me.

        Returns the parsed JSON body. The server returns the user's own quota
        counters to integration keys: each `*_used` count plus a `*_limit` for
        capped resources (free tier caps everything; premium omits the uncapped
        Live Activity / widget limits and switches notifications to a daily cap).
        Raises PushWardAuthError on 401/403 (bad/expired key) and PushWardApiError
        on any other failure, so callers can map auth failures to reauth. Goes
        through the same retry/backoff as the write paths so a transient 429 or
        5xx doesn't flip the usage sensors to unavailable for a whole poll cycle.
        """
        data = await self._request_with_retry(
            "GET",
            "/auth/me",
            forbidden_is_auth=True,
            return_json=True,
        )
        if not isinstance(data, dict):
            raise PushWardApiError("Unexpected /auth/me response shape")
        return data

    async def create_activity(
        self,
        slug: str,
        name: str,
        priority: int,
        ended_ttl: int | None = None,
        stale_ttl: int | None = None,
        dismissal_ttl: int | None = None,
    ) -> None:
        """Create an activity via POST /activities.

        Server upserts on duplicate slug and always returns 201, so `handle_409`
        only covers the `activity.limit_exceeded` path now.
        """
        body: dict = {
            "slug": slug,
            "name": name,
            "priority": priority,
        }
        if ended_ttl is not None:
            body["ended_ttl"] = ended_ttl
        if stale_ttl is not None:
            body["stale_ttl"] = stale_ttl
        if dismissal_ttl is not None:
            body["dismissal_ttl"] = dismissal_ttl
        await self._request_with_retry(
            "POST",
            "/activities",
            json=body,
            handle_409=True,
            quota_kind=QUOTA_KIND_LIVE_ACTIVITY_UPDATES,
        )

    async def update_activity(
        self,
        slug: str,
        state: str,
        content: dict,
        *,
        sound: str | None = None,
        priority: int | None = None,
        ended_ttl: int | None = None,
        stale_ttl: int | None = None,
        dismissal_ttl: int | None = None,
    ) -> None:
        """PATCH /activities/{slug}: sound, priority, and the TTLs are top-level, not content."""
        body: dict = {"state": state, "content": content}
        if sound is not None:
            body["sound"] = sound
        if priority is not None:
            body["priority"] = priority
        if ended_ttl is not None:
            body["ended_ttl"] = ended_ttl
        if stale_ttl is not None:
            body["stale_ttl"] = stale_ttl
        if dismissal_ttl is not None:
            body["dismissal_ttl"] = dismissal_ttl
        await self._request_with_retry(
            "PATCH", f"/activities/{slug}", json=body, quota_kind=QUOTA_KIND_LIVE_ACTIVITY_UPDATES
        )

    async def delete_activity(self, slug: str) -> None:
        """Delete an activity via DELETE /activities/{slug}."""
        await self._request_with_retry(
            "DELETE",
            f"/activities/{slug}",
            allow_404=True,
        )

    async def create_widget(
        self,
        slug: str,
        name: str,
        template: str,
        content: dict,
        *,
        push_throttle: int | None = None,
        stale_after: int | None = None,
    ) -> None:
        """POST /widgets. Server upserts on slug — same slug overwrites in place.

        Template lives inside content (mirrors the activity API shape). Caller's
        `content` dict is merged with `template` here so callers can keep
        passing the template separately.
        """
        body: dict = {
            "slug": slug,
            "name": name,
            "content": {**content, "template": template},
        }
        if push_throttle is not None:
            body["push_throttle"] = push_throttle
        if stale_after is not None:
            body["stale_after"] = stale_after
        await self._request_with_retry("POST", "/widgets", json=body)

    async def patch_widget(self, slug: str, body: dict) -> None:
        """PATCH /widgets/{slug} — RFC 7396 merge patch.

        Caller builds the patch dict (typically {"content": {...},
        "push_throttle": ...}). Template lives inside content; change it via
        `content.template`. Absent fields are preserved server-side.
        """
        await self._request_with_retry("PATCH", f"/widgets/{slug}", json=body, quota_kind=QUOTA_KIND_WIDGET_UPDATES)

    async def delete_widget(self, slug: str) -> None:
        """DELETE /widgets/{slug}. Idempotent — 404 swallowed."""
        await self._request_with_retry("DELETE", f"/widgets/{slug}", allow_404=True)

    async def create_notification(
        self,
        title: str,
        body: str,
        *,
        subtitle: str | None = None,
        level: str | None = None,
        volume: float | None = None,
        thread_id: str | None = None,
        collapse_id: str | None = None,
        source: str | None = None,
        source_display_name: str | None = None,
        activity_slug: str | None = None,
        url: str | None = None,
        media: dict | None = None,
        icon_url: str | None = None,
        metadata: dict[str, str] | None = None,
        actions: list[dict] | None = None,
        push: bool = True,
        send_at: datetime | None = None,
        recurrence: dict | None = None,
    ) -> dict | None:
        """Create a notification via POST /notifications and return it.

        With a timezone-aware ``send_at`` and/or a ``recurrence`` rule
        (``{cron, timezone, until, count}``) it is queued instead, via
        POST /notifications/scheduled, and the schedule is returned. With
        recurrence, send_at is optional and marks where the series starts. Both
        count against the notification quota (a schedule each time it sends), so
        both go through the quota gate.

        With an e2e_key, title, subtitle, body and url go out only inside the
        ``encrypted`` envelope (E2EError when they cannot be sealed); the server
        stores placeholders for them.
        """
        if self.e2e_key is not None:
            payload: dict = {
                "encrypted": seal(self.e2e_key, title=title, body=body, subtitle=subtitle, url=url),
                "push": push,
            }
            subtitle = url = None
        else:
            payload = {"title": title, "body": body, "push": push}
        for key, val in [
            ("subtitle", subtitle),
            ("level", level),
            ("volume", volume),
            ("thread_id", thread_id),
            ("collapse_id", collapse_id),
            ("source", source),
            ("source_display_name", source_display_name),
            ("activity_slug", activity_slug),
            ("url", url),
            ("media", media),
            ("icon_url", icon_url),
            ("metadata", metadata),
            ("actions", actions),
        ]:
            if val is not None:
                payload[key] = val
        path = "/notifications"
        if send_at is not None:
            payload["send_at"] = _aware_isoformat(send_at, "send_at")
            path = "/notifications/scheduled"
        if recurrence is not None:
            payload["recurrence"] = _recurrence_payload(recurrence)
            path = "/notifications/scheduled"
        return await self._request_with_retry(
            "POST", path, json=payload, quota_kind=QUOTA_KIND_NOTIFICATIONS, return_json=True
        )

    async def list_scheduled_notifications(self, status: str = "scheduled") -> list[dict]:
        """GET /notifications/scheduled, following next_cursor.

        status=scheduled comes soonest first, every other status latest first.

        Stops after SCHEDULED_LIST_MAX_PAGES pages. Pending schedules are capped
        at 25 server-side, so only sent/failed history can get that long.
        """
        items: list[dict] = []
        cursor = ""
        for _ in range(SCHEDULED_LIST_MAX_PAGES):
            query: dict[str, str | int] = {"status": status, "limit": SCHEDULED_LIST_PAGE_SIZE}
            if cursor:
                query["cursor"] = cursor
            data = await self._request_with_retry(
                "GET", f"/notifications/scheduled?{urlencode(query)}", return_json=True
            )
            data = data or {}
            items.extend(data.get("items") or [])
            cursor = str(data.get("next_cursor") or "")
            if not cursor:
                break
        return items

    async def get_notification_answer(self, notification_id: int, *, wait: int = 0) -> dict:
        """GET /notifications/answers/{id} once, holding up to ``wait`` seconds (max 25).

        One attempt, no retry: wait_for_notification_answer owns the backoff.
        Not metered (no quota gate), and outside the shared request semaphore.
        Raises PushWardNotFoundError when there is nothing to read (no url-less
        action, sent with another key, or past the 30-day retention),
        PushWardRateLimitedError on 429 and PushWardApiError otherwise.
        """
        path = f"/notifications/answers/{int(notification_id)}"
        if wait > 0:
            path += f"?wait={int(wait)}"
        timeout = aiohttp.ClientTimeout(total=max(0, wait) + ANSWER_REQUEST_MARGIN_SECONDS)
        try:
            async with self._session.request(
                "GET", f"{self._base_url}{path}", headers=self._headers, timeout=timeout
            ) as resp:
                if resp.ok:
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError as err:
                        raise PushWardApiError(f"GET {path} returned invalid JSON") from err
                    if not isinstance(data, dict):
                        raise PushWardApiError(f"GET {path} returned invalid JSON")
                    return data
                if resp.status == HTTPStatus.UNAUTHORIZED:
                    raise PushWardAuthError("Invalid integration key", status_code=resp.status)
                _, detail, raw, _ = await self._parse_problem(resp)
                message = f"GET {path} failed ({resp.status}): {self._truncate(detail or raw)}"
                if resp.status == HTTPStatus.FORBIDDEN:
                    raise PushWardForbiddenError(self._truncate(detail or raw) or "Forbidden", status_code=resp.status)
                if resp.status == HTTPStatus.NOT_FOUND:
                    raise PushWardNotFoundError(message, status_code=resp.status)
                if resp.status == HTTPStatus.TOO_MANY_REQUESTS:
                    raise PushWardRateLimitedError(
                        message, retry_after=self._parse_retry_after(resp.headers.get("Retry-After", ""))
                    )
                raise PushWardApiError(message, status_code=resp.status)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PushWardApiError(f"GET {path} connection error: {self._truncate(str(err))}") from err

    async def poll_notification_answer(self, notification_id: int, *, hold: int) -> tuple[dict, bool]:
        """One read of an answer, and whether it held a server wait.

        A long-poll of up to ``hold`` seconds when one of this install's wait
        slots is free, a plain read otherwise. Errors as get_notification_answer.
        """
        if hold > 0 and not self._answer_wait_semaphore.locked():
            async with self._answer_wait_semaphore:
                return await self.get_notification_answer(notification_id, wait=hold), True
        return await self.get_notification_answer(notification_id), False

    async def wait_for_notification_answer(self, notification_id: int, timeout: float) -> dict:
        """Wait up to ``timeout`` seconds for the answer to a notification.

        Repeats server long-polls (ANSWER_LONG_POLL_SECONDS each) until the
        answer lands or the time runs out; ``timeout`` 0 reads once. A timeout
        comes back as ``answered: False`` with a ``reason``, not as an error.
        401/403/404 fail at once. A 429 (the server's per-user wait cap, shared
        with the account's other clients, or the request limiter) switches to
        plain reads for ANSWER_PLAIN_AFTER_WAIT_LIMIT_SECONDS, the first one
        right away, since a held wait is refused before the answer is looked
        at; so do busy local wait slots. ANSWER_FAILURE_BUDGET consecutive 5xx
        or connection errors give up.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        plain_until = 0.0
        failures = 0
        while True:
            hold = int(min(ANSWER_LONG_POLL_SECONDS, deadline - loop.time()))
            if loop.time() < plain_until:
                hold = 0
            started = loop.time()
            try:
                answer, held = await self.poll_notification_answer(notification_id, hold=hold)
            except PushWardRateLimitedError as err:
                plain_until = loop.time() + ANSWER_PLAIN_AFTER_WAIT_LIMIT_SECONDS
                delay = max(err.retry_after, ANSWER_MIN_POLL_INTERVAL)
            except (PushWardAuthError, PushWardForbiddenError, PushWardNotFoundError):
                raise
            except PushWardApiError as err:
                if err.status_code is not None and err.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
                    raise
                failures += 1
                if failures >= ANSWER_FAILURE_BUDGET:
                    raise
                delay = ANSWER_PLAIN_POLL_INTERVAL * failures
            else:
                failures = 0
                if answer.get("status") == ANSWER_STATUS_ANSWERED:
                    return _answer_result(notification_id, answer, answered=True)
                # A long-poll that ran its hold goes straight into the next one;
                # one that came back early (a server without ?wait=) is spaced out.
                if held:
                    delay = max(0.0, ANSWER_MIN_POLL_INTERVAL - (loop.time() - started))
                else:
                    delay = ANSWER_PLAIN_POLL_INTERVAL
            remaining = deadline - loop.time()
            if remaining < 1:
                result = _answer_result(notification_id, {"status": ANSWER_STATUS_PENDING}, answered=False)
                result["reason"] = f"no answer within {timeout:g}s"
                return result
            await asyncio.sleep(min(delay, remaining))

    async def cancel_scheduled_notification(self, scheduled_id: int, *, purge: bool = False) -> None:
        """DELETE /notifications/scheduled/{id}. Idempotent: 404 swallowed.

        A plain cancel leaves the schedule readable as ``canceled`` for 24 hours
        (the app shows it). ``purge`` removes it outright, for a schedule that is
        only being replaced; a server without purge treats it as a plain cancel.
        """
        path = f"/notifications/scheduled/{int(scheduled_id)}"
        if purge:
            path += "?purge=true"
        await self._request_with_retry("DELETE", path, allow_404=True)

    async def get_scheduled_notification(self, scheduled_id: int) -> dict | None:
        """GET /notifications/scheduled/{id}; None when it no longer exists."""
        return await self._request_with_retry(
            "GET", f"/notifications/scheduled/{int(scheduled_id)}", allow_404=True, return_json=True
        )

    async def send_email(
        self,
        to: str,
        subject: str,
        *,
        text_body: str | None = None,
        html_body: str | None = None,
    ) -> None:
        """Send a transactional email via POST /emails.

        ``to`` must be a verified, non-unsubscribed recipient of the account
        (registered and confirmed in the PushWard iOS app), and the integration
        key needs the ``emails`` capability. Provide ``text_body``, ``html_body``,
        or both.
        """
        payload: dict = {"to": to, "subject": subject}
        if text_body is not None:
            payload["text_body"] = text_body
        if html_body is not None:
            payload["html_body"] = html_body
        await self._request_with_retry("POST", "/emails", json=payload, quota_kind=QUOTA_KIND_EMAILS)

    @staticmethod
    def _truncate(message: str, max_len: int = 200) -> str:
        return message[:max_len] + ("…" if len(message) > max_len else "")

    @staticmethod
    async def _parse_problem(resp: aiohttp.ClientResponse) -> tuple[str, str, str, dict]:
        """Parse a RFC 9457 Problem body. Return (code, detail, raw_body, fields).

        Tolerant to non-Problem bodies (plain text, empty) — falls back to an
        empty code/detail/fields so callers can use the raw body.
        """
        raw = await resp.text()
        if not raw:
            return "", "", raw, {}
        try:
            data = json.loads(raw)
        except ValueError:
            return "", "", raw, {}
        if not isinstance(data, dict):
            return "", "", raw, {}
        return str(data.get("code") or ""), str(data.get("detail") or ""), raw, data

    @staticmethod
    def _quota_error_from(kind: str, data: dict) -> PushWardQuotaExceededError:
        """Build the typed error from a `quota.exceeded` Problem body."""
        reset_at = dt_util.parse_datetime(str(data.get("reset_at") or ""))
        if reset_at is not None and reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=dt_util.UTC)
        return PushWardQuotaExceededError(kind, used=data.get("used"), limit=data.get("limit"), reset_at=reset_at)

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        handle_409: bool = False,
        allow_404: bool = False,
        forbidden_is_auth: bool = False,
        return_json: bool = False,
        quota_kind: str | None = None,
    ) -> Any:
        """Execute an HTTP request with exponential backoff retry.

        ``forbidden_is_auth`` maps 403 to PushWardAuthError (endpoints where a
        403 means a bad/expired key, e.g. /auth/me). ``return_json`` parses and
        returns the success body instead of None. ``quota_kind`` names the metered
        quota the request spends (the server's `x-require-subscription-or-quota`
        routes); such requests are refused locally while the quota gate has that
        kind paused, and a `quota.exceeded` 429 pauses it.
        """
        gate = self._quota_gate if quota_kind is not None else None
        async with self._request_semaphore:
            url = f"{self._base_url}{path}"
            last_error: Exception | None = None

            for attempt in range(MAX_RETRIES):
                # Checked per attempt, not once up front: a request that waited on
                # the semaphore or slept through a rate-limit retry must not go out
                # after another request has just learned the quota is gone.
                if gate is not None:
                    gate.raise_if_blocked(quota_kind)
                try:
                    async with self._session.request(
                        method, url, headers=self._headers, json=json, timeout=_TIMEOUT
                    ) as resp:
                        if resp.ok:
                            if not return_json:
                                return None
                            try:
                                return await resp.json(content_type=None)
                            except ValueError as err:
                                raise PushWardApiError(f"{method} {path} returned invalid JSON") from err

                        if allow_404 and resp.status == HTTPStatus.NOT_FOUND:
                            return None

                        if handle_409 and resp.status == HTTPStatus.CONFLICT:
                            code, detail, raw, _ = await self._parse_problem(resp)
                            if code == "activity.already_exists" or "already exists" in (detail or raw).lower():
                                return None
                            raise PushWardApiError(
                                f"Activity limit reached: {self._truncate(detail or raw)}",
                                status_code=resp.status,
                            )

                        if resp.status == HTTPStatus.UNAUTHORIZED:
                            raise PushWardAuthError(
                                "Invalid integration key",
                                status_code=resp.status,
                            )

                        if resp.status == HTTPStatus.FORBIDDEN:
                            if forbidden_is_auth:
                                raise PushWardAuthError(
                                    "Invalid integration key",
                                    status_code=resp.status,
                                )
                            _, detail, raw, _ = await self._parse_problem(resp)
                            message = self._truncate(detail or raw) or "Forbidden"
                            if path.startswith("/widgets"):
                                raise PushWardWidgetPermissionError(
                                    message,
                                    status_code=resp.status,
                                )
                            if path.startswith("/emails"):
                                raise PushWardEmailPermissionError(
                                    message,
                                    status_code=resp.status,
                                )
                            raise PushWardForbiddenError(
                                message,
                                status_code=resp.status,
                            )

                        if resp.status == HTTPStatus.TOO_MANY_REQUESTS:
                            code, _, _, data = await self._parse_problem(resp)
                            if code == QUOTA_EXCEEDED_CODE:
                                # Spent for the whole period; retrying only burns
                                # requests. Pause the kind and fail fast.
                                err = self._quota_error_from(quota_kind or str(data.get("kind") or ""), data)
                                if gate is not None:
                                    gate.arm(err, server_now=parse_http_date(resp.headers.get("Date")))
                                raise err
                            last_error = PushWardApiError(
                                f"{method} {path} rate limited (429)",
                                status_code=resp.status,
                            )
                            if attempt < MAX_RETRIES - 1:
                                delay = self._parse_retry_after(resp.headers.get("Retry-After", ""))
                                if delay <= 0:
                                    delay = self._backoff_delay(attempt)
                                _LOGGER.debug("Rate limited, retrying in %.1fs", delay)
                                await asyncio.sleep(delay)
                            continue

                        # Other 4xx — don't retry
                        if 400 <= resp.status < 500:
                            _, detail, raw, _ = await self._parse_problem(resp)
                            message = f"{method} {path} failed ({resp.status}): {self._truncate(detail or raw)}"
                            # A 404 that reached here means the caller didn't opt into
                            # allow_404, so a missing resource is a typed error the caller
                            # can catch to recreate it (e.g. widget PATCH -> recreate).
                            if resp.status == HTTPStatus.NOT_FOUND:
                                raise PushWardNotFoundError(message, status_code=resp.status)
                            raise PushWardApiError(message, status_code=resp.status)

                        # 5xx — retry
                        last_error = PushWardApiError(
                            f"{method} {path} failed ({resp.status})",
                            status_code=resp.status,
                        )
                except (aiohttp.ClientError, TimeoutError) as err:
                    last_error = PushWardApiError(f"{method} {path} connection error: {self._truncate(str(err))}")

                if attempt < MAX_RETRIES - 1:
                    delay = self._backoff_delay(attempt)
                    _LOGGER.debug(
                        "Retrying %s %s in %.1fs (attempt %d/%d)",
                        method,
                        path,
                        delay,
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)

            if last_error is None:
                last_error = PushWardApiError(f"{method} {path} failed after {MAX_RETRIES} attempts")
            raise last_error

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        delay = min(RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY)
        # Jitter so many clients rate-limited together do not retry in lockstep.
        return delay * (0.5 + random.random() * 0.5)

    @staticmethod
    def _parse_retry_after(header: str) -> float:
        # Clamp to RETRY_MAX_DELAY: the request+retry loop holds one of the shared
        # MAX_CONCURRENT_REQUESTS semaphore slots while it sleeps, so an honest large
        # value (or a hostile/misconfigured header) must not park that slot for
        # minutes and starve concurrent pushes.
        if not header:
            return 0
        try:
            value = float(header)
        except ValueError:
            pass
        else:
            # NaN parses cleanly and slips past a `<= 0` guard downstream, so
            # asyncio.sleep(nan) would corrupt the loop timer heap. Reject it here;
            # negatives clamp to 0 (caller falls back to backoff), inf clamps to max.
            if value != value:
                return 0
            return min(max(0.0, value), RETRY_MAX_DELAY)
        dt = parse_http_date(header)
        if dt is None:
            return 0
        return min(max(0, dt.timestamp() - time.time()), RETRY_MAX_DELAY)
