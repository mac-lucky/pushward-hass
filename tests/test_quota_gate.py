"""Tests for the quota gate: pause metered requests after a quota 429, resume at reset.

The server answers a spent monthly quota with ``429`` + ``code: quota.exceeded``
and a ``reset_at``. The gate remembers that per kind so the client refuses
matching requests locally, raises the usage-limit Repair issue right away,
and wakes the usage coordinator at the reset so the integration resumes on its
own once ``/auth/me`` confirms the counters are back under the cap.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pushward.api import (
    PushWardApiClient,
    PushWardApiError,
    PushWardQuotaExceededError,
    quota_kind_for,
)
from custom_components.pushward.const import (
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    DOMAIN,
    QUOTA_BLOCK_FALLBACK_SECONDS,
    QUOTA_BLOCK_MIN_SECONDS,
    QUOTA_RELEASE_JITTER_SECONDS,
    metered_resource_for_kind,
    usage_limit_issue_id,
)
from custom_components.pushward.coordinator import PushWardUsageCoordinator
from custom_components.pushward.quota import (
    QuotaGate,
    block_delay_seconds,
    format_reset,
    quota_released_signal,
)

from .conftest import make_api_client, make_mock_response, make_mock_session, make_usage_payload

RESET_AT = datetime(2026, 10, 1, tzinfo=UTC)


def _quota_body(kind: str = "live_activity_updates", **overrides) -> str:
    body = {
        "type": "https://pushward.app/errors/too-many-requests",
        "title": "Too Many Requests",
        "status": 429,
        "detail": f"quota exceeded for {kind}",
        "code": "quota.exceeded",
        "kind": kind,
        "reset_at": "2026-10-01T00:00:00Z",
        "used": 250,
        "limit": 250,
    }
    body.update(overrides)
    return json.dumps(body)


def _rate_limit_body() -> str:
    return json.dumps(
        {
            "type": "https://pushward.app/errors/too-many-requests",
            "title": "Too Many Requests",
            "status": 429,
            "detail": "rate limit exceeded",
            "code": "rate_limit.exceeded",
            "retry_after_ms": 1000,
        }
    )


def _quota_response(kind: str = "live_activity_updates", *, server_now: datetime | None = None, **overrides):
    headers = {"Retry-After": format_datetime(RESET_AT, usegmt=True)}
    if server_now is not None:
        headers["Date"] = format_datetime(server_now, usegmt=True)
    return make_mock_response(429, text=_quota_body(kind, **overrides), headers=headers)


def _entry(entry_id: str = "quota_entry") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: "test-key"},
        version=2,
        unique_id=entry_id,
        entry_id=entry_id,
    )


def _quota_error(
    kind: str = "live_activity_updates", reset_at: datetime | None = RESET_AT
) -> PushWardQuotaExceededError:
    return PushWardQuotaExceededError(kind, used=250, limit=250, reset_at=reset_at)


def _released(hass: HomeAssistant, entry_id: str) -> list[str]:
    """Collect the kinds the gate announces on the dispatcher."""
    seen: list[str] = []
    async_dispatcher_connect(hass, quota_released_signal(entry_id), seen.append)
    return seen


# --- route mapping ---


@pytest.mark.parametrize(
    ("method", "path", "kind"),
    [
        ("POST", "/activities", "live_activity_updates"),
        ("PATCH", "/activities/ha-washer", "live_activity_updates"),
        ("PATCH", "/widgets/ha-users", "widget_updates"),
        ("POST", "/notifications", "notifications"),
        ("POST", "/emails", "emails"),
        ("POST", "/widgets", None),
        ("DELETE", "/activities/ha-washer", None),
        ("DELETE", "/widgets/ha-users", None),
        ("GET", "/auth/me", None),
        ("GET", "/activities", None),
    ],
)
def test_quota_kind_for_mirrors_server_gated_routes(method: str, path: str, kind: str | None) -> None:
    assert quota_kind_for(method, path) == kind


def test_every_server_kind_maps_to_a_metered_resource() -> None:
    for kind in ("notifications", "live_activity_updates", "widget_updates", "emails"):
        resource = metered_resource_for_kind(kind)
        assert resource is not None
        assert resource.used_key == f"{kind}_used"
    assert metered_resource_for_kind("bogus") is None


# --- error shape ---


def test_quota_error_message_is_plain_ascii() -> None:
    err = _quota_error()
    text = str(err)
    assert text == "PushWard live_activity_updates quota exhausted (250/250), resets 2026-10-01 00:00 UTC"
    assert text.isascii()
    assert err.status_code == 429
    assert isinstance(err, PushWardApiError)


def test_quota_error_without_counters() -> None:
    assert str(PushWardQuotaExceededError("emails")) == "PushWard emails quota exhausted"
    assert (
        str(PushWardQuotaExceededError("emails", reset_at=RESET_AT))
        == "PushWard emails quota exhausted, resets 2026-10-01 00:00 UTC"
    )


def test_block_delay_accepts_naive_date_header() -> None:
    from custom_components.pushward.quota import _parse_http_date

    parsed = _parse_http_date("Wed, 01 Oct 2026 00:00:00 -0000")
    assert parsed is not None and parsed.tzinfo is not None
    assert block_delay_seconds(RESET_AT + timedelta(hours=1), parsed) == pytest.approx(3600)


async def test_gate_hands_out_a_fresh_error_per_refusal(hass: HomeAssistant) -> None:
    """Re-raising one stored instance would grow its traceback for the whole period."""
    gate = QuotaGate(hass, _entry())
    gate.arm(_quota_error())
    first = gate.blocked("live_activity_updates")
    second = gate.blocked("live_activity_updates")
    assert first is not second
    assert (first.kind, first.used, first.limit, first.reset_at) == (second.kind, 250, 250, RESET_AT)
    gate.async_shutdown()


# --- delay computation ---


def test_block_delay_uses_server_clock_when_known() -> None:
    server_now = RESET_AT - timedelta(hours=3)
    assert block_delay_seconds(RESET_AT, server_now) == pytest.approx(3 * 3600)


def test_block_delay_floors_a_past_reset() -> None:
    server_now = RESET_AT + timedelta(minutes=5)
    assert block_delay_seconds(RESET_AT, server_now) == QUOTA_BLOCK_MIN_SECONDS


def test_block_delay_falls_back_without_reset_at() -> None:
    assert block_delay_seconds(None, None) == QUOTA_BLOCK_FALLBACK_SECONDS


def test_block_delay_falls_back_on_implausible_reset() -> None:
    far = RESET_AT + timedelta(days=400)
    assert block_delay_seconds(far, RESET_AT) == QUOTA_BLOCK_FALLBACK_SECONDS


def test_block_delay_uses_local_clock_without_date_header() -> None:
    reset_at = dt_util.utcnow() + timedelta(hours=2)
    assert 2 * 3600 - 5 < block_delay_seconds(reset_at, None) <= 2 * 3600


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (RESET_AT, "2026-10-01"),
        ("2026-07-01T00:00:00Z", "2026-07-01"),
        (None, "the next reset"),
    ],
)
def test_format_reset_accepts_datetime_and_string(value, expected) -> None:
    assert format_reset(value) == expected


# --- client without a gate: still fails fast, never retries a quota 429 ---


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_quota_429_raises_typed_error_after_one_request(mock_sleep) -> None:
    session = make_mock_session(_quota_response())
    client = make_api_client(session)

    with pytest.raises(PushWardQuotaExceededError) as excinfo:
        await client.update_activity("ha-washer", "ongoing", {"progress": 0.5})

    err = excinfo.value
    assert err.kind == "live_activity_updates"
    assert (err.used, err.limit) == (250, 250)
    assert err.reset_at == RESET_AT
    assert session.request.call_count == 1
    mock_sleep.assert_not_called()


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_rate_limit_429_still_retries(mock_sleep) -> None:
    """The per-request limiter is a different 429 and keeps the retry behaviour."""
    session = make_mock_session(
        make_mock_response(429, text=_rate_limit_body(), headers={"Retry-After": "1"}),
        make_mock_response(200),
    )
    client = make_api_client(session)

    await client.update_activity("ha-washer", "ongoing", {"progress": 0.5})

    assert session.request.call_count == 2
    mock_sleep.assert_awaited_once_with(1.0)


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_quota_429_with_missing_reset_at_still_typed(mock_sleep) -> None:
    session = make_mock_session(_quota_response("notifications", reset_at=None, used=None, limit=None))
    client = make_api_client(session)

    with pytest.raises(PushWardQuotaExceededError) as excinfo:
        await client.create_notification("Hi", "there")

    assert excinfo.value.reset_at is None
    assert excinfo.value.kind == "notifications"
    assert str(excinfo.value) == "PushWard notifications quota exhausted"


# --- gate + client ---


async def test_gate_blocks_same_kind_without_a_request(hass: HomeAssistant) -> None:
    entry = _entry()
    gate = QuotaGate(hass, entry)
    session = make_mock_session(_quota_response(server_now=RESET_AT - timedelta(days=3)))
    client = PushWardApiClient(session, "https://api.example.com", "k", quota_gate=gate)

    with pytest.raises(PushWardQuotaExceededError):
        await client.create_activity("ha-washer", "Washer", 1)
    with pytest.raises(PushWardQuotaExceededError) as excinfo:
        await client.update_activity("ha-washer", "ongoing", {"progress": 0.5})

    assert session.request.call_count == 1
    assert excinfo.value.kind == "live_activity_updates"
    assert gate.is_blocked("live_activity_updates")
    assert not gate.is_blocked("widget_updates")
    assert gate.snapshot() == {"live_activity_updates": RESET_AT.isoformat()}
    gate.async_shutdown()


async def test_gate_lets_unmetered_and_other_kinds_through(hass: HomeAssistant) -> None:
    entry = _entry()
    gate = QuotaGate(hass, entry)
    session = make_mock_session(_quota_response("widget_updates"), make_mock_response(200), make_mock_response(204))
    client = PushWardApiClient(session, "https://api.example.com", "k", quota_gate=gate)

    with pytest.raises(PushWardQuotaExceededError):
        await client.patch_widget("ha-users", {"content": {}})
    # POST /widgets is not metered and a different kind is not paused.
    await client.create_widget(slug="ha-users", template="value", name="Users", content={})
    await client.delete_widget("ha-users")

    assert session.request.call_count == 3
    gate.async_shutdown()


async def test_gate_arm_raises_repair_issue_and_warns_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _entry()
    gate = QuotaGate(hass, entry)

    with caplog.at_level(logging.DEBUG, logger="custom_components.pushward.quota"):
        gate.arm(_quota_error())
        gate.arm(_quota_error())
        gate.arm(_quota_error())

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "pausing live_activity_updates requests" in warnings[0].getMessage()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, usage_limit_issue_id(entry.entry_id, "live_activity_updates_used")
    )
    assert issue is not None
    assert issue.translation_key == "usage_limit_live_activity"
    assert issue.translation_placeholders == {"used": "250", "limit": "250", "resets_at": "2026-10-01"}
    gate.async_shutdown()


async def test_gate_rearms_when_reset_moves_later(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    entry = _entry()
    gate = QuotaGate(hass, entry)

    with caplog.at_level(logging.WARNING, logger="custom_components.pushward.quota"):
        gate.arm(_quota_error())
        gate.arm(_quota_error(reset_at=RESET_AT + timedelta(days=10)))

    assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 2
    assert gate.snapshot()["live_activity_updates"] == (RESET_AT + timedelta(days=10)).isoformat()
    gate.async_shutdown()


async def test_gate_release_announces_kind_once(hass: HomeAssistant) -> None:
    entry = _entry()
    gate = QuotaGate(hass, entry)
    seen = _released(hass, entry.entry_id)

    gate.arm(_quota_error("widget_updates"))
    gate.release("widget_updates")
    gate.release("widget_updates")
    gate.release("emails")
    await hass.async_block_till_done()

    assert seen == ["widget_updates"]
    assert not gate.is_blocked("widget_updates")


async def test_gate_timer_without_coordinator_releases(hass: HomeAssistant) -> None:
    entry = _entry()
    gate = QuotaGate(hass, entry)
    seen = _released(hass, entry.entry_id)

    gate.arm(_quota_error(reset_at=dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS + 30)))
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS + 30 + QUOTA_RELEASE_JITTER_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert seen == ["live_activity_updates"]
    assert not gate.is_blocked("live_activity_updates")


async def test_gate_timer_asks_coordinator_which_releases(hass: HomeAssistant) -> None:
    """Reset wake-up: refresh /auth/me, and only an under-limit reading releases."""
    entry = _entry()
    entry.add_to_hass(hass)
    gate = QuotaGate(hass, entry)
    seen = _released(hass, entry.entry_id)
    api = AsyncMock()
    api.get_me = AsyncMock(
        return_value=make_usage_payload(live_activity_updates_used=250, live_activity_updates_limit=250)
    )
    coordinator = PushWardUsageCoordinator(hass, api, entry)
    gate.attach_coordinator(coordinator)

    gate.arm(_quota_error(reset_at=dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS)))
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS + QUOTA_RELEASE_JITTER_SECONDS + 1)
    )
    await hass.async_block_till_done()

    # Still capped server-side (clock skew, late rollover): stay paused.
    assert api.get_me.await_count == 1
    assert seen == []
    assert gate.is_blocked("live_activity_updates")

    api.get_me.return_value = make_usage_payload(live_activity_updates_used=3, live_activity_updates_limit=250)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert seen == ["live_activity_updates"]
    assert not gate.is_blocked("live_activity_updates")
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, usage_limit_issue_id(entry.entry_id, "live_activity_updates_used"))
        is None
    )
    gate.async_shutdown()


async def test_gate_shutdown_cancels_timers(hass: HomeAssistant) -> None:
    entry = _entry()
    gate = QuotaGate(hass, entry)
    seen = _released(hass, entry.entry_id)

    gate.arm(_quota_error(reset_at=dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS)))
    gate.async_shutdown()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1))
    await hass.async_block_till_done()

    assert seen == []
    assert gate.snapshot() == {}


async def test_gate_expired_deadline_lets_a_probe_through(hass: HomeAssistant) -> None:
    """If the timer is somehow late, the next request probes the server instead of waiting."""
    entry = _entry()
    gate = QuotaGate(hass, entry)
    gate.arm(_quota_error())
    gate._blocks["live_activity_updates"].deadline = hass.loop.time() - 1

    assert gate.blocked("live_activity_updates") is None
    assert gate.snapshot() == {}


# --- config entry integration ---


async def test_setup_entry_wires_gate_and_diagnostics(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.get_me = AsyncMock(return_value=make_usage_payload())

    with patch("custom_components.pushward.PushWardApiClient", return_value=api) as client_cls:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    gate = hass.data[DOMAIN][entry.entry_id]["quota_gate"]
    assert isinstance(gate, QuotaGate)
    assert client_cls.call_args.kwargs["quota_gate"] is gate
    assert gate._coordinator is hass.data[DOMAIN][entry.entry_id]["coordinator"]

    gate.arm(_quota_error("widget_updates"))
    from custom_components.pushward.diagnostics import async_get_config_entry_diagnostics

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["quota_blocks"] == {"widget_updates": RESET_AT.isoformat()}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert gate.snapshot() == {}


async def test_setup_entry_timer_path_resumes_end_to_end(hass: HomeAssistant) -> None:
    """Arm -> timer -> /auth/me under limit -> release announced to the managers."""
    entry = _entry()
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.get_me = AsyncMock(return_value=make_usage_payload())

    with patch("custom_components.pushward.PushWardApiClient", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    gate: QuotaGate = hass.data[DOMAIN][entry.entry_id]["quota_gate"]
    seen = _released(hass, entry.entry_id)
    api.get_me.reset_mock()

    gate.arm(_quota_error(reset_at=dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS)))
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS + QUOTA_RELEASE_JITTER_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert api.get_me.await_count == 1
    assert seen == ["live_activity_updates"]
    assert not gate.is_blocked("live_activity_updates")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_partial_callback_is_recognised_by_hass_job() -> None:
    """The reset timer is a partial of a @callback method; HA must not push it to the executor."""
    from homeassistant.core import HassJob

    gate = QuotaGate(MagicMock(), MagicMock())
    from functools import partial

    job = HassJob(partial(gate._on_timer, "emails"))
    assert job.job_type.name == "Callback"


async def test_gate_rearm_with_same_reset_but_new_jitter_stays_quiet(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The jitter added to the wake-up must not make a same-reset re-arm look later."""
    entry = _entry()
    gate = QuotaGate(hass, entry)
    with (
        caplog.at_level(logging.WARNING, logger="custom_components.pushward.quota"),
        patch("custom_components.pushward.quota.random.uniform", side_effect=[0.0, QUOTA_RELEASE_JITTER_SECONDS]),
    ):
        gate.arm(_quota_error())
        gate.arm(_quota_error())
    assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 1
    gate.async_shutdown()


async def test_gate_blocks_through_the_jitter_window(hass: HomeAssistant) -> None:
    """Between the server reset and the jittered wake-up, requests still wait for the timer."""
    entry = _entry()
    gate = QuotaGate(hass, entry)
    with patch("custom_components.pushward.quota.random.uniform", return_value=QUOTA_RELEASE_JITTER_SECONDS):
        gate.arm(_quota_error())
    block = gate._blocks["live_activity_updates"]
    assert block.deadline - block.reset_deadline == pytest.approx(QUOTA_RELEASE_JITTER_SECONDS)
    block.reset_deadline = hass.loop.time() - 1
    block.deadline = hass.loop.time() + 30
    assert gate.blocked("live_activity_updates") is not None
    gate.async_shutdown()
