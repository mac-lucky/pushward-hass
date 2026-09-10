"""Quota gate: pause metered requests after a `quota.exceeded` 429 until the reset.

A free-tier account that has used its monthly Live Activity or widget updates
gets a 429 on every further update. Without a memory of that, each HA state
change turns into a rejected request (times the retry count), all month long.
The gate remembers the exhausted kind, refuses matching requests locally, and
wakes the usage coordinator at the server's ``reset_at`` so the integration
resumes on its own: the coordinator confirms the counters are back under the
cap, releases the kind, and the managers re-send whatever changed meanwhile.

One gate per config entry, shared by the API client (which asks and arms) and
the coordinator (which releases). Managers subscribe to releases through the
dispatcher signal from :func:`quota_released_signal`.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import PushWardQuotaExceededError
from .const import (
    APP_STORE_URL,
    DOMAIN,
    QUOTA_BLOCK_FALLBACK_SECONDS,
    QUOTA_BLOCK_MIN_SECONDS,
    QUOTA_BLOCK_PLAUSIBLE_MAX_SECONDS,
    QUOTA_RELEASE_JITTER_SECONDS,
    MeteredResource,
    metered_resource_for_kind,
    usage_limit_issue_id,
)

if TYPE_CHECKING:
    from .coordinator import PushWardUsageCoordinator

_LOGGER = logging.getLogger(__name__)


def quota_released_signal(entry_id: str) -> str:
    """Dispatcher signal fired with the released quota kind as its argument."""
    return f"{DOMAIN}_quota_released_{entry_id}"


def format_reset(value: Any) -> str:
    """Friendly reset hint for the repair description.

    Accepts the ISO-8601 string ``/auth/me`` returns (``2026-07-01T00:00:00Z``)
    or a datetime; the date portion is enough for the user and avoids leaking a
    clock-precise time.
    """
    if isinstance(value, datetime):
        return value.astimezone(dt_util.UTC).date().isoformat()
    if isinstance(value, str) and value:
        return value.split("T", 1)[0]
    return "the next reset"


@callback
def async_report_usage_limit(
    hass: HomeAssistant,
    entry_id: str,
    resource: MeteredResource,
    used: Any,
    limit: Any,
    reset: Any,
) -> None:
    """Raise (or refresh) the usage-limit Repair issue for one metered resource."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        usage_limit_issue_id(entry_id, resource.used_key),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=resource.translation_key,
        translation_placeholders={
            "used": str(used),
            "limit": str(limit),
            "resets_at": format_reset(reset),
        },
        learn_more_url=APP_STORE_URL,
    )


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    # A `-0000` zone parses to a naive datetime; reset_at is always aware.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt_util.UTC)


def block_delay_seconds(reset_at: datetime | None, server_now: datetime | None) -> float:
    """Seconds to pause, measured against the server's clock when it is known.

    The server's ``Date`` header is the same clock that produced ``reset_at``, so
    the difference is skew-free; the local clock is only a fallback. The result is
    clamped: at least QUOTA_BLOCK_MIN_SECONDS (a reset_at already in the past must
    still pause, or the storm continues one request at a time), and the fallback
    length when reset_at is missing or implausibly far away.
    """
    if reset_at is None:
        return float(QUOTA_BLOCK_FALLBACK_SECONDS)
    now = server_now or dt_util.utcnow()
    delay = (reset_at - now).total_seconds()
    if delay > QUOTA_BLOCK_PLAUSIBLE_MAX_SECONDS:
        return float(QUOTA_BLOCK_FALLBACK_SECONDS)
    return max(delay, float(QUOTA_BLOCK_MIN_SECONDS))


@dataclass
class _Block:
    error: PushWardQuotaExceededError
    # Monotonic loop times; an NTP step on the host can neither lift nor extend
    # them. `reset_deadline` is the server's reset, `deadline` adds the wake-up
    # jitter so a request never probes before the timer has had its turn.
    reset_deadline: float
    deadline: float
    unsub_timer: CALLBACK_TYPE | None = None


class QuotaGate:
    """Per-entry memory of exhausted quotas plus the reset-time wake-up."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._blocks: dict[str, _Block] = {}
        self._coordinator: PushWardUsageCoordinator | None = None

    def attach_coordinator(self, coordinator: PushWardUsageCoordinator) -> None:
        """Wire the coordinator that confirms a reset before anything is re-sent."""
        self._coordinator = coordinator
        coordinator.quota_gate = self

    # ----- queried by the API client -----

    def blocked(self, kind: str) -> PushWardQuotaExceededError | None:
        """The stored error while ``kind`` is paused, else None."""
        block = self._blocks.get(kind)
        if block is None:
            return None
        if self._hass.loop.time() >= block.deadline:
            # Timer has not fired yet (or was lost); let this request probe the
            # server. A fresh 429 re-arms the gate with the server's new reset_at.
            self._drop(kind)
            return None
        # A fresh instance per refusal: re-raising one object appends a traceback
        # (and the frames it pins) on every raise, and it lives here all period.
        err = block.error
        return PushWardQuotaExceededError(err.kind, used=err.used, limit=err.limit, reset_at=err.reset_at)

    def is_blocked(self, kind: str) -> bool:
        return self.blocked(kind) is not None

    # ----- armed by the API client -----

    def arm(self, err: PushWardQuotaExceededError, *, server_date: str | None = None) -> None:
        """Pause ``err.kind`` until its reset and schedule the wake-up.

        Idempotent: several in-flight requests can fail together, so only the
        transition into the paused state (or a later reset_at) is worth a WARNING.
        """
        kind = err.kind
        delay = block_delay_seconds(err.reset_at, _parse_http_date(server_date))
        now = self._hass.loop.time()
        existing = self._blocks.get(kind)
        if existing is not None and now + delay <= existing.reset_deadline + 1:
            _LOGGER.debug("PushWard %s quota still exhausted; pause already armed", kind)
            existing.error = err
            self._report_issue(err)
            return

        if existing is not None and existing.unsub_timer is not None:
            existing.unsub_timer()
        wait = delay + random.uniform(0, QUOTA_RELEASE_JITTER_SECONDS)
        block = _Block(error=err, reset_deadline=now + delay, deadline=now + wait)
        block.unsub_timer = async_call_later(self._hass, wait, partial(self._on_timer, kind))
        self._blocks[kind] = block
        _LOGGER.warning(
            "%s; pausing %s requests for %s until the quota resets",
            err,
            kind,
            _describe_delay(delay),
        )
        self._report_issue(err)

    def _report_issue(self, err: PushWardQuotaExceededError) -> None:
        resource = metered_resource_for_kind(err.kind)
        if resource is None:
            return
        async_report_usage_limit(
            self._hass,
            self._entry.entry_id,
            resource,
            err.used if err.used is not None else "?",
            err.limit if err.limit is not None else "?",
            err.reset_at,
        )

    # ----- released by the coordinator -----

    @callback
    def release(self, kind: str) -> None:
        """Lift the pause on ``kind`` and tell the managers to catch up."""
        if kind not in self._blocks:
            return
        self._drop(kind)
        _LOGGER.info("PushWard %s quota available again; resuming", kind)
        async_dispatcher_send(self._hass, quota_released_signal(self._entry.entry_id), kind)

    @callback
    def _on_timer(self, kind: str, _now: datetime | None = None) -> None:
        block = self._blocks.get(kind)
        if block is not None:
            block.unsub_timer = None
        if self._coordinator is None:
            self.release(kind)
            return
        # Let the server confirm the counters are back under the cap before the
        # managers re-send; the coordinator releases the kind when it sees that.
        self._hass.async_create_task(self._coordinator.async_request_refresh())

    def _drop(self, kind: str) -> None:
        block = self._blocks.pop(kind, None)
        if block is not None and block.unsub_timer is not None:
            block.unsub_timer()

    # ----- lifecycle / diagnostics -----

    @callback
    def async_shutdown(self) -> None:
        """Cancel every pending wake-up (config entry unload)."""
        for kind in list(self._blocks):
            self._drop(kind)

    def snapshot(self) -> dict[str, str | None]:
        """Paused kinds and their server-side reset time, for diagnostics."""
        return {
            kind: block.error.reset_at.isoformat() if block.error.reset_at else None
            for kind, block in self._blocks.items()
        }


def _describe_delay(seconds: float) -> str:
    total = int(seconds)
    if total < 3600:
        return f"{max(total // 60, 1)} min"
    if total < 2 * 86400:
        return f"{total // 3600} h"
    return f"{total // 86400} days"
