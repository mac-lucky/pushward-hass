"""Tests for the media transport buttons: which ones are offered, and the callback.

The endpoint is deliberately unauthenticated - iOS POSTs it carrying only what the
activity payload held - so the token checks here are the whole access control story
for someone's speakers.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.pushward.const import (
    CONF_ACTIVITY_NAME,
    CONF_ENTITY_ID,
    CONF_INTEGRATION_KEY,
    CONF_MEDIA_CONTROLS,
    CONF_MEDIA_FAVORITE_SCRIPT,
    CONF_MEDIA_TOKEN,
    CONF_SERVER_URL,
    CONF_SUBENTRY_ID,
    CONF_TEMPLATE,
    DEFAULT_SERVER_URL,
    DOMAIN,
    SUBENTRY_TYPE_ENTITY,
)
from custom_components.pushward.media_control import (
    async_register_media_control_view,
    control_urls,
    new_control_token,
)

from .conftest import make_entity_config, make_mock_state

MEDIA_ENTITY = "media_player.living_room"
TOKEN = "test-control-token"
EXTERNAL_URL = "https://ha.example.com"

_ALL_TRANSPORT = (
    MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.VOLUME_STEP
)


def _media_config(**overrides) -> dict:
    return make_entity_config(
        **{
            CONF_ENTITY_ID: MEDIA_ENTITY,
            CONF_ACTIVITY_NAME: "Living Room",
            CONF_TEMPLATE: "media",
            CONF_MEDIA_TOKEN: TOKEN,
            **overrides,
        }
    )


async def _setup_view(hass: HomeAssistant, hass_client_no_auth, config: dict | None = None):
    """Add a tracked player, register the endpoint, and return (client, subentry_id).

    The view has to be registered before the test client starts the app, or aiohttp
    never sees the route.
    """
    data = config if config is not None else _media_config()
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        version=2,
        unique_id=DOMAIN,
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: "hlk_test"},
        subentries_data=[
            ConfigSubentryData(
                data=data,
                subentry_type=SUBENTRY_TYPE_ENTITY,
                title=data[CONF_ACTIVITY_NAME],
                unique_id=data[CONF_ENTITY_ID],
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "http", {})
    async_register_media_control_view(hass)
    client = await hass_client_no_auth()
    return client, next(iter(entry.subentries))


def _url(subentry_id: str, slot: str, token: str = TOKEN) -> str:
    return f"/api/pushward/media/{subentry_id}/{slot}?token={token}"


@pytest.mark.parametrize(
    ("slot", "service"),
    [
        ("previous", "media_previous_track"),
        ("play_pause", "media_play_pause"),
        ("next", "media_next_track"),
        ("stop", "media_stop"),
        ("volume_up", "volume_up"),
        ("volume_down", "volume_down"),
    ],
)
async def test_a_control_calls_its_media_player_service(hass, hass_client_no_auth, slot, service) -> None:
    client, subentry_id = await _setup_view(hass, hass_client_no_auth)
    calls = async_mock_service(hass, "media_player", service)

    response = await client.post(_url(subentry_id, slot))
    await hass.async_block_till_done()

    assert response.status == 200
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == MEDIA_ENTITY


async def test_favorite_runs_the_configured_script(hass, hass_client_no_auth) -> None:
    config = _media_config(**{CONF_MEDIA_FAVORITE_SCRIPT: "script.star_track"})
    client, subentry_id = await _setup_view(hass, hass_client_no_auth, config)
    calls = async_mock_service(hass, "script", "turn_on")

    response = await client.post(_url(subentry_id, "favorite"))
    await hass.async_block_till_done()

    assert response.status == 200
    assert calls[0].data["entity_id"] == "script.star_track"


async def test_favorite_without_a_script_is_not_a_control(hass, hass_client_no_auth) -> None:
    client, subentry_id = await _setup_view(hass, hass_client_no_auth)

    response = await client.post(_url(subentry_id, "favorite"))

    assert response.status == 404


@pytest.mark.parametrize("token", ["", "wrong-token", TOKEN + "x", "tok\u00e9n"])
async def test_a_bad_token_is_rejected(hass, hass_client_no_auth, token) -> None:
    client, subentry_id = await _setup_view(hass, hass_client_no_auth)
    calls = async_mock_service(hass, "media_player", "media_next_track")

    response = await client.post(_url(subentry_id, "next", token))
    await hass.async_block_till_done()

    assert response.status == 401
    assert not calls


async def test_an_unknown_slot_is_not_found(hass, hass_client_no_auth) -> None:
    client, subentry_id = await _setup_view(hass, hass_client_no_auth)

    response = await client.post(_url(subentry_id, "shuffle"))

    assert response.status == 404


async def test_an_unknown_player_is_not_found(hass, hass_client_no_auth) -> None:
    client, _subentry_id = await _setup_view(hass, hass_client_no_auth)

    response = await client.post(_url("01JQZZZZZZZZZZZZZZZZZZZZZZ", "next"))

    assert response.status == 404


async def test_switching_the_controls_off_stops_the_endpoint_answering(hass, hass_client_no_auth) -> None:
    """A card pushed before the toggle flipped still carries the URLs."""
    config = _media_config(**{CONF_MEDIA_CONTROLS: False})
    client, subentry_id = await _setup_view(hass, hass_client_no_auth, config)
    calls = async_mock_service(hass, "media_player", "media_next_track")

    response = await client.post(_url(subentry_id, "next"))
    await hass.async_block_till_done()

    assert response.status == 404
    assert not calls


# --- which buttons get offered ---------------------------------------------


def _player_state(features: int = _ALL_TRANSPORT):
    return make_mock_state("playing", {"supported_features": features}, entity_id=MEDIA_ENTITY)


def _control_config(**overrides) -> dict:
    return _media_config(**{CONF_SUBENTRY_ID: "sub-media", **overrides})


async def test_control_urls_carry_the_token_and_the_subentry(hass: HomeAssistant) -> None:
    hass.config.external_url = EXTERNAL_URL

    urls = control_urls(hass, _player_state(), _control_config())

    assert urls["next"] == f"{EXTERNAL_URL}/api/pushward/media/sub-media/next?token={TOKEN}"


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        pytest.param(MediaPlayerEntityFeature.NEXT_TRACK, {"next"}, id="next_only"),
        pytest.param(
            MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE, {"play_pause"}, id="play_pause_needs_both"
        ),
        pytest.param(MediaPlayerEntityFeature.PAUSE, set(), id="pause_alone_is_not_play_pause"),
        pytest.param(MediaPlayerEntityFeature.VOLUME_SET, {"volume_down", "volume_up"}, id="volume_set"),
        pytest.param(MediaPlayerEntityFeature.VOLUME_STEP, {"volume_down", "volume_up"}, id="volume_step"),
        pytest.param(0, set(), id="a_player_that_drives_nothing"),
    ],
)
async def test_control_urls_follow_supported_features(hass: HomeAssistant, features, expected) -> None:
    hass.config.external_url = EXTERNAL_URL

    assert set(control_urls(hass, _player_state(features), _control_config())) == expected


async def test_control_urls_add_favorite_only_with_a_script(hass: HomeAssistant) -> None:
    hass.config.external_url = EXTERNAL_URL
    config = _control_config(**{CONF_MEDIA_FAVORITE_SCRIPT: "script.star_track"})

    assert "favorite" in control_urls(hass, _player_state(), config)
    assert "favorite" not in control_urls(hass, _player_state(), _control_config())


async def test_no_control_urls_when_switched_off(hass: HomeAssistant) -> None:
    hass.config.external_url = EXTERNAL_URL

    assert control_urls(hass, _player_state(), _control_config(**{CONF_MEDIA_CONTROLS: False})) == {}


async def test_no_control_urls_without_a_token(hass: HomeAssistant) -> None:
    """A subentry saved before the feature existed has no secret to authenticate with."""
    hass.config.external_url = EXTERNAL_URL

    assert control_urls(hass, _player_state(), _control_config(**{CONF_MEDIA_TOKEN: ""})) == {}


async def test_no_control_urls_without_a_reachable_home_assistant(hass: HomeAssistant) -> None:
    """The phone has to be able to reach the callback, so a URL-less setup gets no buttons."""
    with patch(
        "custom_components.pushward.media_control.get_url",
        side_effect=NoURLAvailableError,
    ):
        assert control_urls(hass, _player_state(), _control_config()) == {}


def test_control_tokens_are_unguessable_and_unique() -> None:
    tokens = {new_control_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(token) >= 32 for token in tokens)
