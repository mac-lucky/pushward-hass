"""Async PushWard API client."""

import asyncio
import logging
import time
from email.utils import parsedate_to_datetime
from http import HTTPStatus

import aiohttp

from .const import MAX_RETRIES, RETRY_BASE_DELAY, RETRY_MAX_DELAY

_LOGGER = logging.getLogger(__name__)


class PushWardApiError(Exception):
    """PushWard API error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PushWardAuthError(PushWardApiError):
    """PushWard authentication error."""


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

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._integration_key}"}

    async def validate_connection(self) -> bool:
        """Validate the connection and integration key via GET /auth/me."""
        try:
            async with self._session.get(
                f"{self._base_url}/auth/me",
                headers=self._headers,
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
        """Create an activity via POST /activities."""
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

    async def update_activity(self, slug: str, state: str, content: dict) -> None:
        """Update an activity via PATCH /activity/{slug}."""
        await self._request_with_retry(
            "PATCH",
            f"/activity/{slug}",
            json={"state": state, "content": content},
        )

    async def delete_activity(self, slug: str) -> None:
        """Delete an activity via DELETE /activities/{slug}."""
        await self._request_with_retry(
            "DELETE",
            f"/activities/{slug}",
            allow_404=True,
        )

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
        url = f"{self._base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                async with self._session.request(method, url, headers=self._headers, json=json) as resp:
                    if resp.ok:
                        return

                    if allow_404 and resp.status == HTTPStatus.NOT_FOUND:
                        return

                    if handle_409 and resp.status == HTTPStatus.CONFLICT:
                        body = await resp.text()
                        if "already exists" in body.lower():
                            return
                        snippet = body[:200] + ("…" if len(body) > 200 else "")
                        raise PushWardApiError(
                            f"Activity limit reached: {snippet}",
                            status_code=resp.status,
                        )

                    if resp.status in (
                        HTTPStatus.UNAUTHORIZED,
                        HTTPStatus.FORBIDDEN,
                    ):
                        raise PushWardAuthError(
                            "Invalid integration key",
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
                        body = await resp.text()
                        snippet = body[:200] + ("…" if len(body) > 200 else "")
                        _LOGGER.debug(
                            "%s %s returned %d: %s", method, path, resp.status, snippet
                        )
                        raise PushWardApiError(
                            f"{method} {path} failed ({resp.status})",
                            status_code=resp.status,
                        )

                    # 5xx — retry
                    last_error = PushWardApiError(
                        f"{method} {path} failed ({resp.status})",
                        status_code=resp.status,
                    )
            except (aiohttp.ClientError, TimeoutError) as err:
                err_msg = str(err)
                snippet = err_msg[:200] + ("…" if len(err_msg) > 200 else "")
                last_error = PushWardApiError(f"{method} {path} connection error: {snippet}")

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
