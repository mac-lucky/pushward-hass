"""Async PushWard API client."""

import asyncio
import json
import logging
import time
from email.utils import parsedate_to_datetime
from http import HTTPStatus

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


class PushWardForbiddenError(PushWardApiError):
    """PushWard 403 — server-side policy rejection (subscription lapsed,
    slug scope, shared-activity, etc.). Not an auth failure — do not reauth."""


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

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._integration_key}"}

    async def validate_connection(self) -> bool:
        """Validate the connection and integration key via GET /auth/me."""
        try:
            async with self._session.get(
                f"{self._base_url}/auth/me",
                headers=self._headers,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    raise PushWardAuthError("Invalid integration key", status_code=resp.status)
                resp.raise_for_status()
                return True
        except aiohttp.ClientError as err:
            raise PushWardApiError(f"Cannot connect to PushWard: {err}") from err

    async def create_activity(
        self,
        slug: str,
        name: str,
        priority: int,
        ended_ttl: int | None = None,
        stale_ttl: int | None = None,
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
    ) -> None:
        """PATCH /activities/{slug} — sound and priority are top-level, not content fields."""
        body: dict = {"state": state, "content": content}
        if sound is not None:
            body["sound"] = sound
        if priority is not None:
            body["priority"] = priority
        await self._request_with_retry("PATCH", f"/activities/{slug}", json=body)

    async def delete_activity(self, slug: str) -> None:
        """Delete an activity via DELETE /activities/{slug}."""
        await self._request_with_retry(
            "DELETE",
            f"/activities/{slug}",
            allow_404=True,
        )

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
    ) -> None:
        """Execute an HTTP request with exponential backoff retry."""
        async with self._request_semaphore:
            url = f"{self._base_url}{path}"
            last_error: Exception | None = None

            for attempt in range(MAX_RETRIES):
                try:
                    async with self._session.request(
                        method, url, headers=self._headers, json=json, timeout=_TIMEOUT
                    ) as resp:
                        if resp.ok:
                            return

                        if allow_404 and resp.status == HTTPStatus.NOT_FOUND:
                            return

                        if handle_409 and resp.status == HTTPStatus.CONFLICT:
                            code, detail, raw = await self._parse_problem(resp)
                            if code == "activity.already_exists" or "already exists" in (detail or raw).lower():
                                return
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
                            _, detail, raw = await self._parse_problem(resp)
                            raise PushWardForbiddenError(
                                self._truncate(detail or raw) or "Forbidden",
                                status_code=resp.status,
                            )

                        if resp.status == HTTPStatus.TOO_MANY_REQUESTS:
                            delay = self._parse_retry_after(resp.headers.get("Retry-After", ""))
                            if delay <= 0:
                                delay = self._backoff_delay(attempt)
                            _LOGGER.debug("Rate limited, retrying in %.1fs", delay)
                            await asyncio.sleep(delay)
                            continue

                        # Other 4xx — don't retry
                        if 400 <= resp.status < 500:
                            _, detail, raw = await self._parse_problem(resp)
                            raise PushWardApiError(
                                f"{method} {path} failed ({resp.status}): {self._truncate(detail or raw)}",
                                status_code=resp.status,
                            )

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

            raise last_error  # type: ignore[misc]

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        return min(RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY)

    @staticmethod
    def _parse_retry_after(header: str) -> float:
        if not header:
            return 0
        try:
            return float(header)
        except ValueError:
            pass
        try:
            dt = parsedate_to_datetime(header)
            delta = dt.timestamp() - time.time()
            return max(0, delta)
        except (ValueError, TypeError):
            return 0
