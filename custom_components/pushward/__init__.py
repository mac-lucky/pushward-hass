"""PushWard integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from functools import partial
from urllib.parse import urlparse

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .activity_manager import ActivityManager, build_history_store
from .api import (
    PushWardApiClient,
    PushWardApiError,
    PushWardForbiddenError,
)
from .const import (
    ACTIVITY_STATE_ENDED,
    ACTIVITY_STATES,
    ACTIVITY_TTL_MAX,
    ACTIVITY_TTL_MIN,
    BOARD_MAX_TILES,
    BOARD_TILE_ICON_MAX,
    BOARD_TILE_LABEL_MAX,
    BOARD_TILE_UNIT_MAX,
    BOARD_TILE_VALUE_MAX,
    BOARD_TRENDS,
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    CONF_SLUG,
    DEFAULT_PRIORITY,
    DISMISSAL_TTL_MAX,
    DISMISSAL_TTL_MIN,
    DOMAIN,
    LOG_LEVELS,
    LOG_LINE_TEXT_MAX,
    LOG_MAX_LINES,
    MAX_SEVERITY_LABEL_LEN,
    MAX_TAP_ACTION_BODY_LEN,
    MAX_TAP_ACTION_ICON_LEN,
    MAX_TAP_ACTION_TITLE_LEN,
    MAX_TEXT_INPUT_LABEL_LEN,
    NOTIFICATION_LEVELS,
    PRIORITY_MAX,
    PRIORITY_MIN,
    SCALES,
    SEVERITIES,
    SOUNDS,
    SUBENTRY_TYPE_ENTITY,
    SUBENTRY_TYPE_WIDGET,
    TAP_ACTION_METHODS,
    TEMPLATES,
    USAGE_LIMIT_RESOURCES,
    usage_limit_issue_id,
    validate_action_headers,
    validate_duration,
    validate_slug,
    validate_tap_action_url,
    validate_url,
)
from .coordinator import PushWardUsageCoordinator
from .widget_manager import WidgetManager, build_widget_store

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


SERVICE_UPDATE_ACTIVITY = "update_activity"
# Per-template update services (one per const.TEMPLATES) — the template is implied by the
# service name, so the UI shows only that template's fields (HA can't hide fields by value).
SERVICE_UPDATE_TEMPLATE_PREFIX = "update_activity_"
SERVICE_CREATE_ACTIVITY = "create_activity"
SERVICE_END_ACTIVITY = "end_activity"
SERVICE_DELETE_ACTIVITY = "delete_activity"
SERVICE_SEND_NOTIFICATION = "send_notification"
SERVICE_SEND_EMAIL = "send_email"
SERVICE_WIDGET_REFRESH = "widget_refresh"
SERVICE_DELETE_WIDGET = "delete_widget"

# Keys that turn an action into a silent HTTP webhook — gated by _validate_http_action_fields.
_HTTP_ACTION_KEYS = ("method", "headers", "body")

# Shared HTTP-routing fields for tap actions / action buttons. method/headers/body only
# apply to http(s) URLs — the server (and _validate_http_action_fields below) reject them on
# a custom-scheme URL. Spread into both the activity tap-action schemas and the notification
# action schema.
_HTTP_ACTION_FIELDS = {
    vol.Optional("method"): vol.All(str, vol.Upper, vol.In(TAP_ACTION_METHODS)),
    vol.Optional("headers"): vol.All(vol.Schema({str: str}), validate_action_headers),
    vol.Optional("body"): vol.All(str, vol.Length(max=MAX_TAP_ACTION_BODY_LEN)),
}


def _action_url_is_http(data: dict) -> bool:
    """True when the action's url carries an http(s) scheme (the silent-webhook shape)."""
    return urlparse(str(data.get("url", ""))).scheme.lower() in ("http", "https")


def _validate_http_action_fields(data: dict) -> dict:
    """Reject method/headers/body on a non-http(s) action URL.

    Mirrors pushward-server ValidateAction (hasHTTPShape && !isHTTP): only a *non-empty*
    method/headers/body needs an http(s) url, so an empty body/headers on a custom-scheme
    tap target is fine. The caller gets a clear HA error instead of a server 400; a
    missing/empty url has no scheme and fails here too.
    """
    if any(data.get(key) for key in _HTTP_ACTION_KEYS) and not _action_url_is_http(data):
        raise vol.Invalid("method, headers, and body require an http or https url")
    return data


def _validate_text_input_fields(data: dict) -> dict:
    """Reject a reply-with-text action that the server would 400.

    Mirrors pushward-server ValidateTextInput: the placeholder / button label
    require text_input, and text_input itself needs a silent (non-foreground)
    http(s) action, the only shape the iOS client renders a reply field for.
    Surfacing it here gives a clear HA error instead of a server 400.
    """
    if not data.get("text_input"):
        if data.get("text_input_placeholder") or data.get("text_input_button_title"):
            raise vol.Invalid("text_input_placeholder and text_input_button_title require text_input")
        return data
    if not _action_url_is_http(data):
        raise vol.Invalid("text_input requires an http or https url")
    if data.get("foreground"):
        raise vol.Invalid("text_input is only valid on silent (non-foreground) actions")
    return data


# Base tap target / silent webhook (title/icon not rendered for this slot). _URL_ACTION_SCHEMA
# extends this with the button-facing label/icon, so the shared slot is defined once.
_TAP_ACTION_BASE = vol.Schema(
    {
        vol.Required("url"): validate_tap_action_url,
        vol.Optional("foreground"): cv.boolean,
        **_HTTP_ACTION_FIELDS,
    }
)
_TAP_ACTION_SCHEMA = vol.All(_TAP_ACTION_BASE, _validate_http_action_fields)

# Tappable button (primary / secondary): tap-action routing plus a button label + SF Symbol.
_URL_ACTION_SCHEMA = vol.All(
    _TAP_ACTION_BASE.extend(
        {
            vol.Optional("title"): vol.All(str, vol.Length(max=MAX_TAP_ACTION_TITLE_LEN)),
            vol.Optional("icon"): vol.All(str, vol.Length(max=MAX_TAP_ACTION_ICON_LEN)),
        }
    ),
    _validate_http_action_fields,
)

# Action fields the server accepts on EVERY activity template (content_schema.go
# tapActionProperties). Legacy url/secondary_url strings sit alongside the richer
# *_action objects; the server allows custom-scheme URLs here, hence validate_tap_action_url.
_UNIVERSAL_ACTION_FIELDS = {
    vol.Optional("url"): validate_tap_action_url,
    vol.Optional("secondary_url"): validate_tap_action_url,
    vol.Optional("tap_action"): _TAP_ACTION_SCHEMA,
    vol.Optional("url_action"): _URL_ACTION_SCHEMA,
    vol.Optional("secondary_url_action"): _URL_ACTION_SCHEMA,
}

# Composable field-groups for the update services. The shared groups (top-level +
# universal labels/appearance/actions) merge with one template-specific group per service, so
# each per-template schema accepts that template's fields plus the universal ones; the
# deprecated update_activity schema is rebuilt below as the union of all of them.
_UPDATE_TOPLEVEL_FIELDS = {
    vol.Required("slug"): validate_slug,
    vol.Required("state"): vol.In(ACTIVITY_STATES),
    # Patchable persistence windows, top-level on the PATCH body, not content.
    vol.Optional("ended_ttl"): vol.All(vol.Coerce(int), vol.Range(min=ACTIVITY_TTL_MIN, max=ACTIVITY_TTL_MAX)),
    vol.Optional("stale_ttl"): vol.All(vol.Coerce(int), vol.Range(min=ACTIVITY_TTL_MIN, max=ACTIVITY_TTL_MAX)),
    vol.Optional("dismissal_ttl"): vol.All(vol.Coerce(int), vol.Range(min=DISMISSAL_TTL_MIN, max=DISMISSAL_TTL_MAX)),
}
_UNIVERSAL_LABEL_FIELDS = {
    vol.Optional("state_text"): str,
    vol.Optional("subtitle"): str,
    vol.Optional("icon"): str,
    vol.Optional("progress"): vol.Coerce(float),
}
_UNIVERSAL_APPEARANCE_FIELDS = {
    vol.Optional("completion_message"): str,
    vol.Optional("accent_color"): str,
    vol.Optional("background_color"): str,
    vol.Optional("text_color"): str,
    vol.Optional("remaining_time"): vol.Coerce(int),
    vol.Optional("sound"): vol.In(SOUNDS),
    vol.Optional("priority"): vol.All(vol.Coerce(int), vol.Range(min=0, max=10)),
}
# Animation window for the live-progress templates (generic, steps). The service
# path is a stateless pass-through with no anchor carry-forward, so the caller
# owns the window; steps needs both dates when live_progress is true.
_LIVE_PROGRESS_FIELDS = {
    vol.Optional("live_progress"): cv.boolean,
    vol.Optional("start_date"): vol.Coerce(int),
    vol.Optional("end_date"): vol.Coerce(int),
}
_COUNTDOWN_TEMPLATE_FIELDS = {
    vol.Optional("end_date"): vol.Coerce(int),
    # Set-and-forget alternative to end_date: int seconds (>=1) or a Go-style duration
    # string ("60s", "1h30m"). The server re-anchors start_date=now / end_date=now+duration
    # whenever duration is present, so duration takes precedence if end_date is also sent.
    vol.Optional("duration"): validate_duration,
    vol.Optional("start_date"): vol.Coerce(int),
    vol.Optional("warning_threshold"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional("alarm"): cv.boolean,
    vol.Optional("snooze_seconds"): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
}
_STEPS_TEMPLATE_FIELDS = {
    vol.Optional("total_steps"): vol.Coerce(int),
    vol.Optional("current_step"): vol.Coerce(int),
    vol.Optional("step_labels"): list,
    vol.Optional("step_rows"): list,
    vol.Optional("step_weights"): list,
    vol.Optional("step_colors"): list,
    # Re-anchors the live-progress window server-side (start_date=now / end_date=now+duration),
    # same validator as the countdown field: int seconds (>=1) or a Go-style duration string.
    vol.Optional("duration"): validate_duration,
}
_ALERT_TEMPLATE_FIELDS = {
    vol.Optional("severity"): vol.In(SEVERITIES),
    # Optional override for the Info/Warning/Critical badge text.
    vol.Optional("severity_label"): vol.All(str, vol.Length(max=MAX_SEVERITY_LABEL_LEN)),
    vol.Optional("fired_at"): vol.Coerce(int),
}
_GAUGE_TEMPLATE_FIELDS = {
    # A gauge value is a single number (timeline's `value` is the dict form).
    vol.Optional("value"): vol.Coerce(float),
    vol.Optional("min_value"): vol.Coerce(float),
    vol.Optional("max_value"): vol.Coerce(float),
    vol.Optional("unit"): str,
}
_TIMELINE_TEMPLATE_FIELDS = {
    vol.Optional("value"): vol.Any(vol.Coerce(float), dict),
    vol.Optional("unit"): str,
    vol.Optional("units"): dict,
    # Override the headline series (else the mapper auto-picks it). Server caps it at 32.
    vol.Optional("primary_series"): vol.All(str, vol.Length(max=32)),
    vol.Optional("scale"): vol.In(SCALES),
    vol.Optional("decimals"): vol.All(vol.Coerce(int), vol.Range(min=0, max=10)),
    vol.Optional("smoothing"): bool,
    vol.Optional("thresholds"): list,
    # Initial history seed only (series-name -> [{timestamp, value}]). The server takes over
    # as source of truth after the first update — see PrepareTimelineUpdate.
    vol.Optional("history"): dict,
}
# board: 1-BOARD_MAX_TILES tiles (RFC-7396 atomic replace). value is a STRING so
# "Open"/"On"/numbers all render. url_action is per-tile (reuses the rich tap-action schema).
_BOARD_TILE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("label"): vol.All(vol.Coerce(str), vol.Length(min=1, max=BOARD_TILE_LABEL_MAX)),
            vol.Required("value"): vol.All(vol.Coerce(str), vol.Length(min=1, max=BOARD_TILE_VALUE_MAX)),
            vol.Optional("unit"): vol.All(vol.Coerce(str), vol.Length(max=BOARD_TILE_UNIT_MAX)),
            vol.Optional("icon"): vol.All(str, vol.Length(max=BOARD_TILE_ICON_MAX)),
            vol.Optional("color"): str,  # named/hex; server ValidateColor is authoritative
            vol.Optional("trend"): vol.In(BOARD_TRENDS),
            vol.Optional("url_action"): _URL_ACTION_SCHEMA,
        }
    ),
)
_BOARD_TEMPLATE_FIELDS = {
    vol.Optional("tiles"): vol.All([_BOARD_TILE_SCHEMA], vol.Length(min=1, max=BOARD_MAX_TILES)),
}
# log: 1-LOG_MAX_LINES lines (newest-first, atomic replace). level is info/warn/error
# (a different set from alert's severity); log_backlog is server-owned and never sent.
_LOG_LINE_SCHEMA = vol.Schema(
    {
        vol.Required("text"): vol.All(vol.Coerce(str), vol.Length(min=1, max=LOG_LINE_TEXT_MAX)),
        # `at` is a positive unix timestamp server-side; reject 0/negative locally.
        vol.Optional("at"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional("level"): vol.In(LOG_LEVELS),
    }
)
_LOG_TEMPLATE_FIELDS = {
    vol.Optional("lines"): vol.All([_LOG_LINE_SCHEMA], vol.Length(min=1, max=LOG_MAX_LINES)),
}

# Lean field groups for board/log: only the fields those templates actually render.
# Board/log have no progress bar, no remaining_time, and no whole-activity button slots
# (board uses per-tile url_action; log has no buttons) — so those are deliberately absent.
_BOARD_LOG_LABEL_FIELDS = {
    vol.Optional("state_text"): str,
    vol.Optional("subtitle"): str,
    vol.Optional("icon"): str,
}
_BOARD_LOG_APPEARANCE_FIELDS = {
    vol.Optional("completion_message"): str,
    vol.Optional("accent_color"): str,
    vol.Optional("background_color"): str,
    vol.Optional("text_color"): str,
    vol.Optional("sound"): vol.In(SOUNDS),
    vol.Optional("priority"): vol.All(vol.Coerce(int), vol.Range(min=0, max=10)),
}
_BOARD_LOG_ACTION_FIELDS = {vol.Optional("tap_action"): _TAP_ACTION_SCHEMA}  # whole-activity tap only


def _board_log_schema(template_fields: dict) -> vol.Schema:
    """Build a lean board/log update schema: only the fields those templates render."""
    return vol.Schema(
        {
            **_UPDATE_TOPLEVEL_FIELDS,
            **_BOARD_LOG_LABEL_FIELDS,
            **_BOARD_LOG_APPEARANCE_FIELDS,
            **_BOARD_LOG_ACTION_FIELDS,
            **template_fields,
        }
    )


def _update_template_schema(*template_fields: dict) -> vol.Schema:
    """Build an update_activity schema from the shared groups plus the given template groups.

    Groups may repeat a key (live-progress and countdown both carry start_date/
    end_date): voluptuous markers compare by key name, so the merge keeps the
    first group's marker and the last group's validator. Keep overlapping keys'
    validators identical or the union schema silently takes the last one.
    """
    merged = {
        **_UPDATE_TOPLEVEL_FIELDS,
        **_UNIVERSAL_LABEL_FIELDS,
        **_UNIVERSAL_APPEARANCE_FIELDS,
        **_UNIVERSAL_ACTION_FIELDS,
    }
    for group in template_fields:
        merged.update(group)
    return vol.Schema(merged)


_UPDATE_TEMPLATE_SCHEMAS = {
    "generic": _update_template_schema(_LIVE_PROGRESS_FIELDS),
    "countdown": _update_template_schema(_COUNTDOWN_TEMPLATE_FIELDS),
    "steps": _update_template_schema(_LIVE_PROGRESS_FIELDS, _STEPS_TEMPLATE_FIELDS),
    "alert": _update_template_schema(_ALERT_TEMPLATE_FIELDS),
    "gauge": _update_template_schema(_GAUGE_TEMPLATE_FIELDS),
    "timeline": _update_template_schema(_TIMELINE_TEMPLATE_FIELDS),
    # board/log use the lean schema (no progress / remaining_time / button slots).
    "board": _board_log_schema(_BOARD_TEMPLATE_FIELDS),
    "log": _board_log_schema(_LOG_TEMPLATE_FIELDS),
}

# The deprecated update_activity accepts every template's fields (plus an explicit
# `template` selector) — i.e. the union of all per-template schemas.
SCHEMA_UPDATE_ACTIVITY = _update_template_schema(
    {vol.Optional("template"): str},
    _LIVE_PROGRESS_FIELDS,
    _COUNTDOWN_TEMPLATE_FIELDS,
    _STEPS_TEMPLATE_FIELDS,
    _ALERT_TEMPLATE_FIELDS,
    _GAUGE_TEMPLATE_FIELDS,
    _TIMELINE_TEMPLATE_FIELDS,
    _BOARD_TEMPLATE_FIELDS,
    _LOG_TEMPLATE_FIELDS,
)

SCHEMA_CREATE_ACTIVITY = vol.Schema(
    {
        vol.Required("slug"): validate_slug,
        vol.Required("name"): str,
        # services.yaml's number selectors are only a UI hint; automations, scripts and
        # REST callers reach the schema directly, so enforce the server's bounds here.
        vol.Optional("priority", default=DEFAULT_PRIORITY): vol.All(
            vol.Coerce(int), vol.Range(min=PRIORITY_MIN, max=PRIORITY_MAX)
        ),
        vol.Optional("ended_ttl"): vol.All(vol.Coerce(int), vol.Range(min=ACTIVITY_TTL_MIN, max=ACTIVITY_TTL_MAX)),
        vol.Optional("stale_ttl"): vol.All(vol.Coerce(int), vol.Range(min=ACTIVITY_TTL_MIN, max=ACTIVITY_TTL_MAX)),
        vol.Optional("dismissal_ttl"): vol.All(
            vol.Coerce(int), vol.Range(min=DISMISSAL_TTL_MIN, max=DISMISSAL_TTL_MAX)
        ),
    }
)

SCHEMA_END_ACTIVITY = vol.Schema(
    {
        vol.Required("slug"): validate_slug,
        vol.Optional("completion_message"): str,
    }
)

SCHEMA_DELETE_ACTIVITY = vol.Schema(
    {
        vol.Required("slug"): validate_slug,
    }
)

_MEDIA_TYPES = ("image", "video", "audio")

SCHEMA_MEDIA = vol.Schema(
    {
        vol.Required("url"): validate_url,
        vol.Required("type"): vol.In(_MEDIA_TYPES),
    }
)

SCHEMA_ACTION = vol.All(
    vol.Schema(
        {
            vol.Required("id"): vol.All(str, vol.Length(min=1)),
            vol.Required("title"): vol.All(str, vol.Length(min=1)),
            # The server accepts any non-blocked scheme on a notification action (deep links
            # like homeassistant:// / tel: / mailto:), matching the activity tap-action fields.
            vol.Optional("url"): validate_tap_action_url,
            vol.Optional("foreground"): cv.boolean,
            vol.Optional("destructive"): cv.boolean,
            vol.Optional("authentication_required"): cv.boolean,
            vol.Optional("icon"): str,
            vol.Optional("text_input"): cv.boolean,
            vol.Optional("text_input_placeholder"): vol.All(str, vol.Length(max=MAX_TEXT_INPUT_LABEL_LEN)),
            vol.Optional("text_input_button_title"): vol.All(str, vol.Length(max=MAX_TEXT_INPUT_LABEL_LEN)),
            **_HTTP_ACTION_FIELDS,
        }
    ),
    # method/headers/body turn the button into a webhook — they need an http(s) url.
    _validate_http_action_fields,
    # text_input (reply-with-text) needs that same silent http(s) shape.
    _validate_text_input_fields,
)

SCHEMA_SEND_NOTIFICATION = vol.Schema(
    {
        vol.Required("title"): str,
        vol.Required("body"): str,
        vol.Optional("subtitle"): str,
        vol.Optional("level"): vol.In(NOTIFICATION_LEVELS),
        vol.Optional("volume"): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
        vol.Optional("thread_id"): str,
        vol.Optional("collapse_id"): vol.All(str, vol.Length(max=64)),
        vol.Optional("source"): str,
        vol.Optional("source_display_name"): str,
        vol.Optional("activity_slug"): validate_slug,
        vol.Optional("url"): validate_url,
        vol.Optional("media"): SCHEMA_MEDIA,
        vol.Optional("icon_url"): validate_url,
        vol.Optional("metadata"): vol.Schema({str: str}),
        vol.Optional("actions"): vol.All([SCHEMA_ACTION], vol.Length(max=10)),
        vol.Optional("push", default=True): bool,
    }
)


def _no_line_breaks(value: str) -> str:
    """Reject CR/LF — guards against email header injection via to/subject."""
    if "\r" in value or "\n" in value:
        raise vol.Invalid("must not contain line breaks")
    return value


def _require_email_body(data: dict) -> dict:
    """Require a non-empty plain-text or HTML body — empty strings don't count.

    `cv.has_at_least_one_key` only checks key presence, so `body: ""` would slip
    through and ship an empty payload the server rejects.
    """
    if not (data.get("body") or data.get("html_body")):
        raise vol.Invalid("must provide a non-empty 'body', 'html_body', or both")
    return data


# `body` maps to the API `text_body`; `html_body` passes through.
SCHEMA_SEND_EMAIL = vol.All(
    vol.Schema(
        {
            vol.Required("to"): vol.All(str, vol.Email(), vol.Length(max=254)),
            vol.Required("subject"): vol.All(str, _no_line_breaks, vol.Length(min=1, max=256)),
            vol.Optional("body"): str,
            vol.Optional("html_body"): str,
        }
    ),
    _require_email_body,
)

# Target a single widget by slug XOR entity_id (an entity_id resolves to its tracked widget's
# slug). Shared by widget_refresh and delete_widget so the targeting contract is defined once.
SCHEMA_WIDGET_TARGET = vol.All(
    vol.Schema(
        {
            vol.Exclusive("slug", "widget_target"): validate_slug,
            vol.Exclusive("entity_id", "widget_target"): cv.entity_id,
        }
    ),
    cv.has_at_least_one_key("slug", "entity_id"),
)
SCHEMA_WIDGET_REFRESH = SCHEMA_WIDGET_TARGET
SCHEMA_DELETE_WIDGET = SCHEMA_WIDGET_TARGET


def _get_api(hass: HomeAssistant) -> PushWardApiClient:
    """Get the API client from the first available config entry."""
    entries = hass.data.get(DOMAIN)
    if not entries:
        raise HomeAssistantError("PushWard is not configured. Add the integration via Settings → Devices & Services.")
    return next(iter(entries.values()))["api"]


@contextmanager
def _surface_api_errors():
    """Translate PushWard API errors into user-facing HA errors.

    Without this, an exception from the API bubbles up to the service layer as a
    generic "Unknown error" with no hint of the cause. A 403 (missing capability,
    unverified recipient, …) is user-fixable, so it becomes a ServiceValidationError;
    everything else (4xx/5xx/connection) becomes a HomeAssistantError carrying the
    server's message.
    """
    try:
        yield
    except PushWardForbiddenError as err:
        raise ServiceValidationError(str(err)) from err
    except PushWardApiError as err:
        raise HomeAssistantError(str(err)) from err


async def _send_activity_update(hass: HomeAssistant, call: ServiceCall, *, template: str | None = None) -> None:
    """PATCH an activity from a service call.

    sound/priority and the ended/stale/dismissal TTLs are top-level PATCH kwargs; every
    remaining field is content. state_text is the user-facing name for the content "state"
    string. The per-template actions inject their template (their schema omits it); the
    deprecated alias lets the caller pass it.
    """
    api = _get_api(hass)
    content = dict(call.data)
    slug = content.pop("slug")
    state = content.pop("state")
    sound = content.pop("sound", None) or None
    priority = content.pop("priority", None)
    ended_ttl = content.pop("ended_ttl", None)
    stale_ttl = content.pop("stale_ttl", None)
    dismissal_ttl = content.pop("dismissal_ttl", None)
    if "state_text" in content:
        content["state"] = content.pop("state_text")
    if template is not None:
        content["template"] = template
    with _surface_api_errors():
        await api.update_activity(
            slug,
            state,
            content,
            sound=sound,
            priority=priority,
            ended_ttl=ended_ttl,
            stale_ttl=stale_ttl,
            dismissal_ttl=dismissal_ttl,
        )


async def _async_handle_update_activity(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the deprecated update_activity service call.

    Superseded by the per-template ``update_activity_<template>`` services. Kept as a
    backward-compatible alias; raises an idempotent Repair issue (cleared on restart once
    automations migrate) instead of logging a warning on every call.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        "deprecated_update_activity",
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="deprecated_update_activity",
    )
    await _send_activity_update(hass, call)


async def _async_handle_update_template(hass: HomeAssistant, call: ServiceCall, *, template: str) -> None:
    """Handle an update_activity_<template> service call (template implied by the name)."""
    await _send_activity_update(hass, call, template=template)


async def _async_handle_create_activity(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the create_activity service call."""
    api = _get_api(hass)
    with _surface_api_errors():
        await api.create_activity(
            slug=call.data["slug"],
            name=call.data["name"],
            priority=call.data["priority"],
            ended_ttl=call.data.get("ended_ttl"),
            stale_ttl=call.data.get("stale_ttl"),
            dismissal_ttl=call.data.get("dismissal_ttl"),
        )


async def _async_handle_end_activity(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the end_activity service call."""
    api = _get_api(hass)
    slug = call.data["slug"]
    content = {}
    if "completion_message" in call.data:
        content["completion_message"] = call.data["completion_message"]
    with _surface_api_errors():
        await api.update_activity(slug, ACTIVITY_STATE_ENDED, content)


async def _async_handle_delete_activity(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the delete_activity service call."""
    api = _get_api(hass)
    with _surface_api_errors():
        await api.delete_activity(call.data["slug"])


_NOTIFICATION_FIELDS = [
    "subtitle",
    "level",
    "volume",
    "thread_id",
    "collapse_id",
    "source",
    "source_display_name",
    "activity_slug",
    "url",
    "media",
    "icon_url",
    "metadata",
    "actions",
]


async def _async_handle_send_notification(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the send_notification service call."""
    api = _get_api(hass)
    kwargs: dict = {}
    for field in _NOTIFICATION_FIELDS:
        if field in call.data:
            kwargs[field] = call.data[field]
    with _surface_api_errors():
        await api.create_notification(
            title=call.data["title"],
            body=call.data["body"],
            push=call.data["push"],
            **kwargs,
        )


async def _async_handle_send_email(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the send_email service call."""
    api = _get_api(hass)
    with _surface_api_errors():
        await api.send_email(
            to=call.data["to"],
            subject=call.data["subject"],
            text_body=call.data.get("body") or None,
            html_body=call.data.get("html_body") or None,
        )


async def _async_handle_widget_refresh(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the widget_refresh service call.

    Routes the refresh to every config entry's WidgetManager; the manager
    that owns the slug / entity_id wins, others raise ValueError (swallowed).
    """
    slug = call.data.get("slug")
    entity_id = call.data.get("entity_id")
    domain_data = hass.data.get(DOMAIN) or {}
    if not domain_data:
        raise HomeAssistantError("PushWard is not configured.")

    found = False
    for entry_data in domain_data.values():
        manager: WidgetManager | None = entry_data.get("widget_manager")
        if manager is None:
            continue
        try:
            await manager.async_refresh(slug=slug, entity_id=entity_id)
            found = True
        except ValueError:
            continue
    if not found:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="widget_not_found",
            translation_placeholders={"slug": slug or "", "entity_id": entity_id or ""},
        )


def _find_widget_slug(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """Resolve an entity_id to the slug of the tracked widget bound to it."""
    for entry_data in (hass.data.get(DOMAIN) or {}).values():
        manager: WidgetManager | None = entry_data.get("widget_manager")
        if manager is None:
            continue
        slug = manager.slug_for_entity(entity_id)
        if slug:
            return slug
    return None


async def _async_handle_delete_widget(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the delete_widget service call.

    Deletes the server-side widget (DELETE /widgets/{slug}). If a tracked_widget subentry
    still drives this slug it will be recreated on the next restart/sync — remove the subentry
    to delete it permanently (subentry removal also deletes the server widget automatically).
    """
    api = _get_api(hass)
    slug = call.data.get("slug")
    entity_id = call.data.get("entity_id")
    if slug is None:
        slug = _find_widget_slug(hass, entity_id)
        if slug is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="widget_not_found",
                translation_placeholders={"slug": slug or "", "entity_id": entity_id or ""},
            )
    with _surface_api_errors():
        await api.delete_widget(slug)


def _register_services(hass: HomeAssistant) -> None:
    """Register PushWard services (only once)."""
    if hass.services.has_service(DOMAIN, SERVICE_UPDATE_ACTIVITY):
        return

    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_ACTIVITY, partial(_async_handle_update_activity, hass), SCHEMA_UPDATE_ACTIVITY
    )
    for template in TEMPLATES:
        hass.services.async_register(
            DOMAIN,
            f"{SERVICE_UPDATE_TEMPLATE_PREFIX}{template}",
            partial(_async_handle_update_template, hass, template=template),
            _UPDATE_TEMPLATE_SCHEMAS[template],
        )
    hass.services.async_register(
        DOMAIN, SERVICE_CREATE_ACTIVITY, partial(_async_handle_create_activity, hass), SCHEMA_CREATE_ACTIVITY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_END_ACTIVITY, partial(_async_handle_end_activity, hass), SCHEMA_END_ACTIVITY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_ACTIVITY, partial(_async_handle_delete_activity, hass), SCHEMA_DELETE_ACTIVITY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_NOTIFICATION, partial(_async_handle_send_notification, hass), SCHEMA_SEND_NOTIFICATION
    )
    hass.services.async_register(DOMAIN, SERVICE_SEND_EMAIL, partial(_async_handle_send_email, hass), SCHEMA_SEND_EMAIL)
    hass.services.async_register(
        DOMAIN, SERVICE_WIDGET_REFRESH, partial(_async_handle_widget_refresh, hass), SCHEMA_WIDGET_REFRESH
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_WIDGET, partial(_async_handle_delete_widget, hass), SCHEMA_DELETE_WIDGET
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register PushWard services at component setup.

    Services live for the component's lifetime (not per config entry) so automations that
    reference them validate even before an entry loads; the handlers raise a clear error via
    ``_get_api`` when no entry is configured.
    """
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PushWard from a config entry."""
    session = async_get_clientsession(hass)
    api = PushWardApiClient(session, entry.data[CONF_SERVER_URL], entry.data[CONF_INTEGRATION_KEY])

    # The usage coordinator's first refresh doubles as the connection/key check:
    # a bad key surfaces as ConfigEntryAuthFailed (→ reauth); a transient failure
    # surfaces as UpdateFailed, which async_config_entry_first_refresh translates
    # to ConfigEntryNotReady (→ retry).
    coordinator = PushWardUsageCoordinator(hass, api, entry)
    await coordinator.async_config_entry_first_refresh()

    entities = [dict(sub.data) for sub in entry.subentries.values() if sub.subentry_type == SUBENTRY_TYPE_ENTITY]
    widgets = [dict(sub.data) for sub in entry.subentries.values() if sub.subentry_type == SUBENTRY_TYPE_WIDGET]
    manager = ActivityManager(hass, api, entities, entry)
    widget_manager = WidgetManager(hass, api, widgets, entry)
    await asyncio.gather(manager.async_start(), widget_manager.async_start())

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "manager": manager,
        "widget_manager": widget_manager,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry or subentry updates — reload entity + widget tracking."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data is None:
        return
    entities = [dict(sub.data) for sub in entry.subentries.values() if sub.subentry_type == SUBENTRY_TYPE_ENTITY]
    widgets = [dict(sub.data) for sub in entry.subentries.values() if sub.subentry_type == SUBENTRY_TYPE_WIDGET]
    widget_manager: WidgetManager | None = data.get("widget_manager")
    reloads = [data["manager"].async_reload(entities)]
    if widget_manager is not None:
        reloads.append(widget_manager.async_reload(widgets))
    await asyncio.gather(*reloads)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entry to current version."""
    _LOGGER.debug("Migrating PushWard from version %s", config_entry.version)

    if config_entry.version == 1:
        # V1 stored entities in options; V2 uses subentries.
        # Clear legacy options — users re-add entities as subentries.
        hass.config_entries.async_update_entry(config_entry, version=2, options={})

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload PushWard config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if data:
        widget_manager: WidgetManager | None = data.get("widget_manager")
        stops = [data["manager"].async_stop()]
        if widget_manager is not None:
            stops.append(widget_manager.async_stop())
        await asyncio.gather(*stops)

    # Non-persistent usage-limit repair issues clear on restart but not on a plain
    # unload/reload — drop any outstanding ones so they don't linger after teardown.
    for resource in USAGE_LIMIT_RESOURCES:
        ir.async_delete_issue(hass, DOMAIN, usage_limit_issue_id(entry.entry_id, resource.used_key))

    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete server-side widgets, then persisted history and widget cache, on removal.

    Removing the whole integration must also delete every tracked widget server-side —
    otherwise the widget rows + device widget-push tokens leak forever. Per-subentry removal
    is handled by WidgetManager.async_reload, but no manager is live here (async_unload_entry
    already ran and popped hass.data), so build a throwaway client from entry.data.
    """
    widget_slugs = [
        slug
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_WIDGET and (slug := sub.data.get(CONF_SLUG))
    ]
    if widget_slugs:
        api = PushWardApiClient(
            async_get_clientsession(hass),
            entry.data[CONF_SERVER_URL],
            entry.data[CONF_INTEGRATION_KEY],
        )
        # delete_widget is 404-safe; isolate failures so one bad slug can't strand the rest.
        await asyncio.gather(*(api.delete_widget(slug) for slug in widget_slugs), return_exceptions=True)
    await build_history_store(hass, entry.entry_id).async_remove()
    await build_widget_store(hass, entry.entry_id).async_remove()
