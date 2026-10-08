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
    PushWardNotFoundError,
    PushWardQuotaExceededError,
    PushWardRateLimitedError,
)
from custom_components.pushward.const import (
    ANSWER_FAILURE_BUDGET,
    ANSWER_LONG_POLL_SECONDS,
    ANSWER_MAX_CONCURRENT_WAITS,
    ANSWER_MIN_POLL_INTERVAL,
    MAX_CONCURRENT_REQUESTS,
    MAX_RETRIES,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
)
from custom_components.pushward.e2e import E2EError, key_id, open_envelope

from .conftest import make_api_client as _make_client
from .conftest import make_mock_response as _mock_response
from .conftest import make_mock_session as _make_session
from .server_contract import (
    assert_valid_notification_receipt,
    assert_valid_notification_request,
    assert_valid_receipts_canceled,
)

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


async def test_create_notification_returns_created_notification():
    resp = _mock_response(201, json_body={"id": 991, "title": "t"})
    client = _make_client(_make_session(resp))

    assert await client.create_notification("t", "b") == {"id": 991, "title": "t"}


async def test_create_notification_with_send_at_schedules():
    """send_at posts to /notifications/scheduled with an RFC 3339 send_at."""
    resp = _mock_response(201, json_body={"id": 42, "status": "scheduled"})
    session = _make_session(resp)
    client = _make_client(session)

    result = await client.create_notification(
        "Bins",
        "Tonight",
        send_at=datetime(2026, 10, 1, 16, 0, tzinfo=UTC),
        source="home",
    )

    assert result == {"id": 42, "status": "scheduled"}
    call_args = session.request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[0][1].endswith("/notifications/scheduled")
    assert call_args[1]["json"] == {
        "title": "Bins",
        "body": "Tonight",
        "push": True,
        "source": "home",
        "send_at": "2026-10-01T16:00:00+00:00",
    }


async def test_create_notification_requires_aware_send_at():
    client = _make_client(_make_session(_mock_response(201)))
    with pytest.raises(ValueError):
        await client.create_notification("t", "b", send_at=datetime(2026, 10, 1, 16, 0))


_E2E_KEY = bytes(range(32))


async def test_create_notification_with_e2e_key_sends_only_the_envelope():
    """title, subtitle, body and url travel only inside `encrypted`; the rest stays plain."""
    session = _make_session(_mock_response(201))
    client = _make_client(session)
    client.e2e_key = _E2E_KEY

    await client.create_notification(
        "Door",
        "Opened",
        subtitle="Hall",
        url="https://ha.example.com/lovelace",
        level="active",
        thread_id="security",
        metadata={"entity_id": "binary_sensor.door"},
    )

    call_args = session.request.call_args
    assert call_args[0][1].endswith("/notifications")
    body = call_args[1]["json"]
    assert set(body) == {"encrypted", "push", "level", "thread_id", "metadata"}
    assert_valid_notification_request(body)
    assert body["encrypted"].startswith(f"pw1.{key_id(_E2E_KEY)}.")
    assert open_envelope(_E2E_KEY, body["encrypted"]) == {
        "title": "Door",
        "subtitle": "Hall",
        "body": "Opened",
        "url": "https://ha.example.com/lovelace",
    }


async def test_create_notification_with_e2e_key_seals_scheduled_sends():
    session = _make_session(_mock_response(201, json_body={"id": 42, "status": "scheduled"}))
    client = _make_client(session)
    client.e2e_key = _E2E_KEY

    await client.create_notification("Bins", "Tonight", send_at=datetime(2026, 10, 1, 16, 0, tzinfo=UTC))

    call_args = session.request.call_args
    assert call_args[0][1].endswith("/notifications/scheduled")
    body = call_args[1]["json"]
    assert set(body) == {"encrypted", "push", "send_at"}
    assert open_envelope(_E2E_KEY, body["encrypted"]) == {"title": "Bins", "body": "Tonight"}


async def test_create_notification_sends_the_acknowledge_fields():
    receipt = {
        "notification_id": 991,
        "status": "active",
        "repeat_seconds": 120,
        "expires_at": "2026-10-07T13:00:00Z",
        "repeats_sent": 0,
        "tags": ["garage"],
        "created_at": "2026-10-07T12:00:00Z",
    }
    session = _make_session(_mock_response(201, json_body={"id": 991, "answerable": True, "receipt": receipt}))
    client = _make_client(session)
    client.e2e_key = _E2E_KEY

    created = await client.create_notification(
        "Garage",
        "Still open",
        acknowledge={"repeat_seconds": 120},
        tags=["garage"],
        callback_url="https://hooks.example.com/pushward",
    )

    body = session.request.call_args[1]["json"]
    assert body["acknowledge"] == {"repeat_seconds": 120}
    assert body["tags"] == ["garage"]
    assert body["callback_url"] == "https://hooks.example.com/pushward"
    assert_valid_notification_request(body)
    assert_valid_notification_receipt(created["receipt"])


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_acknowledged_send_retries_under_one_collapse_id(mock_sleep):
    # A retry after a lost response must supersede the first receipt, not start a second one.
    session = _make_session(
        _mock_response(503, text="unavailable"),
        _mock_response(502, text="bad gateway"),
        _mock_response(201, json_body={"id": 7, "answerable": True}),
    )
    client = _make_client(session)

    await client.create_notification("Garage", "Still open", acknowledge={"repeat_seconds": 60})

    ids = [call[1]["json"].get("collapse_id") for call in session.request.call_args_list]
    assert len(ids) == 3
    assert ids[0]
    assert ids == [ids[0]] * 3
    assert_valid_notification_request(session.request.call_args[1]["json"])


async def test_acknowledged_send_gets_a_new_collapse_id_per_call():
    session = _make_session(_mock_response(201, json_body={"id": 1}), _mock_response(201, json_body={"id": 2}))
    client = _make_client(session)

    await client.create_notification("t", "b", acknowledge={})
    await client.create_notification("t", "b", acknowledge={})

    first, second = (call[1]["json"]["collapse_id"] for call in session.request.call_args_list)
    assert first != second


async def test_acknowledged_send_keeps_the_callers_collapse_id():
    session = _make_session(_mock_response(201, json_body={"id": 1}))
    client = _make_client(session)

    await client.create_notification("t", "b", acknowledge={}, collapse_id="garage-door")

    assert session.request.call_args[1]["json"]["collapse_id"] == "garage-door"


@pytest.mark.parametrize(
    ("kwargs", "acknowledge"),
    [
        ({}, None),
        ({"send_at": datetime(2030, 1, 1, tzinfo=UTC)}, {}),
        ({"recurrence": {"cron": "0 8 * * *", "timezone": "UTC"}}, {}),
    ],
    ids=["no-acknowledge", "scheduled", "recurring"],
)
async def test_only_immediate_acknowledged_sends_get_a_collapse_id(kwargs, acknowledge):
    session = _make_session(_mock_response(201, json_body={"id": 1}))
    client = _make_client(session)

    await client.create_notification("t", "b", acknowledge=acknowledge, **kwargs)

    assert "collapse_id" not in session.request.call_args[1]["json"]


async def test_cancel_notification_receipt_returns_the_receipt():
    receipt = {
        "notification_id": 991,
        "status": "canceled",
        "repeat_seconds": 60,
        "expires_at": "2026-10-07T13:00:00Z",
        "repeats_sent": 2,
        "canceled_at": "2026-10-07T12:03:00Z",
        "cancel_reason": "api",
        "created_at": "2026-10-07T12:00:00Z",
    }
    session = _make_session(_mock_response(200, json_body=receipt))
    client = _make_client(session)

    assert await client.cancel_notification_receipt(991) == receipt
    call_args = session.request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[0][1].endswith("/notifications/receipts/991/cancel")
    assert_valid_notification_receipt(receipt)


async def test_a_refusal_carries_the_problem_code():
    problem = json.dumps({"status": 422, "code": "notification.encryption_unavailable", "detail": "org key"})
    client = _make_client(_make_session(_mock_response(422, text=problem)))

    with pytest.raises(PushWardApiError) as exc_info:
        await client.create_notification("t", "b")
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "notification.encryption_unavailable"


def _problem(status: int, code: str) -> AsyncMock:
    body = {"status": status, "detail": "refused"}
    if code:
        body["code"] = code
    return _mock_response(status, text=json.dumps(body))


_ACK_REFUSAL_CASES = [
    (409, "notification_receipt.limit_exceeded", "receipt_limit"),
    (422, "notification_receipt.disabled", "receipts_disabled"),
    (422, "notification.answer_url_unavailable", "answer_url"),
    (422, "notification.encrypted_too_large", "encrypted_too_large"),
]


@pytest.mark.parametrize(("status", "code", "reason"), _ACK_REFUSAL_CASES)
async def test_refused_acknowledge_is_sent_once_without_it(status, code, reason, caplog):
    # The 25 receipts are per account: other senders can fill them, and the water
    # leak must still go out.
    session = _make_session(_problem(status, code), _mock_response(201, json_body={"id": 8, "answerable": True}))
    client = _make_client(session)

    created = await client.create_notification(
        "Water leak",
        "Kitchen sink",
        level="time-sensitive",
        collapse_id="leak",
        actions=[{"id": "open", "title": "Open", "url": "https://example.com"}],
        acknowledge={"repeat_seconds": 120},
        tags=["leak"],
        callback_url="https://hooks.example.com/pushward",
    )

    assert created == {"id": 8, "answerable": True, "acknowledge_refused": reason}
    first, second = (call[1]["json"] for call in session.request.call_args_list)
    assert first["acknowledge"] == {"repeat_seconds": 120}
    assert second == {key: val for key, val in first.items() if key not in ("acknowledge", "tags", "callback_url")}
    assert second["collapse_id"] == "leak"
    assert_valid_notification_request(second)
    assert reason in caplog.text
    assert "test-key" not in caplog.text


async def test_refused_acknowledge_resends_the_same_envelope():
    session = _make_session(
        _problem(409, "notification_receipt.limit_exceeded"), _mock_response(201, json_body={"id": 8})
    )
    client = _make_client(session)
    client.e2e_key = _E2E_KEY

    await client.create_notification("Water leak", "Kitchen sink", subtitle="Basement", acknowledge={})

    first, second = (call[1]["json"] for call in session.request.call_args_list)
    assert second["encrypted"] == first["encrypted"]
    assert not {"title", "body", "subtitle", "acknowledge"} & set(second)
    assert open_envelope(_E2E_KEY, second["encrypted"]) == {
        "title": "Water leak",
        "body": "Kitchen sink",
        "subtitle": "Basement",
    }
    assert_valid_notification_request(second)


async def test_refused_acknowledge_is_resent_only_once():
    session = _make_session(
        _problem(409, "notification_receipt.limit_exceeded"),
        _problem(400, "notification.invalid"),
    )
    client = _make_client(session)

    with pytest.raises(PushWardApiError) as exc_info:
        await client.create_notification("t", "b", acknowledge={})
    assert exc_info.value.code == "notification.invalid"
    assert session.request.call_count == 2


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_mock_response(401), PushWardAuthError),
        (_mock_response(403, text='{"detail": "subscription required"}'), PushWardForbiddenError),
        (
            _mock_response(429, text='{"code": "quota.exceeded", "kind": "notifications", "used": 5, "limit": 5}'),
            PushWardQuotaExceededError,
        ),
        (_problem(409, ""), PushWardApiError),
        (_problem(422, "notification.encryption_unavailable"), PushWardApiError),
        (_problem(400, ""), PushWardApiError),
        # The service schema already checked the acknowledge rules: these mean another field is wrong.
        (_problem(400, "notification.invalid"), PushWardApiError),
        (_problem(422, ""), PushWardApiError),
        (_problem(404, ""), PushWardNotFoundError),
    ],
    ids=["401", "403", "quota", "409-no-code", "422-other", "400-no-code", "400-invalid", "422-no-code", "404"],
)
async def test_other_refusals_of_an_acknowledged_send_are_not_resent(response, error):
    session = _make_session(response)
    client = _make_client(session)

    with pytest.raises(error):
        await client.create_notification("t", "b", acknowledge={})
    session.request.assert_called_once()


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_acknowledged_send_that_never_got_through_is_not_resent(mock_sleep):
    # 5xx and a lost connection: the server may already have taken one of the attempts.
    session = _make_session(*[_mock_response(503, text="unavailable")] * (MAX_RETRIES - 1))
    session.request.side_effect = [*session.request.side_effect, aiohttp.ClientConnectionError("reset")]
    client = _make_client(session)

    with pytest.raises(PushWardApiError):
        await client.create_notification("t", "b", acknowledge={})
    assert session.request.call_count == MAX_RETRIES
    assert all("acknowledge" in call[1]["json"] for call in session.request.call_args_list)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"send_at": datetime(2030, 1, 1, tzinfo=UTC), "acknowledge": {}}],
    ids=["no-acknowledge", "scheduled"],
)
async def test_refusal_without_an_acknowledged_send_now_is_not_resent(kwargs):
    session = _make_session(_problem(422, "notification_receipt.disabled"))
    client = _make_client(session)

    with pytest.raises(PushWardApiError) as exc_info:
        await client.create_notification("t", "b", **kwargs)
    assert exc_info.value.code == "notification_receipt.disabled"
    session.request.assert_called_once()


async def test_cancel_notification_receipt_without_one_is_not_found():
    client = _make_client(_make_session(_mock_response(404)))
    with pytest.raises(PushWardNotFoundError):
        await client.cancel_notification_receipt(991)


async def test_cancel_notification_receipts_by_tag_returns_the_count():
    session = _make_session(_mock_response(200, json_body={"canceled": 3}))
    client = _make_client(session)

    assert await client.cancel_notification_receipts_by_tag("garage") == 3
    call_args = session.request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[0][1].endswith("/notifications/receipts/cancel")
    assert call_args[1]["json"] == {"tag": "garage"}
    assert_valid_receipts_canceled({"canceled": 3})


async def test_create_notification_trim_to_fit_shortens_only_sealed_text():
    session = _make_session(_mock_response(201))
    await _make_client(session).create_notification("Shopping", "\u4e2d" * 1000, trim_to_fit=True)
    assert session.request.call_args[1]["json"]["body"] == "\u4e2d" * 1000

    session = _make_session(_mock_response(201))
    client = _make_client(session)
    client.e2e_key = _E2E_KEY
    await client.create_notification("Shopping", "\u4e2d" * 1000, trim_to_fit=True)
    body = session.request.call_args[1]["json"]
    assert_valid_notification_request(body)
    opened = open_envelope(_E2E_KEY, body["encrypted"])
    assert opened["title"] == "Shopping"
    assert 0 < len(opened["body"]) < 1000


@pytest.mark.parametrize(
    ("title", "body", "url"),
    [("", "b", None), ("t", "", None), ("t", "b", "javascript:alert(1)"), ("t", "x" * 3000, None)],
)
async def test_create_notification_with_e2e_key_refuses_before_sending(title, body, url):
    """What the server can no longer check once sealed is refused here, and nothing is sent."""
    session = _make_session(_mock_response(201))
    client = _make_client(session)
    client.e2e_key = _E2E_KEY

    with pytest.raises(E2EError):
        await client.create_notification(title, body, url=url)
    session.request.assert_not_called()


async def test_list_scheduled_notifications():
    resp = _mock_response(200, json_body={"items": [{"id": 3}]})
    session = _make_session(resp)
    client = _make_client(session)

    assert await client.list_scheduled_notifications("sent") == [{"id": 3}]
    call_args = session.request.call_args
    assert call_args[0][0] == "GET"
    assert call_args[0][1].endswith("/notifications/scheduled?status=sent&limit=100")


async def test_cancel_scheduled_notification_swallows_404():
    session = _make_session(_mock_response(404))
    client = _make_client(session)

    await client.cancel_scheduled_notification(42)

    call_args = session.request.call_args
    assert call_args[0][0] == "DELETE"
    assert call_args[0][1].endswith("/notifications/scheduled/42")


async def test_create_notification_with_recurrence_schedules_without_send_at():
    """recurrence alone posts to /notifications/scheduled with no send_at and until as RFC 3339."""
    session = _make_session(_mock_response(201, json_body={"id": 7, "status": "scheduled"}))
    client = _make_client(session)

    await client.create_notification(
        "Bins",
        "Tonight",
        recurrence={
            "cron": "0 19 * * 2",
            "timezone": "Europe/Warsaw",
            "until": datetime(2026, 12, 31, 23, 0, tzinfo=UTC),
            "count": None,
        },
    )

    call_args = session.request.call_args
    assert call_args[0][1].endswith("/notifications/scheduled")
    assert call_args[1]["json"] == {
        "title": "Bins",
        "body": "Tonight",
        "push": True,
        "recurrence": {"cron": "0 19 * * 2", "timezone": "Europe/Warsaw", "until": "2026-12-31T23:00:00+00:00"},
    }


async def test_create_notification_requires_aware_recurrence_until():
    client = _make_client(_make_session(_mock_response(201)))
    with pytest.raises(ValueError):
        await client.create_notification(
            "t", "b", recurrence={"cron": "@daily", "timezone": "UTC", "until": datetime(2026, 12, 31)}
        )


async def test_list_scheduled_notifications_follows_next_cursor():
    session = _make_session(
        _mock_response(200, json_body={"items": [{"id": 1}], "next_cursor": "abc"}),
        _mock_response(200, json_body={"items": [{"id": 2}]}),
    )
    client = _make_client(session)

    assert await client.list_scheduled_notifications("all") == [{"id": 1}, {"id": 2}]
    urls = [call[0][1] for call in session.request.call_args_list]
    assert urls[0].endswith("/notifications/scheduled?status=all&limit=100")
    assert urls[1].endswith("/notifications/scheduled?status=all&limit=100&cursor=abc")


# --- notification answers ---


async def test_get_notification_answer_long_polls_with_its_own_timeout():
    answer = {"notification_id": 7, "status": "answered", "action_id": "yes"}
    session = _make_session(_mock_response(200, json_body=answer))
    client = _make_client(session)

    assert await client.get_notification_answer(7, wait=20) == answer
    call_args = session.request.call_args
    assert call_args[0][0] == "GET"
    assert call_args[0][1].endswith("/notifications/answers/7?wait=20")
    # The hold plus a margin, not the 30s default a 25s hold would come close to.
    assert call_args[1]["timeout"].total > 20


async def test_get_notification_answer_maps_errors():
    client = _make_client(_make_session(_mock_response(404, text='{"code": "notification_answer.not_found"}')))
    with pytest.raises(PushWardNotFoundError):
        await client.get_notification_answer(7)

    client = _make_client(_make_session(_mock_response(429, headers={"Retry-After": "3"})))
    with pytest.raises(PushWardRateLimitedError) as exc:
        await client.get_notification_answer(7, wait=20)
    assert exc.value.retry_after == 3

    client = _make_client(_make_session(_mock_response(401)))
    with pytest.raises(PushWardAuthError):
        await client.get_notification_answer(7)


async def test_wait_for_notification_answer_polls_until_answered():
    session = _make_session(
        _mock_response(200, json_body={"notification_id": 7, "status": "pending"}),
        _mock_response(
            200,
            json_body={
                "notification_id": 7,
                "status": "answered",
                "action_id": "reply",
                "text": "on my way",
                "answered_at": "2026-09-28T12:00:00Z",
            },
        ),
    )
    client = _make_client(session)

    with patch("custom_components.pushward.api.asyncio.sleep", new=AsyncMock()):
        result = await client.wait_for_notification_answer(7, 300)

    assert result == {
        "answered": True,
        "notification_id": 7,
        "status": "answered",
        "action_id": "reply",
        "text": "on my way",
        "answered_at": "2026-09-28T12:00:00Z",
    }
    for call in session.request.call_args_list:
        assert call[0][1].endswith(f"/notifications/answers/7?wait={ANSWER_LONG_POLL_SECONDS}")


async def test_wait_for_notification_answer_zero_timeout_reads_once():
    session = _make_session(_mock_response(200, json_body={"notification_id": 7, "status": "pending"}))
    client = _make_client(session)

    result = await client.wait_for_notification_answer(7, 0)

    assert result["answered"] is False
    assert result["status"] == "pending"
    assert result["action_id"] is None
    assert result["reason"] == "no answer within 0s"
    assert session.request.call_count == 1
    assert session.request.call_args[0][1].endswith("/notifications/answers/7")


async def test_wait_for_notification_answer_rides_out_the_wait_cap():
    """A 429 (the server's per-user wait cap) is not a failure: back off, keep asking."""
    session = _make_session(
        _mock_response(429, text='{"code": "answer_wait.limit_exceeded"}'),
        _mock_response(200, json_body={"notification_id": 7, "status": "answered", "action_id": "yes"}),
    )
    client = _make_client(session)
    sleep = AsyncMock()

    with patch("custom_components.pushward.api.asyncio.sleep", new=sleep):
        result = await client.wait_for_notification_answer(7, 300)

    assert result["action_id"] == "yes"
    # The server refuses a held wait before it looks at the answer, so the next
    # read must not wait: an answer recorded meanwhile is seen at once.
    urls = [call[0][1] for call in session.request.call_args_list]
    assert urls[0].endswith(f"/notifications/answers/7?wait={ANSWER_LONG_POLL_SECONDS}")
    assert urls[1].endswith("/notifications/answers/7")
    sleep.assert_awaited_once_with(ANSWER_MIN_POLL_INTERVAL)


async def test_wait_for_notification_answer_reads_without_holding_when_waits_are_busy():
    session = _make_session(_mock_response(200, json_body={"notification_id": 7, "status": "answered"}))
    client = _make_client(session)
    for _ in range(ANSWER_MAX_CONCURRENT_WAITS):
        await client._answer_wait_semaphore.acquire()

    result = await client.wait_for_notification_answer(7, 300)

    assert result["answered"] is True
    assert session.request.call_args[0][1].endswith("/notifications/answers/7")


async def test_wait_for_notification_answer_gives_up_after_failure_budget():
    session = _make_session(*[_mock_response(503) for _ in range(ANSWER_FAILURE_BUDGET)])
    client = _make_client(session)

    with (
        patch("custom_components.pushward.api.asyncio.sleep", new=AsyncMock()),
        pytest.raises(PushWardApiError),
    ):
        await client.wait_for_notification_answer(7, 300)
    assert session.request.call_count == ANSWER_FAILURE_BUDGET


async def test_wait_for_notification_answer_fails_fast_on_404():
    session = _make_session(_mock_response(404))
    client = _make_client(session)

    with pytest.raises(PushWardNotFoundError):
        await client.wait_for_notification_answer(7, 300)
    assert session.request.call_count == 1


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
    """A rate-limit 429 (code rate_limit.exceeded) is retried; only quota.exceeded is not."""
    body = '{"status":429,"detail":"rate limit exceeded","code":"rate_limit.exceeded","retry_after_ms":2000}'
    resp_429 = _mock_response(429, text=body, headers={"Retry-After": "2"})
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
