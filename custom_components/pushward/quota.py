"""Quota gate: pause metered requests after a `quota.exceeded` 429 until the reset.

A free-tier account that has used its monthly Live Activity or widget updates
gets a 429 on every further update. Without a memory of that, each HA state
change turns into a rejected request (times the retry count), all month long.
The gate remembers the exhausted kind, refuses matching requests locally, and
wakes the usage coordinator at the server's ``reset_at`` so the integration
resumes on its own: the coordinator confirms the counters are back under the
cap, releases the kind, and the managers re-send whatever changed meanwhile.

One gate per config entry, shared by the API client (which asks and arms) and
the coordinator (which owns the usage-limit repair issue and releases). Managers
subscribe to releases through the dispatcher signal from
:func:`quota_released_signal`.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import PushWardQuotaExceededError
from .const import (
    DOMAIN,
    QUOTA_BLOCK_FALLBACK_SECONDS,
    QUOTA_BLOCK_MIN_SECONDS,
    QUOTA_BLOCK_PLAUSIBLE_MAX_SECONDS,
    QUOTA_RELEASE_JITTER_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


def quota_released_signal(entry_id: str) -> str:
    """Dispatcher signal fired with the released quota kind as its argument."""
    return f"{DOMAIN}_quota_released_{entry_id}"


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
    delay = (reset_at - (server_now or dt_util.utcnow())).total_seconds()
    if delay > QUOTA_BLOCK_PLAUSIBLE_MAX_SECONDS:
        return float(QUOTA_BLOCK_FALLBACK_SECONDS)
    return max(delay, float(QUOTA_BLOCK_MIN_SECONDS))


@dataclass
class _Block:
    # Plain fields rather than the exception: a raised exception carries its
    # traceback and the frames (request payloads) it pins for the whole period.
    used: int | None
    limit: int | None
    reset_at: datetime | None
    # Monotonic loop time of the jittered wake-up; an NTP step on the host can
    # neither lift nor extend it.
    deadline: float
    unsub_timer: CALLBACK_TYPE | None = None


class QuotaGate:
    """Per-entry memory of exhausted quotas plus the reset-time wake-up."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._blocks: dict[str, _Block] = {}
        # Set by the usage coordinator; asked to re-read /auth/me at the reset so
        # the server confirms the rollover before anything is re-sent.
        self.wakeup: Callable[[], Awaitable[None]] | None = None

    # ----- queried by the API client -----

    def blocked(self, kind: str) -> PushWardQuotaExceededError | None:
        """A fresh error while ``kind`` is paused, else None."""
        block = self._blocks.get(kind)
        if block is None:
            return None
        if self._hass.loop.time() >= block.deadline:
            # The timer is late or lost: let this request probe the server. A
            # fresh 429 re-arms the gate with the server's new reset_at.
            self._drop(kind)
            return None
        return PushWardQuotaExceededError(kind, used=block.used, limit=block.limit, reset_at=block.reset_at)

    def raise_if_blocked(self, kind: str) -> None:
        err = self.blocked(kind)
        if err is not None:
            raise err

    # ----- armed by the API client -----

    def arm(self, err: PushWardQuotaExceededError, *, server_now: datetime | None = None) -> None:
        """Pause ``err.kind`` until its reset and schedule the wake-up.

        Idempotent: several in-flight requests can fail together, so only the
        transition into the paused state (or a later reset_at) is worth a WARNING.
        """
        kind = err.kind
        delay = block_delay_seconds(err.reset_at, server_now)
        now = self._hass.loop.time()
        existing = self._blocks.get(kind)
        if existing is not None and now + delay <= existing.deadline:
            _LOGGER.debug("PushWard %s quota still exhausted; pause already armed", kind)
            return

        self._drop(kind)
        wait = delay + random.uniform(0, QUOTA_RELEASE_JITTER_SECONDS)

        @callback
        def _on_timer(_now: datetime) -> None:
            block = self._blocks.get(kind)
            if block is not None:
                block.unsub_timer = None
            if self.wakeup is None:
                self.release(kind)
            else:
                self._hass.async_create_task(self.wakeup())

        self._blocks[kind] = _Block(
            used=err.used,
            limit=err.limit,
            reset_at=err.reset_at,
            deadline=now + wait,
            unsub_timer=async_call_later(self._hass, wait, _on_timer),
        )
        _LOGGER.warning("%s; pausing %s requests for %s until the quota resets", err, kind, _describe_delay(delay))
        # The coordinator owns the usage-limit repair issue; a refresh raises it
        # from the account's real counters within seconds.
        if self.wakeup is not None:
            self._hass.async_create_task(self.wakeup())

    # ----- released by the coordinator -----

    @callback
    def release(self, kind: str) -> None:
        """Lift the pause on ``kind`` and tell the managers to catch up."""
        if kind not in self._blocks:
            return
        self._drop(kind)
        _LOGGER.info("PushWard %s quota available again; resuming", kind)
        async_dispatcher_send(self._hass, quota_released_signal(self._entry_id), kind)

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
        return {kind: block.reset_at.isoformat() if block.reset_at else None for kind, block in self._blocks.items()}


def _describe_delay(seconds: float) -> str:
    total = int(seconds)
    if total < 3600:
        return f"{max(total // 60, 1)} min"
    if total < 2 * 86400:
        return f"{total // 3600} h"
    return f"{total // 86400} days"
