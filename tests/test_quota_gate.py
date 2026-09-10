"""Tests for the quota gate: pause metered requests after a quota 429, resume at reset.

The server answers a spent monthly quota with ``429`` + ``code: quota.exceeded``
and a ``reset_at``. The gate remembers that per kind so the client refuses
matching requests locally and wakes the usage coordinator at the reset; the
coordinator owns the usage-limit repair issue and releases the kind once
``/auth/me`` confirms the counters are back under the cap.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

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
    parse_http_date,
)
from custom_components.pushward.const import (
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    DOMAIN,
    QUOTA_BLOCK_FALLBACK_SECONDS,
    QUOTA_BLOCK_MIN_SECONDS,
    QUOTA_RELEASE_JITTER_SECONDS,
    USAGE_LIMIT_RESOURCES,
    usage_limit_issue_id,
)
from custom_components.pushward.coordinator import PushWardUsageCoordinator
from custom_components.pushward.diagnostics import async_get_config_entry_diagnostics
from custom_components.pushward.quota import QuotaGate, block_delay_seconds, quota_released_signal

from .conftest import (
    make_api_client,
    make_mock_response,
    make_mock_session,
    make_quota_error,
    make_usage_payload,
)

RESET_AT = datetime(2026, 10, 1, tzinfo=UTC)
ENTRY_ID = "quota_entry"


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


def _quota_response(kind: str = "live_activity_updates", *, server_now: datetime | None = None, **overrides):
    headers = {"Retry-After": format_datetime(RESET_AT, usegmt=True)}
    if server_now is not None:
        headers["Date"] = format_datetime(server_now, usegmt=True)
    return make_mock_response(429, text=_quota_body(kind, **overrides), headers=headers)


def _config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: "test-key"},
        version=2,
        unique_id=ENTRY_ID,
        entry_id=ENTRY_ID,
    )


def _error(**overrides) -> PushWardQuotaExceededError:
    return make_quota_error(**{"reset_at": RESET_AT, **overrides})


def _released(hass: HomeAssistant) -> list[str]:
    """Collect the kinds the gate announces on the dispatcher."""
    seen: list[str] = []
    async_dispatcher_connect(hass, quota_released_signal(ENTRY_ID), seen.append)
    return seen


def _paused(gate: QuotaGate, kind: str) -> bool:
    return kind in gate.snapshot()


# --- contract constants ---


def test_metered_resources_name_the_server_kinds() -> None:
    assert [r.kind for r in USAGE_LIMIT_RESOURCES] == [
        "notifications",
        "live_activity_updates",
        "widget_updates",
        "emails",
    ]
    assert USAGE_LIMIT_RESOURCES[1].used_key == "live_activity_updates_used"
    assert USAGE_LIMIT_RESOURCES[1].limit_key == "live_activity_updates_limit"


def test_quota_error_message_is_plain_ascii() -> None:
    err = _error()
    assert str(err) == "PushWard live_activity_updates quota exhausted (250/250), resets 2026-10-01 00:00 UTC"
    assert err.status_code == 429
    assert isinstance(err, PushWardApiError)
    assert str(make_quota_error("emails", used=None, limit=None)) == "PushWard emails quota exhausted"
    assert (
        str(make_quota_error("emails", used=None, limit=None, reset_at=RESET_AT))
        == "PushWard emails quota exhausted, resets 2026-10-01 00:00 UTC"
    )


# --- delay computation ---


def test_block_delay_uses_server_clock_when_known() -> None:
    assert block_delay_seconds(RESET_AT, RESET_AT - timedelta(hours=3)) == pytest.approx(3 * 3600)


def test_block_delay_floors_a_past_reset() -> None:
    assert block_delay_seconds(RESET_AT, RESET_AT + timedelta(minutes=5)) == QUOTA_BLOCK_MIN_SECONDS


def test_block_delay_falls_back_without_or_with_implausible_reset() -> None:
    assert block_delay_seconds(None, None) == QUOTA_BLOCK_FALLBACK_SECONDS
    assert block_delay_seconds(RESET_AT + timedelta(days=400), RESET_AT) == QUOTA_BLOCK_FALLBACK_SECONDS


def test_block_delay_uses_local_clock_without_date_header() -> None:
    reset_at = dt_util.utcnow() + timedelta(hours=2)
    assert 2 * 3600 - 5 < block_delay_seconds(reset_at, None) <= 2 * 3600


def test_parse_http_date_is_always_aware() -> None:
    assert parse_http_date(None) is None
    assert parse_http_date("nonsense") is None
    aware = parse_http_date("Wed, 01 Oct 2026 00:00:00 GMT")
    naive_zone = parse_http_date("Wed, 01 Oct 2026 00:00:00 -0000")
    assert aware == naive_zone == RESET_AT


# --- client: a quota 429 is typed, not retried; a rate-limit 429 still is ---


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_quota_429_raises_typed_error_after_one_request(mock_sleep) -> None:
    session = make_mock_session(_quota_response())
    client = make_api_client(session)

    with pytest.raises(PushWardQuotaExceededError) as excinfo:
        await client.update_activity("ha-washer", "ongoing", {"progress": 0.5})

    err = excinfo.value
    assert (err.kind, err.used, err.limit, err.reset_at) == ("live_activity_updates", 250, 250, RESET_AT)
    assert session.request.call_count == 1
    mock_sleep.assert_not_called()


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_quota_429_kind_comes_from_the_endpoint(mock_sleep) -> None:
    """The client knows which quota each endpoint spends; the body only adds the counters."""
    session = make_mock_session(_quota_response("notifications", reset_at=None, used=None, limit=None))
    client = make_api_client(session)

    with pytest.raises(PushWardQuotaExceededError) as excinfo:
        await client.create_notification("Hi", "there")

    assert excinfo.value.kind == "notifications"
    assert excinfo.value.reset_at is None
    assert str(excinfo.value) == "PushWard notifications quota exhausted"


# --- gate + client ---


async def test_gate_blocks_same_kind_without_a_request(hass: HomeAssistant) -> None:
    gate = QuotaGate(hass, ENTRY_ID)
    session = make_mock_session(_quota_response(server_now=RESET_AT - timedelta(days=3)))
    client = PushWardApiClient(session, "https://api.example.com", "k", quota_gate=gate)

    with pytest.raises(PushWardQuotaExceededError):
        await client.create_activity("ha-washer", "Washer", 1)
    with pytest.raises(PushWardQuotaExceededError) as excinfo:
        await client.update_activity("ha-washer", "ongoing", {"progress": 0.5})

    assert session.request.call_count == 1
    assert (excinfo.value.kind, excinfo.value.used) == ("live_activity_updates", 250)
    assert gate.snapshot() == {"live_activity_updates": RESET_AT.isoformat()}
    gate.async_shutdown()


async def test_gate_lets_unmetered_and_other_kinds_through(hass: HomeAssistant) -> None:
    gate = QuotaGate(hass, ENTRY_ID)
    session = make_mock_session(_quota_response("widget_updates"), make_mock_response(200), make_mock_response(204))
    client = PushWardApiClient(session, "https://api.example.com", "k", quota_gate=gate)

    with pytest.raises(PushWardQuotaExceededError):
        await client.patch_widget("ha-users", {"content": {}})
    # POST /widgets is not metered and a different kind is not paused.
    await client.create_widget(slug="ha-users", template="value", name="Users", content={})
    await client.delete_widget("ha-users")

    assert session.request.call_count == 3
    gate.async_shutdown()


async def test_gate_arm_warns_once_and_asks_the_coordinator(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    gate = QuotaGate(hass, ENTRY_ID)
    wakeup = AsyncMock()
    gate.wakeup = wakeup

    with caplog.at_level(logging.DEBUG, logger="custom_components.pushward.quota"):
        for _ in range(3):
            gate.arm(_error())
    await hass.async_block_till_done()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "pausing live_activity_updates requests" in warnings[0].getMessage()
    # One refresh for the transition; the coordinator raises the repair issue from it.
    assert wakeup.await_count == 1
    gate.async_shutdown()


async def test_gate_rearms_only_when_reset_moves_later(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    gate = QuotaGate(hass, ENTRY_ID)

    with caplog.at_level(logging.WARNING, logger="custom_components.pushward.quota"):
        gate.arm(_error())
        # Same reset, different jitter draw: still the same pause.
        with patch("custom_components.pushward.quota.random.uniform", return_value=QUOTA_RELEASE_JITTER_SECONDS):
            gate.arm(_error())
        gate.arm(_error(reset_at=RESET_AT + timedelta(days=10)))

    assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 2
    assert gate.snapshot()["live_activity_updates"] == (RESET_AT + timedelta(days=10)).isoformat()
    gate.async_shutdown()


async def test_gate_release_announces_kind_once(hass: HomeAssistant) -> None:
    gate = QuotaGate(hass, ENTRY_ID)
    seen = _released(hass)

    gate.arm(make_quota_error("widget_updates"))
    gate.release("widget_updates")
    gate.release("widget_updates")
    gate.release("emails")
    await hass.async_block_till_done()

    assert seen == ["widget_updates"]
    assert not _paused(gate, "widget_updates")


async def test_gate_timer_without_coordinator_releases(hass: HomeAssistant) -> None:
    gate = QuotaGate(hass, ENTRY_ID)
    seen = _released(hass)

    gate.arm(_error(reset_at=dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS + 30)))
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS + 30 + QUOTA_RELEASE_JITTER_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert seen == ["live_activity_updates"]
    assert not _paused(gate, "live_activity_updates")


async def test_gate_timer_asks_coordinator_which_releases(hass: HomeAssistant) -> None:
    """Reset wake-up: refresh /auth/me; only an under-limit reading releases and clears the issue."""
    entry = _config_entry()
    entry.add_to_hass(hass)
    gate = QuotaGate(hass, ENTRY_ID)
    seen = _released(hass)
    api = AsyncMock()
    api.get_me = AsyncMock(
        return_value=make_usage_payload(live_activity_updates_used=250, live_activity_updates_limit=250)
    )
    coordinator = PushWardUsageCoordinator(hass, api, entry, gate)
    issue_id = usage_limit_issue_id(ENTRY_ID, "live_activity_updates_used")

    gate.arm(_error(reset_at=dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS)))
    await hass.async_block_till_done()
    # Arming already asked for a refresh, which raised the repair issue from real counters.
    assert api.get_me.await_count == 1
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {"used": "250", "limit": "250", "resets_at": "2026-07-01"}

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS + QUOTA_RELEASE_JITTER_SECONDS + 1)
    )
    await hass.async_block_till_done()

    # Still capped server-side (clock skew, late rollover): stay paused.
    assert api.get_me.await_count == 2
    assert seen == []
    assert _paused(gate, "live_activity_updates")

    api.get_me.return_value = make_usage_payload(live_activity_updates_used=3, live_activity_updates_limit=250)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert seen == ["live_activity_updates"]
    assert not _paused(gate, "live_activity_updates")
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    gate.async_shutdown()


async def test_gate_shutdown_cancels_timers(hass: HomeAssistant) -> None:
    gate = QuotaGate(hass, ENTRY_ID)
    seen = _released(hass)

    gate.arm(_error(reset_at=dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS)))
    gate.async_shutdown()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1))
    await hass.async_block_till_done()

    assert seen == []
    assert gate.snapshot() == {}


async def test_gate_late_timer_lets_a_probe_through(hass: HomeAssistant) -> None:
    """If the wake-up is somehow late, the next request probes the server instead of waiting."""
    gate = QuotaGate(hass, ENTRY_ID)
    gate.arm(_error())
    with patch.object(hass.loop, "time", return_value=hass.loop.time() + 60 * 86400):
        assert gate.blocked("live_activity_updates") is None
    assert gate.snapshot() == {}


async def test_gate_hands_out_a_fresh_error_per_refusal(hass: HomeAssistant) -> None:
    """Re-raising one stored instance would grow its traceback for the whole period."""
    gate = QuotaGate(hass, ENTRY_ID)
    gate.arm(_error())
    first = gate.blocked("live_activity_updates")
    second = gate.blocked("live_activity_updates")
    assert first is not second
    assert (first.kind, first.used, first.limit, first.reset_at) == (second.kind, 250, 250, RESET_AT)
    gate.async_shutdown()


# --- config entry integration ---


async def test_setup_entry_wires_gate_end_to_end(hass: HomeAssistant) -> None:
    """Arm -> repair issue + diagnostics -> timer -> /auth/me under limit -> release -> unload cancels."""
    entry = _config_entry()
    entry.add_to_hass(hass)
    api = AsyncMock()
    capped = make_usage_payload(widget_updates_used=50, widget_updates_limit=50)
    api.get_me = AsyncMock(return_value=capped)

    with patch("custom_components.pushward.PushWardApiClient", return_value=api) as client_cls:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    gate: QuotaGate = hass.data[DOMAIN][entry.entry_id]["quota_gate"]
    assert client_cls.call_args.kwargs["quota_gate"] is gate
    seen = _released(hass)
    api.get_me.reset_mock()

    gate.arm(make_quota_error("widget_updates", reset_at=dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS)))
    await hass.async_block_till_done()
    assert api.get_me.await_count == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, usage_limit_issue_id(ENTRY_ID, "widget_updates_used")) is not None
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert list(diag["quota_blocks"]) == ["widget_updates"]

    api.get_me.return_value = make_usage_payload()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=QUOTA_BLOCK_MIN_SECONDS + QUOTA_RELEASE_JITTER_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert api.get_me.await_count == 2
    assert seen == ["widget_updates"]
    assert not _paused(gate, "widget_updates")
    assert ir.async_get(hass).async_get_issue(DOMAIN, usage_limit_issue_id(ENTRY_ID, "widget_updates_used")) is None

    api.get_me.return_value = capped
    gate.arm(make_quota_error("emails", reset_at=RESET_AT))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert gate.snapshot() == {}
