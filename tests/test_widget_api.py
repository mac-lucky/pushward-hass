"""Tests for the widget endpoints on PushWardApiClient."""

import pytest

from custom_components.pushward.api import (
    PushWardForbiddenError,
    PushWardWidgetPermissionError,
)

from .conftest import make_api_client as _make_client
from .conftest import make_mock_response as _mock_response
from .conftest import make_mock_session as _make_session


async def test_create_widget_posts_body():
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.create_widget(
        slug="ha-users",
        name="Registered Users",
        template="value",
        content={"value": 42.0, "unit": "users"},
        push_throttle=60,
    )

    method, url = session.request.call_args[0]
    body = session.request.call_args[1]["json"]
    assert method == "POST"
    assert url.endswith("/widgets")
    assert body == {
        "slug": "ha-users",
        "name": "Registered Users",
        "content": {"value": 42.0, "unit": "users", "template": "value"},
        "push_throttle": 60,
    }


async def test_create_widget_omits_push_throttle_when_none():
    resp = _mock_response(201)
    session = _make_session(resp)
    client = _make_client(session)

    await client.create_widget(
        slug="ha-users",
        name="Users",
        template="value",
        content={"value": 1.0},
    )
    body = session.request.call_args[1]["json"]
    assert "push_throttle" not in body


async def test_patch_widget_sends_merge_body():
    resp = _mock_response(200)
    session = _make_session(resp)
    client = _make_client(session)

    await client.patch_widget("ha-users", {"content": {"value": 43.0}})

    method, url = session.request.call_args[0]
    assert method == "PATCH"
    assert url.endswith("/widgets/ha-users")
    assert session.request.call_args[1]["json"] == {"content": {"value": 43.0}}


async def test_delete_widget_treats_404_as_success():
    resp = _mock_response(404)
    session = _make_session(resp)
    client = _make_client(session)

    # Should not raise
    await client.delete_widget("ha-users")
    assert session.request.call_args[0][0] == "DELETE"


async def test_widget_403_maps_to_widget_permission_error():
    body = (
        '{"type":"about:blank","title":"Forbidden","status":403,'
        '"detail":"integration key does not have widget permission"}'
    )
    resp = _mock_response(403, text=body)
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardWidgetPermissionError):
        await client.create_widget(
            slug="ha-users",
            name="Users",
            template="value",
            content={"value": 1.0},
        )


async def test_activity_403_still_uses_forbidden_error():
    body = '{"type":"about:blank","title":"Forbidden","status":403,"detail":"subscription required"}'
    resp = _mock_response(403, text=body)
    session = _make_session(resp)
    client = _make_client(session)

    with pytest.raises(PushWardForbiddenError) as ex:
        await client.update_activity("slug", "ONGOING", {})
    # Make sure it's NOT mis-routed to the widget subclass.
    assert not isinstance(ex.value, PushWardWidgetPermissionError)
