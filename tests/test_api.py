"""Tests for the PushWard API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.pushward.api import (
    PushWardApiClient,
    PushWardApiError,
    PushWardAuthError,
)


def _mock_response(status: int, *, text: str = "", headers: dict | None = None) -> AsyncMock:
    """Create a mock aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.ok = 200 <= status < 300
    resp.text = AsyncMock(return_value=text)
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(), history=(), status=status, message=text
        )
    return resp


def _make_session(*responses: AsyncMock) -> AsyncMock:
    """Create a mock aiohttp.ClientSession that returns responses in sequence."""
    session = AsyncMock(spec=aiohttp.ClientSession)
    ctx_managers = []
    for resp in responses:
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx_managers.append(cm)
    session.request = MagicMock(side_effect=ctx_managers)
    session.get = MagicMock()
    return session


def _make_client(session: AsyncMock) -> PushWardApiClient:
    return PushWardApiClient(session, "https://api.example.com", "test-key")


# --- validate_connection ---


async def test_validate_connection_success():
    resp = _mock_response(200)
    session = AsyncMock(spec=aiohttp.ClientSession)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=cm)

    client = _make_client(session)
    result = await client.validate_connection()

    assert result is True
    session.get.assert_called_once()
    call_kwargs = session.get.call_args
    assert "/auth/me" in call_kwargs[0][0]
    assert call_kwargs[1]["headers"]["Authorization"] == "Bearer test-key"


async def test_validate_connection_auth_error():
    resp = _mock_response(401)
    session = AsyncMock(spec=aiohttp.ClientSession)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=cm)

    client = _make_client(session)
    with pytest.raises(PushWardAuthError):
        await client.validate_connection()


# --- create_activity ---


async def test_create_activity_success():
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.create_activity("test-slug", "Test", priority=1, ended_ttl=300, stale_ttl=1800)

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


async def test_create_activity_already_exists():
    resp = _mock_response(409, text="activity already exists")
    session = _make_session(resp)
    client = _make_client(session)

    # Should not raise
    await client.create_activity("test-slug", "Test", priority=1, ended_ttl=300, stale_ttl=1800)


async def test_create_activity_limit():
    resp = _mock_response(409, text="activity limit reached")
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


async def test_create_activity_partial_ttls():
    """Only non-None TTLs are included in JSON body."""
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.create_activity("test-slug", "Test", priority=1, ended_ttl=600)

    body = session.request.call_args[1]["json"]
    assert body["ended_ttl"] == 600
    assert "stale_ttl" not in body


# --- update_activity ---


async def test_update_activity_success():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.update_activity("test-slug", "ONGOING", {"progress": 0.5})

    call_args = session.request.call_args
    assert call_args[0][0] == "PATCH"
    assert "/activity/test-slug" in call_args[0][1]
    assert call_args[1]["json"] == {"state": "ONGOING", "content": {"progress": 0.5}}


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
        category="SECURITY",
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
    assert body["category"] == "SECURITY"
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


# --- retry ---


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_retry_on_server_error(mock_sleep):
    resp_500 = _mock_response(500, text="Internal Server Error")
    resp_200 = _mock_response(200)
    session = _make_session(resp_500, resp_200)
    client = _make_client(session)

    await client.update_activity("test-slug", "ONGOING", {"progress": 0.5})

    assert session.request.call_count == 2
    mock_sleep.assert_called_once()


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_retry_on_429_with_retry_after(mock_sleep):
    resp_429 = _mock_response(429, headers={"Retry-After": "2"})
    resp_200 = _mock_response(200)
    session = _make_session(resp_429, resp_200)
    client = _make_client(session)

    await client.update_activity("test-slug", "ONGOING", {"progress": 0.5})

    assert session.request.call_count == 2
    # Should sleep for the Retry-After value (2 seconds)
    mock_sleep.assert_any_call(2.0)


@patch("custom_components.pushward.api.asyncio.sleep", new_callable=AsyncMock)
async def test_no_retry_on_client_error(mock_sleep):
    resp_400 = _mock_response(400, text="Bad Request")
    session = _make_session(resp_400)
    client = _make_client(session)

    with pytest.raises(PushWardApiError, match="400"):
        await client.update_activity("test-slug", "ONGOING", {"progress": 0.5})

    assert session.request.call_count == 1
    mock_sleep.assert_not_called()
