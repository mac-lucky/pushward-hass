"""The media_player half of the media template: cover art and transport buttons.

Everything here knows what a `media_player` entity is, which is why it lives apart
from `content_mapper`: the mapper turns a State into content, and this module
supplies the two pieces of that content only the media_player domain can answer -
the artwork bytes and which transport buttons the player can actually drive.

The buttons are silent webhooks pointing back at Home Assistant. iOS POSTs the URL
carried in the activity, so the callback cannot ask for a Home Assistant token; the
per-subentry secret in the query string is the credential instead, and it only ever
travels to devices the activity was pushed to.

Two deliberate choices on the endpoint: an unknown subentry answers 404 before the
token check (there is no stored secret to compare against), and failed tokens do
not feed HA's login-ban middleware - the 43-char secret is the brute-force
defense, and a flood of guesses only ever produces 401s and one warning per hit.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from http import HTTPStatus
from urllib.parse import urlencode

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.media_player import DATA_COMPONENT, MediaPlayerEntityFeature
from homeassistant.const import ATTR_ENTITY_ID, ATTR_ENTITY_PICTURE, ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    CONF_ENTITY_ID,
    CONF_IMAGE_SHAPE,
    CONF_IMAGE_THUMBHASH,
    CONF_IMAGE_URL,
    CONF_MEDIA_CONTROLS,
    CONF_MEDIA_FAVORITE_SCRIPT,
    CONF_MEDIA_TOKEN,
    CONF_SUBENTRY_ID,
    CONF_TEMPLATE,
    DEFAULT_IMAGE_SHAPE,
    DEFAULT_MEDIA_CONTROLS,
    DOMAIN,
    IMAGE_SHAPES,
    MEDIA_CONTROL_SLOTS,
    SUBENTRY_TYPE_ENTITY,
    is_valid_image_url,
)
from .image_hash import ThumbhashError, async_thumbhash_for_bytes, cached_thumbhash

_LOGGER = logging.getLogger(__name__)

CONTROL_URL_PATH = "/api/pushward/media/{subentry_id}/{slot}"
CONTROL_TOKEN_PARAM = "token"
_TOKEN_BYTES = 32

# slot -> the media_player service the callback calls. play/pause are absent on
# purpose: iOS falls back to play_pause, and Home Assistant's own play_pause
# service needs both capabilities anyway (see _CONTROL_FEATURES).
CONTROL_SERVICES = {
    "previous": "media_previous_track",
    "play_pause": "media_play_pause",
    "next": "media_next_track",
    "stop": "media_stop",
    "volume_down": "volume_down",
    "volume_up": "volume_up",
}

# Every slot a mapped frame can carry. The wire `controls` object deep-merges
# server-side, so the mapper emits ALL of these on every frame - offered slots
# with their action, the rest as the null that deletes a previously pushed
# button. A slot merely omitted would keep an earlier frame's button alive on
# the card, dead, after this side stopped answering for it.
EMITTED_CONTROL_SLOTS = (*CONTROL_SERVICES, "favorite")

# slot -> the feature masks that let its service run, read the way Home Assistant
# reads required_features on those same services: any one mask whose bits are all
# present is enough. Pressing a button the player cannot drive would come back as
# "entity does not support this service", so the slot is left out instead.
_CONTROL_FEATURES: dict[str, tuple[int, ...]] = {
    "previous": (MediaPlayerEntityFeature.PREVIOUS_TRACK,),
    "play_pause": (MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE,),
    "next": (MediaPlayerEntityFeature.NEXT_TRACK,),
    "stop": (MediaPlayerEntityFeature.STOP,),
    "volume_down": (MediaPlayerEntityFeature.VOLUME_SET, MediaPlayerEntityFeature.VOLUME_STEP),
    "volume_up": (MediaPlayerEntityFeature.VOLUME_SET, MediaPlayerEntityFeature.VOLUME_STEP),
}

_NO_URL_LOGGED = "pushward_media_no_url_logged"


def new_control_token() -> str:
    """Mint the secret that authenticates one tracked player's callbacks."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _warn_missing_url(hass: HomeAssistant) -> None:
    """WARN the first time a control URL cannot be built, DEBUG after.

    Every push of every media player hits this, so the second one onwards would be
    noise about a setting the user has already been told to fix. The marker lives
    in hass.data rather than the module so a reloaded instance warns afresh.
    """
    message = "PushWard media controls need an https Home Assistant URL reachable from the phone; none is configured"
    if hass.data.get(_NO_URL_LOGGED):
        _LOGGER.debug(message)
        return
    hass.data[_NO_URL_LOGGED] = True
    _LOGGER.warning(message)


def _control_url(base: str, subentry_id: str, slot: str, token: str) -> str:
    path = CONTROL_URL_PATH.format(subentry_id=subentry_id, slot=slot)
    return f"{base}{path}?{urlencode({CONTROL_TOKEN_PARAM: token})}"


def _slot_offered(slot: str, features: int, entity_config: dict) -> bool:
    if slot == "favorite":
        return bool(entity_config.get(CONF_MEDIA_FAVORITE_SCRIPT))
    masks = _CONTROL_FEATURES.get(slot)
    return bool(masks) and any(features & mask == mask for mask in masks)


def control_urls(hass: HomeAssistant | None, state: State, entity_config: dict) -> dict[str, str]:
    """Return slot -> callback URL for the buttons this player can drive.

    Empty when controls are switched off, when the subentry carries no token (a
    config written before this existed), or when Home Assistant has no URL the
    phone could reach.
    """
    if hass is None or not entity_config.get(CONF_MEDIA_CONTROLS, DEFAULT_MEDIA_CONTROLS):
        return {}
    subentry_id = entity_config.get(CONF_SUBENTRY_ID) or ""
    token = entity_config.get(CONF_MEDIA_TOKEN) or ""
    if not subentry_id or not token:
        return {}
    try:
        # https only: the iOS extension that fires these webhooks ships no App
        # Transport Security exception, so a cleartext internal URL would render
        # buttons that fail silently on every phone. An https internal URL (VPN,
        # real certs) still qualifies.
        base = get_url(hass, prefer_external=True, require_ssl=True)
    except NoURLAvailableError:
        _warn_missing_url(hass)
        return {}

    features = _supported_features(state)
    return {
        slot: _control_url(base, subentry_id, slot, token)
        for slot in MEDIA_CONTROL_SLOTS
        if _slot_offered(slot, features, entity_config)
    }


def _supported_features(state: State) -> int:
    try:
        return int(state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0)
    except (TypeError, ValueError):
        return 0


def _media_entity(hass: HomeAssistant, entity_id: str):
    """The live MediaPlayerEntity object, or None when the domain isn't loaded."""
    component = hass.data.get(DATA_COMPONENT)
    if component is None:
        return None
    return component.get_entity(entity_id)


def _attach_shape(entity_config: dict, content: dict) -> None:
    shape = entity_config.get(CONF_IMAGE_SHAPE) or DEFAULT_IMAGE_SHAPE
    if shape in IMAGE_SHAPES:
        content[CONF_IMAGE_SHAPE] = shape


async def async_ensure_media_artwork(hass: HomeAssistant, state: State, entity_config: dict, content: dict) -> None:
    """Attach the playing track's cover art to a media frame, in place.

    ``entity_picture`` is usually a relative, token-signed proxy path: the phone
    cannot fetch it and the server would reject it as an image_url. So the bytes are
    read straight off the entity (no HTTP, no auth) and ride along as an inline
    ThumbHash instead. A player that publishes its artwork remotely does expose an
    absolute https picture, and that one doubles as a real image_url.

    An image_url configured by hand wins: the user asked for that picture.

    Best effort, like ``async_ensure_thumbhash``, and for the same reason it must run
    BEFORE any content-equality check - the stored frame carries the hash, so a
    hash-less comparison would report a change on every single update.
    """
    if content.get("template") != "media" or entity_config.get(CONF_IMAGE_URL):
        return
    picture = str(state.attributes.get(ATTR_ENTITY_PICTURE) or "")
    if not picture:
        return
    if is_valid_image_url(picture):
        content[CONF_IMAGE_URL] = picture
        # The shape rides with whichever image field makes it out - a URL whose
        # byte read then fails must not go out shapeless.
        _attach_shape(entity_config, content)

    # The cache is consulted BEFORE the entity is asked for bytes: integrations
    # commonly re-download their artwork on every async_get_media_image call, and
    # an unchanged track is re-pushed on every throttled update. Keyed on
    # entity_picture, which the proxy rewrites per track, so a new track re-hashes
    # and a paused one does not. The entity_id is what may be named in a message:
    # the picture path carries a signed token.
    try:
        thumbhash = cached_thumbhash(hass, picture, state.entity_id)
    except ThumbhashError as err:
        _LOGGER.debug("No cover-art ThumbHash for %s: %s", state.entity_id, err)
        return
    if thumbhash is None:
        entity = _media_entity(hass, state.entity_id)
        if entity is None:
            return
        try:
            data, _content_type = await entity.async_get_media_image()
        except Exception:
            # Whatever integration owns the player runs this, and it reaches
            # anything from a cloud API to a local socket. Artwork is decoration.
            _LOGGER.debug("Could not read cover art from %s", state.entity_id, exc_info=True)
            return
        if not data:
            return
        try:
            thumbhash = await async_thumbhash_for_bytes(hass, data, picture, state.entity_id)
        except ThumbhashError as err:
            _LOGGER.debug("No cover-art ThumbHash for %s: %s", state.entity_id, err)
            return
    content[CONF_IMAGE_THUMBHASH] = thumbhash
    _attach_shape(entity_config, content)


def _tracked_media_config(hass: HomeAssistant, subentry_id: str) -> dict | None:
    """The stored config of the tracked media player with this subentry id."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        # A disabled integration must not keep driving players from the Lock
        # Screen; its cards are already frozen anyway.
        if entry.disabled_by is not None:
            continue
        subentry = entry.subentries.get(subentry_id)
        if subentry is None or subentry.subentry_type != SUBENTRY_TYPE_ENTITY:
            continue
        config = dict(subentry.data)
        return config if config.get(CONF_TEMPLATE) == "media" else None
    return None


def _slot_target(config: dict, slot: str) -> tuple[str, str, str] | None:
    """(domain, service, entity_id) for a control slot, or None when it has none."""
    if not config.get(CONF_MEDIA_CONTROLS, DEFAULT_MEDIA_CONTROLS):
        return None
    if slot == "favorite":
        script = config.get(CONF_MEDIA_FAVORITE_SCRIPT)
        return ("script", "turn_on", script) if script else None
    service = CONTROL_SERVICES.get(slot)
    if service is None:
        return None
    entity_id = config.get(CONF_ENTITY_ID)
    return ("media_player", service, entity_id) if entity_id else None


class PushWardMediaControlView(HomeAssistantView):
    """Endpoint the transport buttons of a media Live Activity POST to.

    Unauthenticated on purpose: the request arrives from the phone carrying only
    what the activity payload held, so the per-subentry token is the credential.
    """

    url = CONTROL_URL_PATH
    name = "api:pushward:media"
    requires_auth = False

    async def post(self, request: web.Request, subentry_id: str, slot: str) -> web.Response:
        """Run the service behind one control slot."""
        hass = request.app[KEY_HASS]
        config = _tracked_media_config(hass, subentry_id)
        if config is None:
            return self.json_message("Unknown player", HTTPStatus.NOT_FOUND)

        expected = config.get(CONF_MEDIA_TOKEN) or ""
        token = request.query.get(CONTROL_TOKEN_PARAM, "")
        # Compared as bytes: the str form raises TypeError on non-ASCII input,
        # which would turn a probe into a 500 off an unauthenticated endpoint.
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            _LOGGER.warning(
                "Rejected a PushWard media control for %s: bad token", config.get(CONF_ENTITY_ID, subentry_id)
            )
            return self.json_message("Invalid token", HTTPStatus.UNAUTHORIZED)

        target = _slot_target(config, slot)
        if target is None:
            return self.json_message("Unknown control", HTTPStatus.NOT_FOUND)

        domain, service, entity_id = target
        # Not blocking: the phone is waiting on this response, and a player that
        # takes seconds to answer must not hold the request open that long.
        await hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=False)
        return self.json({"slot": slot, "entity_id": entity_id})


@callback
def async_register_media_control_view(hass: HomeAssistant) -> None:
    """Register the control endpoint. Called once, at component setup."""
    hass.http.register_view(PushWardMediaControlView())
