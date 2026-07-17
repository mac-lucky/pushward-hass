"""Async PushWard API client."""

import asyncio
import json
import logging
import random
import time
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Any

import aiohttp

from .const import MAX_CONCURRENT_REQUESTS, MAX_RETRIES, RETRY_BASE_DELAY, RETRY_MAX_DELAY

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)


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


class PushWardApiClient:
    """Async client for the PushWard REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        integration_key: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._integration_key = integration_key
        self._request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._headers = {"Authorization": f"Bearer {self._integration_key}"}

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
        await self._request_with_retry("PATCH", f"/activities/{slug}", json=body)

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
        await self._request_with_retry("POST", "/widgets", json=body)

    async def patch_widget(self, slug: str, body: dict) -> None:
        """PATCH /widgets/{slug} — RFC 7396 merge patch.

        Caller builds the patch dict (typically {"content": {...},
        "push_throttle": ...}). Template lives inside content; change it via
        `content.template`. Absent fields are preserved server-side.
        """
        await self._request_with_retry("PATCH", f"/widgets/{slug}", json=body)

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
    ) -> None:
        """Create a notification via POST /notifications."""
        payload: dict = {"title": title, "body": body, "push": push}
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
        await self._request_with_retry("POST", "/notifications", json=payload)

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
        await self._request_with_retry("POST", "/emails", json=payload)

    @staticmethod
    def _truncate(message: str, max_len: int = 200) -> str:
        return message[:max_len] + ("…" if len(message) > max_len else "")

    @staticmethod
    async def _parse_problem(resp: aiohttp.ClientResponse) -> tuple[str, str, str]:
        """Parse a RFC 9457 Problem body. Return (code, detail, raw_body).

        Tolerant to non-Problem bodies (plain text, empty) — falls back to an
        empty code/detail so callers can use the raw body.
        """
        raw = await resp.text()
        if not raw:
            return "", "", raw
        try:
            data = json.loads(raw)
        except ValueError:
            return "", "", raw
        if not isinstance(data, dict):
            return "", "", raw
        return str(data.get("code") or ""), str(data.get("detail") or ""), raw

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
    ) -> Any:
        """Execute an HTTP request with exponential backoff retry.

        ``forbidden_is_auth`` maps 403 to PushWardAuthError (endpoints where a
        403 means a bad/expired key, e.g. /auth/me). ``return_json`` parses and
        returns the success body instead of None.
        """
        async with self._request_semaphore:
            url = f"{self._base_url}{path}"
            last_error: Exception | None = None

            for attempt in range(MAX_RETRIES):
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
                            code, detail, raw = await self._parse_problem(resp)
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
                            _, detail, raw = await self._parse_problem(resp)
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
                            _, detail, raw = await self._parse_problem(resp)
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
        try:
            dt = parsedate_to_datetime(header)
            delta = dt.timestamp() - time.time()
            return min(max(0, delta), RETRY_MAX_DELAY)
        except (ValueError, TypeError):
            return 0
