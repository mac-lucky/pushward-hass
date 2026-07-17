"""Tests for the PushWard API client."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.pushward.api import (
    PushWardApiClient,
    PushWardApiError,
    PushWardAuthError,
    PushWardEmailPermissionError,
    PushWardForbiddenError,
)
from custom_components.pushward.const import (
    MAX_CONCURRENT_REQUESTS,
    MAX_RETRIES,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
)

from .conftest import make_api_client as _make_client
from .conftest import make_mock_response as _mock_response
from .conftest import make_mock_session as _make_session

# --- validate_connection ---


async def test_validate_connection_success():
    payload = {"id": "u1"}
    session = _make_session(_mock_response(200, json_body=payload))

    client = _make_client(session)
    result = await client.validate_connection()

    assert result is True
    session.request.assert_called_once()
    call = session.request.call_args
    assert call[0][0] == "GET"
    assert "/auth/me" in call[0][1]
    assert call[1]["headers"]["Authorization"] == "Bearer test-key"


async def test_validate_connection_auth_error():
    session = _make_session(_mock_response(401))
    client = _make_client(session)
    with pytest.raises(PushWardAuthError):
        await client.validate_connection()


# --- get_me ---


async def test_get_me_returns_usage_dict():
    payload = {"id": "u1", "subscribed": False, "notifications_used": 7, "notifications_limit": 500}
    session = _make_session(_mock_response(200, json_body=payload))
    client = _make_client(session)

    result = await client.get_me()

    assert result == payload
    call = session.request.call_args
    assert call[0][0] == "GET"
    assert "/auth/me" in call[0][1]
    assert call[1]["headers"]["Authorization"] == "Bearer test-key"


async def test_get_me_auth_error():
    """403 on /auth/me means a bad/expired key, not a policy rejection."""
    session = _make_session(_mock_response(403))
    client = _make_client(session)
    with pytest.raises(PushWardAuthError):
        await client.get_me()


async def test_get_me_rejects_non_dict_body():
    resp = _mock_response(200)
    resp.json = AsyncMock(return_value=["not", "a", "dict"])
    session = _make_session(resp)
    client = _make_client(session)
    with pytest.raises(PushWardApiError):
        await client.get_me()


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_get_me_retries_429(mock_sleep):
    """A transient 429 on the usage poll retries instead of failing the cycle."""
    payload = {"id": "u1", "notifications_used": 7}
    session = _make_session(
        _mock_response(429, headers={"Retry-After": "1"}),
        _mock_response(200, json_body=payload),
    )
    client = _make_client(session)

    result = await client.get_me()

    assert result == payload
    assert session.request.call_count == 2


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_get_me_429_exhaustion_is_typed(mock_sleep):
    session = _make_session(*[_mock_response(429) for _ in range(MAX_RETRIES)])
    client = _make_client(session)

    with pytest.raises(PushWardApiError) as excinfo:
        await client.get_me()

    assert excinfo.value.status_code == 429


# --- create_activity ---


async def test_create_activity_success():
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.create_activity("test-slug", "Test", priority=1, ended_ttl=300, stale_ttl=1800, dismissal_ttl=45)

    session.request.assert_called_once()
    call_args = session.request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[0][1].endswith("/activities")
    body = call_args[1]["json"]
    assert body["slug"] == "test-slug"
    assert body["name"] == "Test"
    assert body["priority"] == 1
    assert body["ended_ttl"] == 300
    assert body["stale_ttl"] == 1800
    assert body["dismissal_ttl"] == 45


async def test_create_activity_already_exists():
    body = (
        '{"type":"about:blank","title":"Conflict","status":409,'
        '"detail":"activity already exists","code":"activity.already_exists"}'
    )
    resp = _mock_response(409, text=body)
    session = _make_session(resp)
    client = _make_client(session)

    # Should not raise
    await client.create_activity("test-slug", "Test", priority=1, ended_ttl=300, stale_ttl=1800)


async def test_create_activity_limit():
    body = (
        '{"type":"about:blank","title":"Conflict","status":409,'
        '"detail":"activity limit reached","code":"activity.limit_exceeded"}'
    )
    resp = _mock_response(409, text=body)
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardApiError, match="limit"):
        await client.create_activity("test-slug", "Test", priority=1, ended_ttl=300, stale_ttl=1800)


async def test_create_activity_optional_ttls():
    """TTLs are omitted from JSON body when None."""
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.create_activity("test-slug", "Test", priority=1)

    body = session.request.call_args[1]["json"]
    assert "ended_ttl" not in body
    assert "stale_ttl" not in body
    assert "dismissal_ttl" not in body


async def test_create_activity_partial_ttls():
    """Only non-None TTLs are included in JSON body."""
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.create_activity("test-slug", "Test", priority=1, ended_ttl=600, dismissal_ttl=0)

    body = session.request.call_args[1]["json"]
    assert body["ended_ttl"] == 600
    assert "stale_ttl" not in body
    # dismissal_ttl=0 is meaningful (immediate removal) and must be sent, not dropped.
    assert body["dismissal_ttl"] == 0


# --- update_activity ---


async def test_update_activity_success():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.update_activity("test-slug", "ongoing", {"progress": 0.5})

    call_args = session.request.call_args
    assert call_args[0][0] == "PATCH"
    assert "/activities/test-slug" in call_args[0][1]
    assert call_args[1]["json"] == {"state": "ongoing", "content": {"progress": 0.5}}


# --- delete_activity ---


async def test_delete_activity_success():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.delete_activity("test-slug")

    call_args = session.request.call_args
    assert call_args[0][0] == "DELETE"
    assert "/activities/test-slug" in call_args[0][1]


async def test_delete_activity_not_found():
    resp = _mock_response(404)
    session = _make_session(resp)
    client = _make_client(session)

    # 404 should be treated as success
    await client.delete_activity("test-slug")


# --- create_notification ---


async def test_create_notification_required_fields():
    """create_notification sends title, body, and push to POST /notifications."""
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.create_notification("Door Opened", "The front door was opened.")

    session.request.assert_called_once()
    call_args = session.request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[0][1].endswith("/notifications")
    body = call_args[1]["json"]
    assert body["title"] == "Door Opened"
    assert body["body"] == "The front door was opened."
    assert body["push"] is True


async def test_create_notification_all_fields():
    """create_notification includes all optional fields in payload."""
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.create_notification(
        "Alert",
        "Motion detected",
        subtitle="Front Yard",
        level="time-sensitive",
        volume=0.8,
        thread_id="security",
        collapse_id="motion-front",
        source="home-assistant",
        source_display_name="Home Assistant",
        activity_slug="ha-motion",
        push=False,
    )

    body = session.request.call_args[1]["json"]
    assert body["title"] == "Alert"
    assert body["body"] == "Motion detected"
    assert body["subtitle"] == "Front Yard"
    assert body["level"] == "time-sensitive"
    assert body["volume"] == 0.8
    assert body["thread_id"] == "security"
    assert body["collapse_id"] == "motion-front"
    assert body["source"] == "home-assistant"
    assert body["source_display_name"] == "Home Assistant"
    assert body["activity_slug"] == "ha-motion"
    assert body["push"] is False


async def test_create_notification_omits_none_fields():
    """Optional fields set to None are not included in the JSON payload."""
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.create_notification("Test", "Hello")

    body = session.request.call_args[1]["json"]
    assert set(body.keys()) == {"title", "body", "push"}


# --- send_email ---


async def test_send_email_text_body():
    """send_email POSTs to /emails with to/subject/text_body."""
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.send_email("alerts@example.com", "Deploy done", text_body="Succeeded.")

    session.request.assert_called_once()
    call_args = session.request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[0][1].endswith("/emails")
    body = call_args[1]["json"]
    assert body == {"to": "alerts@example.com", "subject": "Deploy done", "text_body": "Succeeded."}


async def test_send_email_html_and_text():
    """Both bodies are included in the payload when provided."""
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.send_email("alerts@example.com", "Report", text_body="plain", html_body="<p>html</p>")

    body = session.request.call_args[1]["json"]
    assert body["text_body"] == "plain"
    assert body["html_body"] == "<p>html</p>"


async def test_send_email_omits_none_bodies():
    """Body fields set to None are not included in the payload."""
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.send_email("alerts@example.com", "Subj", html_body="<p>x</p>")

    body = session.request.call_args[1]["json"]
    assert set(body.keys()) == {"to", "subject", "html_body"}


# --- retry ---


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_retry_on_server_error(mock_sleep):
    resp_500 = _mock_response(500, text="Internal Server Error")
    resp_200 = _mock_response(200)
    session = _make_session(resp_500, resp_200)
    client = _make_client(session)

    await client.update_activity("test-slug", "ongoing", {"progress": 0.5})

    assert session.request.call_count == 2
    mock_sleep.assert_called_once()


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_retry_on_429_with_retry_after(mock_sleep):
    resp_429 = _mock_response(429, headers={"Retry-After": "2"})
    resp_200 = _mock_response(200)
    session = _make_session(resp_429, resp_200)
    client = _make_client(session)

    await client.update_activity("test-slug", "ongoing", {"progress": 0.5})

    assert session.request.call_count == 2
    # Should sleep for the Retry-After value (2 seconds)
    mock_sleep.assert_any_call(2.0)


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_no_retry_on_client_error(mock_sleep):
    body = '{"type":"about:blank","title":"Bad Request","status":400,"detail":"Bad Request","code":"validation.failed"}'
    resp_400 = _mock_response(400, text=body)
    session = _make_session(resp_400)
    client = _make_client(session)

    with pytest.raises(PushWardApiError, match="400") as excinfo:
        await client.update_activity("test-slug", "ongoing", {"progress": 0.5})

    # Response body must be surfaced on the exception so HA logs show the real reason.
    assert "Bad Request" in str(excinfo.value)
    assert session.request.call_count == 1
    mock_sleep.assert_not_called()


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_4xx_error_body_truncated_in_exception(mock_sleep):
    """Large Problem.detail values are truncated to 200 chars + ellipsis in the exception."""
    long_detail = "x" * 500
    body = json.dumps(
        {
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": long_detail,
            "code": "validation.failed",
        }
    )
    resp = _mock_response(400, text=body)
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardApiError) as excinfo:
        await client.update_activity("test-slug", "ongoing", {"progress": 0.5})

    msg = str(excinfo.value)
    assert "…" in msg
    assert "x" * 200 in msg
    assert "x" * 201 not in msg


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_all_429_exhaustion_raises_api_error(mock_sleep):
    """Exhausting every retry on 429 raises a typed error, not TypeError from `raise None`."""
    session = _make_session(*[_mock_response(429) for _ in range(MAX_RETRIES)])
    client = _make_client(session)

    with pytest.raises(PushWardApiError, match="429") as excinfo:
        await client.update_activity("test-slug", "ongoing", {"progress": 0.5})

    assert excinfo.value.status_code == 429
    assert session.request.call_count == MAX_RETRIES
    # No wasted sleep after the final attempt.
    assert mock_sleep.await_count == MAX_RETRIES - 1


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_all_5xx_exhaustion_raises_api_error(mock_sleep):
    session = _make_session(*[_mock_response(500, text="boom") for _ in range(MAX_RETRIES)])
    client = _make_client(session)

    with pytest.raises(PushWardApiError, match="500") as excinfo:
        await client.update_activity("test-slug", "ongoing", {"progress": 0.5})

    assert excinfo.value.status_code == 500
    assert session.request.call_count == MAX_RETRIES


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_retry_after_http_date(mock_sleep):
    """Retry-After in HTTP-date form is honored as a relative delay."""
    header = format_datetime(datetime.now(UTC) + timedelta(seconds=5), usegmt=True)
    session = _make_session(
        _mock_response(429, headers={"Retry-After": header}),
        _mock_response(200),
    )
    client = _make_client(session)

    await client.update_activity("test-slug", "ongoing", {"progress": 0.5})

    delay = mock_sleep.call_args_list[0].args[0]
    assert 0 < delay <= 5


def test_parse_retry_after_clamped_to_max():
    """Retry-After is clamped to RETRY_MAX_DELAY so a hostile/large value can't park a slot.

    The request+retry loop sleeps while holding a shared concurrency-semaphore slot, so an
    unbounded Retry-After (numeric or an HTTP-date far in the future) must not exceed the cap.
    """
    parse = PushWardApiClient._parse_retry_after
    # Small honest values pass through untouched.
    assert parse("5") == 5.0
    # A large numeric value is clamped to the cap, not obeyed literally.
    assert parse("600") == RETRY_MAX_DELAY
    assert parse(str(RETRY_MAX_DELAY + 1)) == RETRY_MAX_DELAY
    # An HTTP-date far in the future clamps to the cap as well.
    future = format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True)
    assert parse(future) == RETRY_MAX_DELAY
    # Empty / unparseable headers fall back to 0 (caller then uses its own backoff).
    assert parse("") == 0
    assert parse("not-a-date") == 0
    # NaN parses via float() but must be rejected: asyncio.sleep(nan) corrupts the loop timer.
    assert parse("nan") == 0
    # A negative delay clamps to 0 rather than sleeping a nonsensical negative time.
    assert parse("-5") == 0


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_retry_after_large_value_clamped_on_429(mock_sleep):
    """A 429 with a large Retry-After sleeps at most RETRY_MAX_DELAY, not the raw header."""
    session = _make_session(
        _mock_response(429, headers={"Retry-After": "600"}),
        _mock_response(200),
    )
    client = _make_client(session)

    await client.update_activity("test-slug", "ongoing", {"progress": 0.5})

    delay = mock_sleep.call_args_list[0].args[0]
    assert delay == RETRY_MAX_DELAY


def test_backoff_delay_jitter_bounds():
    """Backoff stays within [base/2, base] so retries never sync in lockstep."""
    for attempt in range(6):
        base = min(RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY)
        for _ in range(50):
            delay = PushWardApiClient._backoff_delay(attempt)
            assert base * 0.5 <= delay <= base


async def test_4xx_problem_detail_surfaced():
    """Problem.detail is preferred over raw body in the exception message."""
    body = (
        '{"type":"about:blank","title":"Bad Request","status":400,"detail":"slug too long","code":"validation.failed"}'
    )
    resp = _mock_response(400, text=body)
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardApiError, match="slug too long"):
        await client.update_activity("slug", "ongoing", {"template": "generic"})


async def test_4xx_non_json_body_falls_back_to_raw():
    """When the body isn't JSON, the raw text is used as the error snippet."""
    resp = _mock_response(400, text="plain text error")
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardApiError, match="plain text error"):
        await client.update_activity("slug", "ongoing", {"template": "generic"})


# --- semaphore concurrency cap ---


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_semaphore_caps_concurrency(mock_sleep):
    """Concurrent API calls are capped at MAX_CONCURRENT_REQUESTS."""
    lock = asyncio.Lock()
    current = 0
    peak = 0
    resp = _mock_response(200)

    class _SlowContextManager:
        async def __aenter__(self_cm):
            nonlocal current, peak
            async with lock:
                current += 1
                if current > peak:
                    peak = current
            # Yield control so other tasks can attempt to enter concurrently
            await asyncio.sleep(0.01)
            return resp

        async def __aexit__(self_cm, *exc):
            nonlocal current
            async with lock:
                current -= 1
            return False

    session = AsyncMock(spec=aiohttp.ClientSession)
    session.request = MagicMock(side_effect=lambda *a, **kw: _SlowContextManager())
    client = _make_client(session)

    await asyncio.gather(*(client.update_activity(f"slug-{i}", "ongoing", {"i": i}) for i in range(10)))

    assert peak <= MAX_CONCURRENT_REQUESTS
    assert session.request.call_count == 10


# --- 403 demux ---


async def test_forbidden_403_subscription_raises_forbidden():
    body = (
        '{"type":"about:blank","title":"Forbidden","status":403,'
        '"detail":"account owner\'s subscription is not active",'
        '"code":"subscription.required"}'
    )
    resp = _mock_response(403, text=body)
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardForbiddenError) as excinfo:
        await client.update_activity("slug", "ongoing", {"template": "generic"})

    assert "subscription" in str(excinfo.value)
    assert excinfo.value.status_code == 403


async def test_forbidden_403_slug_scope_raises_forbidden():
    body = (
        '{"type":"about:blank","title":"Forbidden","status":403,'
        '"detail":"key not allowed for this activity",'
        '"code":"activity.not_in_scope"}'
    )
    resp = _mock_response(403, text=body)
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardForbiddenError) as excinfo:
        await client.update_activity("slug", "ongoing", {"template": "generic"})

    assert excinfo.value.status_code == 403
    assert "key not allowed" in str(excinfo.value)


async def test_forbidden_403_with_empty_body_still_raises_forbidden():
    resp = _mock_response(403, text="")
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardForbiddenError) as excinfo:
        await client.update_activity("slug", "ongoing", {"template": "generic"})

    assert "Forbidden" in str(excinfo.value)
    assert excinfo.value.status_code == 403


async def test_unauthorized_401_still_raises_auth_error():
    resp = _mock_response(401)
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardAuthError):
        await client.update_activity("slug", "ongoing", {"template": "generic"})


async def test_forbidden_403_emails_raises_email_permission_error():
    """403 on /emails (unverified recipient or missing capability) raises
    PushWardEmailPermissionError with the server detail surfaced."""
    body = (
        '{"type":"about:blank","title":"Forbidden","status":403,'
        '"detail":"recipient is not a verified address for this account",'
        '"code":"email.recipient_not_verified"}'
    )
    resp = _mock_response(403, text=body)
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardEmailPermissionError) as excinfo:
        await client.send_email("alerts@example.com", "Subj", text_body="hi")

    assert "verified address" in str(excinfo.value)
    assert excinfo.value.status_code == 403


# --- sound / priority top-level fields ---


async def test_update_activity_sends_sound_top_level():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.update_activity("slug", "ongoing", {"template": "generic"}, sound="chime")

    body = session.request.call_args[1]["json"]
    assert body == {"state": "ongoing", "content": {"template": "generic"}, "sound": "chime"}


async def test_update_activity_sends_priority_top_level():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.update_activity("slug", "ongoing", {"template": "generic"}, priority=7)

    body = session.request.call_args[1]["json"]
    assert body == {"state": "ongoing", "content": {"template": "generic"}, "priority": 7}


async def test_update_activity_sends_both_sound_and_priority():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.update_activity("slug", "ongoing", {"template": "generic"}, sound="chime", priority=7)

    body = session.request.call_args[1]["json"]
    assert body == {"state": "ongoing", "content": {"template": "generic"}, "sound": "chime", "priority": 7}


async def test_update_activity_omits_sound_when_none():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.update_activity("slug", "ongoing", {"template": "generic"})

    body = session.request.call_args[1]["json"]
    assert "sound" not in body


async def test_update_activity_omits_priority_when_none():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.update_activity("slug", "ongoing", {"template": "generic"})

    body = session.request.call_args[1]["json"]
    assert "priority" not in body


# --- patchable TTLs top-level ---


async def test_update_activity_sends_ttls_top_level():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.update_activity(
        "slug", "ongoing", {"template": "generic"}, ended_ttl=300, stale_ttl=1800, dismissal_ttl=60
    )

    body = session.request.call_args[1]["json"]
    assert body["ended_ttl"] == 300
    assert body["stale_ttl"] == 1800
    assert body["dismissal_ttl"] == 60
    # TTLs are top-level PATCH fields, never content.
    assert "ended_ttl" not in body["content"]
    assert "dismissal_ttl" not in body["content"]


async def test_update_activity_omits_ttls_when_none():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.update_activity("slug", "ongoing", {"template": "generic"})

    body = session.request.call_args[1]["json"]
    assert "ended_ttl" not in body
    assert "stale_ttl" not in body
    assert "dismissal_ttl" not in body


async def test_update_activity_sends_dismissal_ttl_zero():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    # 0 means "remove immediately on end", a meaningful value that must not be dropped.
    await client.update_activity("slug", "ongoing", {"template": "generic"}, dismissal_ttl=0)

    body = session.request.call_args[1]["json"]
    assert body["dismissal_ttl"] == 0


async def test_update_activity_sends_partial_ttls():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.update_activity("slug", "ongoing", {"template": "generic"}, stale_ttl=1800)

    body = session.request.call_args[1]["json"]
    assert body["stale_ttl"] == 1800
    assert "ended_ttl" not in body
    assert "dismissal_ttl" not in body


# --- exception hierarchy ---


def test_forbidden_exception_is_subclass_of_api_error():
    assert issubclass(PushWardForbiddenError, PushWardApiError)
    assert not issubclass(PushWardForbiddenError, PushWardAuthError)


def test_email_permission_error_is_subclass_of_forbidden():
    assert issubclass(PushWardEmailPermissionError, PushWardForbiddenError)
