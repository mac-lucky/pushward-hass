"""Websocket commands behind the PushWard sidebar panel, all admin only.

pushward/preview renders what a saved or draft subentry shows, read-only and
without network calls. pushward/send_test pushes such a payload to fixed slugs,
so repeated tests reuse one activity, widget or banner. pushward/api carries the
embedded playground's requests, limited to an allowlist of endpoints.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Coroutine, Mapping
from functools import wraps
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.util import dt as dt_util

from .activity_manager import _companion_entity_ids
from .api import PushWardApiError, PushWardAuthError, PushWardNotFoundError, PushWardQuotaExceededError
from .config_flow import (
    _details_schema,
    _entity_details_to_config,
    _entity_reconfigure_defaults,
    _entity_template_schema,
    _heatmap_statistics,
    _todo_data,
    _todo_schema,
    _widget_details_defaults,
    _widget_details_schema,
    _widget_details_to_config,
    _widget_reconfigure_defaults,
    _widget_step1_schema,
)
from .const import (
    _SLUG_RE,
    ACTIVITY_STATE_ENDED,
    ACTIVITY_STATE_ONGOING,
    ACTIVITY_STATES,
    CONF_ENTITY_ID,
    CONF_SLUG,
    CONF_SUBENTRY_ID,
    CONF_TEMPLATE,
    CONF_WIDGET_TEMPLATE,
    DOMAIN,
    NOTIFICATION_LEVELS,
    PRIORITY_MIN,
    QUOTA_KIND_LIVE_ACTIVITY_UPDATES,
    QUOTA_KIND_NOTIFICATIONS,
    QUOTA_KIND_WIDGET_UPDATES,
    SERVICE_TEMPLATES,
    SUBENTRY_TYPE_ENTITY,
    SUBENTRY_TYPE_TODO,
    SUBENTRY_TYPE_WIDGET,
    WIDGET_TEMPLATE_HEATMAP,
    WIDGET_TEMPLATES,
)
from .content_mapper import lookup_registry_icon, map_content
from .e2e import E2EError, seal
from .recorder_history import async_daily_values
from .widget_manager import TrackedWidget, _entity_ids_for_widget, heatmap_read
from .widget_mapper import map_widget_content, widget_name_from_config

PREVIEW_ACTIVITY_SLUG = "ha-preview"
PREVIEW_WIDGET_SLUG = "ha-preview-widget"
# The banner of every test notification replaces the previous one.
PREVIEW_COLLAPSE_ID = "ha-preview"
MAX_PAYLOAD_BYTES = 64 * 1024

_KINDS = {SUBENTRY_TYPE_ENTITY: "activity", SUBENTRY_TYPE_WIDGET: "widget", SUBENTRY_TYPE_TODO: "notification"}

_PAYLOAD_SCHEMAS = {
    "activity": vol.Schema(
        {
            vol.Required("name"): str,
            vol.Required("content"): vol.Schema(
                {vol.Required("template"): vol.In(SERVICE_TEMPLATES)}, extra=vol.ALLOW_EXTRA
            ),
            vol.Optional("state"): vol.In(ACTIVITY_STATES),
        }
    ),
    "widget": vol.Schema(
        {
            vol.Optional("name"): str,
            vol.Required("template"): vol.In(WIDGET_TEMPLATES),
            vol.Required("content"): dict,
        }
    ),
    # A POST /notifications body; what create_notification does not take (push,
    # collapse_id, ...) is dropped.
    "notification": vol.Schema(
        {
            vol.Required("title"): str,
            vol.Required("body"): str,
            vol.Optional("subtitle"): str,
            vol.Optional("level"): vol.In(NOTIFICATION_LEVELS),
            vol.Optional("thread_id"): str,
            vol.Optional("source"): str,
            vol.Optional("source_display_name"): str,
            vol.Optional("url"): str,
            vol.Optional("metadata"): {str: str},
            vol.Optional("actions"): [dict],
        },
        extra=vol.REMOVE_EXTRA,
    ),
}

# The playground's endpoints, each with the quota it spends (as the client methods
# for them pass it). A slug in the path is matched as {slug}.
_PLAYGROUND_ROUTES: dict[tuple[str, str], str | None] = {
    ("POST", "/activities"): QUOTA_KIND_LIVE_ACTIVITY_UPDATES,
    ("PATCH", "/activities/{slug}"): QUOTA_KIND_LIVE_ACTIVITY_UPDATES,
    ("POST", "/notifications"): QUOTA_KIND_NOTIFICATIONS,
    ("POST", "/widgets"): None,
    ("PATCH", "/widgets/{slug}"): QUOTA_KIND_WIDGET_UPDATES,
    ("GET", "/app/status"): None,
    ("GET", "/app/status?channel=play"): None,
}
_SLUG_COLLECTIONS = ("/activities", "/widgets")
_CONTENT_TYPES = ("application/json", "application/merge-patch+json")
# The notification text an e2e key seals (create_notification seals the same four).
_SEALED_FIELDS = ("title", "subtitle", "body", "url")


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    for command in (ws_preview, ws_send_test, ws_api):
        websocket_api.async_register_command(hass, command)


def _entry_data(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict | None:
    """The runtime objects of the loaded entry the message names; answers not_found otherwise."""
    data = (hass.data.get(DOMAIN) or {}).get(msg["entry_id"])
    if data is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "No loaded PushWard entry with that id")
    return data


def _too_big(value: Any) -> bool:
    return len(json.dumps(value)) > MAX_PAYLOAD_BYTES


def _api_errors(func: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., Coroutine[Any, Any, None]]:
    """Answer a failed PushWard call with an error code; the message never carries the key."""

    @wraps(func)
    async def wrapper(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
        try:
            await func(hass, connection, msg)
        except PushWardQuotaExceededError as err:
            connection.send_error(msg["id"], "quota_exceeded", str(err))
        except PushWardAuthError:
            connection.send_error(msg["id"], "auth_failed", "PushWard rejected the integration key")
        except (PushWardApiError, E2EError) as err:
            connection.send_error(msg["id"], "api_error", str(err))

    return wrapper


# --- pushward/preview ---


def _submitted(schema: vol.Schema, values: dict) -> tuple[dict | None, dict[str, str]]:
    """values as a step handler gets them once HA ran the submit through the form schema.

    Defaults fill what is left out; otherwise {field: message} for what the schema
    refuses. A missing section counts as empty: the form always sends one.
    """
    sections = {str(key): {} for key, validator in schema.schema.items() if isinstance(validator, section)}
    try:
        return schema({**sections, **values}), {}
    except vol.MultipleInvalid as err:
        return None, {str(error.path[-1]) if error.path else "base": error.error_message for error in err.errors}


async def _parse_draft(
    hass: HomeAssistant, subentry_type: str, draft: dict, saved: Mapping[str, Any] | None
) -> tuple[dict | None, dict[str, str]]:
    """A draft taken the way its flow takes a form submit, so what previews is what saves.

    HA's frontend leaves top-level empty strings out, the step schemas fill their
    defaults (built from the saved subentry on reconfigure), then the details parse runs.
    """
    draft = {key: value for key, value in draft.items() if value != ""}
    if subentry_type == SUBENTRY_TYPE_TODO:
        values, errors = _submitted(_todo_schema(saved), draft)
        return (None, errors) if errors else (_todo_data(values), {})
    step1_schema = (_entity_template_schema if subentry_type == SUBENTRY_TYPE_ENTITY else _widget_step1_schema)(
        dict(saved) if saved is not None else None
    )
    step1_keys = {str(key) for key in step1_schema.schema}
    step1, errors = _submitted(step1_schema, {key: value for key, value in draft.items() if key in step1_keys})
    if errors:
        return None, errors
    details = {key: value for key, value in draft.items() if key not in step1_keys}
    entity_id = step1[CONF_ENTITY_ID]
    if subentry_type == SUBENTRY_TYPE_ENTITY:
        defaults = _entity_reconfigure_defaults(saved) if saved is not None else None
        schema = _details_schema(entity_id, step1[CONF_TEMPLATE], defaults=defaults, hass=hass)
        user_input, errors = _submitted(schema, details)
        return (None, errors) if errors else _entity_details_to_config(step1, user_input, hass)
    info, errors = await _heatmap_statistics(hass, step1)
    if errors:
        return None, errors
    template = step1[CONF_WIDGET_TEMPLATE]
    stored = _widget_reconfigure_defaults(saved) if saved is not None else {}
    defaults = _widget_details_defaults(template, stored, info)
    user_input, errors = _submitted(_widget_details_schema(entity_id, template, defaults=defaults), details)
    return (None, errors) if errors else _widget_details_to_config(step1, user_input, info)


async def _activity_preview(hass: HomeAssistant, data: dict, cfg: dict, saved: bool) -> dict | None:
    entity_id = cfg[CONF_ENTITY_ID]
    if (state := hass.states.get(entity_id)) is None:
        return None
    manager = data["manager"]
    tracked = manager._tracked.get(entity_id) if saved else None
    # A running activity's last frame has what a fresh map lacks: log lines, artwork, thumbhash.
    content = tracked.last_content if tracked is not None else None
    if content is None:
        content = map_content(state, cfg, registry_icon=lookup_registry_icon(hass, entity_id), hass=hass)
    return {"name": manager._activity_name(entity_id, cfg), "content": content}


async def _widget_preview(hass: HomeAssistant, data: dict, cfg: dict, saved: bool) -> dict | None:
    widget_manager = data["widget_manager"]
    heatmap = cfg.get(CONF_WIDGET_TEMPLATE) == WIDGET_TEMPLATE_HEATMAP
    tracked = widget_manager._tracked.get(cfg[CONF_SLUG]) if saved else None
    # Trend points sampled the way a push samples them, on a copy so the manager's buffer stays as it is.
    scratch = TrackedWidget(config=cfg)
    if tracked is not None:
        scratch.points_buffer.extend(tracked.points_buffer)
    await widget_manager._seed_points(scratch)
    cells = None
    if heatmap:
        # The manager's read of today when it has one: the panel previews on every state change.
        if tracked is not None and tracked.cells_read_day == dt_util.now().date():
            cells = tracked.cells
        else:
            cells = await async_daily_values(hass, *heatmap_read(cfg))
    # Mapped fresh even when a frame was sent: what cannot map now, the manager no longer pushes.
    content = map_widget_content(
        hass,
        cfg,
        registry_icon=widget_manager._lookup_registry_icon(cfg),
        points=widget_manager._sample_points(scratch),
        cells=cells,
    )
    if content is None:
        return None
    # The last sent frame matches what the phone shows (its trend arrow included).
    if tracked is not None and tracked.last_content is not None:
        content = tracked.last_content
    return {"name": widget_name_from_config(cfg, hass), "template": cfg[CONF_WIDGET_TEMPLATE], "content": content}


async def _todo_preview(hass: HomeAssistant, data: dict, cfg: dict, saved: bool) -> dict | None:
    return await data["todo_manager"].async_preview(cfg, cfg.get(CONF_SUBENTRY_ID, ""))


def _bound_entity_ids(subentry_type: str, cfg: dict) -> list[str]:
    """The entities whose changes the managers react to, so the panel knows when to refresh a preview."""
    if subentry_type == SUBENTRY_TYPE_ENTITY:
        return list(dict.fromkeys([cfg[CONF_ENTITY_ID], *_companion_entity_ids(cfg)]))
    if subentry_type == SUBENTRY_TYPE_WIDGET:
        return _entity_ids_for_widget(cfg)
    return [cfg[CONF_ENTITY_ID]]


def _running(task: asyncio.Task | None) -> bool:
    return task is not None and not task.done()


def _push_pending(data: dict, subentry_type: str, cfg: dict) -> bool:
    """Whether a manager still has a push of this subentry queued, in flight or ending.

    A saved preview shows the last pushed frame, so until that push lands it shows
    the one before; the panel asks again while this is true.
    """
    if subentry_type == SUBENTRY_TYPE_ENTITY:
        activity = data["manager"]._tracked.get(cfg[CONF_ENTITY_ID])
        return activity is not None and (
            activity.flush_unsub is not None
            or activity.update_pending
            or _running(activity.pending_task)
            or _running(activity.end_task)
        )
    if subentry_type == SUBENTRY_TYPE_WIDGET:
        widget = data["widget_manager"]._tracked.get(cfg[CONF_SLUG])
        return widget is not None and (widget.update_pending or _running(widget.pending_task))
    return False


_PREVIEWS = {
    SUBENTRY_TYPE_ENTITY: _activity_preview,
    SUBENTRY_TYPE_WIDGET: _widget_preview,
    SUBENTRY_TYPE_TODO: _todo_preview,
}


@websocket_api.websocket_command(
    {
        vol.Required("type"): "pushward/preview",
        vol.Required("entry_id"): str,
        # A draft may name the subentry it reconfigures; that only supplies the saved defaults.
        vol.Optional("subentry_id"): str,
        vol.Optional("subentry_type"): vol.In(list(_KINDS)),
        vol.Optional("data"): dict,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """What a saved subentry (subentry_id) or a draft (subentry_type + data) shows. Read-only."""
    if (data := _entry_data(hass, connection, msg)) is None:
        return
    is_draft = "data" in msg
    if is_draft != ("subentry_type" in msg) or not (is_draft or "subentry_id" in msg):
        connection.send_error(
            msg["id"], websocket_api.ERR_INVALID_FORMAT, "Give subentry_id, or subentry_type and data"
        )
        return
    sub = None
    if "subentry_id" in msg:
        entry = hass.config_entries.async_get_entry(msg["entry_id"])
        sub = entry.subentries.get(msg["subentry_id"]) if entry is not None else None
        if sub is None or (is_draft and sub.subentry_type != msg["subentry_type"]):
            connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "No subentry with that id")
            return
    if not is_draft:
        subentry_type, saved = sub.subentry_type, True
        cfg: dict | None = {**sub.data, CONF_SUBENTRY_ID: sub.subentry_id}
        errors: dict[str, str] = {}
    else:
        subentry_type, saved = msg["subentry_type"], False
        try:
            cfg, errors = await _parse_draft(hass, subentry_type, msg["data"], sub.data if sub else None)
        except (KeyError, TypeError, ValueError, AttributeError):
            # Shapes the form's own schema would have refused before the parse.
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "The draft cannot be parsed")
            return
    payload = await _PREVIEWS[subentry_type](hass, data, cfg, saved) if cfg is not None else None
    connection.send_result(
        msg["id"],
        {
            "kind": _KINDS[subentry_type],
            "payload": payload,
            "errors": errors or None,
            "available": payload is not None,
            "entity_ids": _bound_entity_ids(subentry_type, cfg) if cfg is not None else [],
            "pending": saved and _push_pending(data, subentry_type, cfg),
        },
    )


# --- pushward/send_test ---


@websocket_api.websocket_command(
    {
        vol.Required("type"): "pushward/send_test",
        vol.Required("entry_id"): str,
        vol.Required("kind"): vol.In(["activity", "widget", "notification"]),
        vol.Required("action"): vol.In(["send", "end"]),
        vol.Optional("payload"): dict,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_api_errors
async def ws_send_test(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Show a preview payload on the phone under a fixed slug, or end / delete it again."""
    if (data := _entry_data(hass, connection, msg)) is None:
        return
    kind, action, payload = msg["kind"], msg["action"], msg.get("payload")
    if kind == "notification" and action == "end":
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "A notification cannot be ended")
        return
    if payload is None and action == "send":
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "send needs a payload")
        return
    if payload is not None:
        if _too_big(payload):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "The payload is too large")
            return
        try:
            payload = _PAYLOAD_SCHEMAS[kind](payload)
        except vol.Invalid as err:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
            return
    api = data["api"]
    if kind == "activity" and action == "send":
        # Priority 0, the lowest: at the account's Live Activity cap the server evicts the
        # lowest-priority, least recently updated activity, so a test already on the phone
        # goes before any tracked activity above priority 0.
        await api.create_activity(PREVIEW_ACTIVITY_SLUG, payload["name"], PRIORITY_MIN)
        await api.update_activity(PREVIEW_ACTIVITY_SLUG, ACTIVITY_STATE_ONGOING, payload["content"])
    elif kind == "activity":
        # An empty merge patch keeps the stored frame and only ends it. A 404 is already gone
        # (dismissed and cleaned up, or deleted elsewhere): nothing is left to end.
        with contextlib.suppress(PushWardNotFoundError):
            try:
                await api.update_activity(
                    PREVIEW_ACTIVITY_SLUG, ACTIVITY_STATE_ENDED, payload["content"] if payload else {}
                )
            except PushWardQuotaExceededError:
                # Out of updates: the unmetered DELETE still takes it off the phone, like the manager's end.
                await api.delete_activity(PREVIEW_ACTIVITY_SLUG)
    elif kind == "widget" and action == "send":
        name = payload.get("name") or "PushWard preview"
        await api.create_widget(PREVIEW_WIDGET_SLUG, name, payload["template"], payload["content"])
    elif kind == "widget":
        await api.delete_widget(PREVIEW_WIDGET_SLUG)
    else:
        # Same trimming the reminder manager uses, so a long to-do preview still seals.
        await api.create_notification(**payload, collapse_id=PREVIEW_COLLAPSE_ID, trim_to_fit=True)
    connection.send_result(msg["id"])


# --- pushward/api ---


def _route(method: str, path: str) -> tuple[str, str] | None:
    """The allowlist key of a request; None for a slug path whose slug is not valid."""
    collection, _, slug = path.rpartition("/")
    if collection in _SLUG_COLLECTIONS:
        return (method, f"{collection}/{{slug}}") if _SLUG_RE.fullmatch(slug) else None
    return method, path


def _sealed_notification(key: bytes, body: dict) -> dict:
    """A playground notification with its text sealed, the way create_notification sends it."""
    text = {name: body[name] for name in _SEALED_FIELDS if body.get(name) is not None}
    if not all(isinstance(value, str) for value in text.values()):
        raise E2EError("title, subtitle, body and url must be text to be sealed")
    envelope = seal(
        key,
        title=text.get("title", ""),
        body=text.get("body", ""),
        subtitle=text.get("subtitle"),
        url=text.get("url"),
    )
    return {**{name: value for name, value in body.items() if name not in _SEALED_FIELDS}, "encrypted": envelope}


@websocket_api.websocket_command(
    {
        vol.Required("type"): "pushward/api",
        vol.Required("entry_id"): str,
        vol.Required("method"): str,
        vol.Required("path"): str,
        vol.Optional("body"): dict,
        vol.Optional("content_type"): vol.In(_CONTENT_TYPES),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_api_errors
async def ws_api(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """One playground request; a non-2xx answer comes back as its status and body, like fetch."""
    if (data := _entry_data(hass, connection, msg)) is None:
        return
    route = _route(msg["method"], msg["path"])
    if route not in _PLAYGROUND_ROUTES:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_ALLOWED, "The playground cannot call this endpoint")
        return
    body = msg.get("body")
    if body is not None and _too_big(body):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "The body is too large")
        return
    api = data["api"]
    notification = route == ("POST", "/notifications")
    if notification and body is not None and "encrypted" in body:
        # A sealed body goes out as it is, so plaintext next to the envelope would reach the server.
        if any(body.get(name) is not None for name in _SEALED_FIELDS):
            connection.send_error(
                msg["id"], websocket_api.ERR_NOT_ALLOWED, "A sealed notification cannot also carry its text in plain"
            )
            return
    elif notification and body is not None and api.e2e_key is not None:
        body = _sealed_notification(api.e2e_key, body)
    status, answer = await api._request_with_retry(
        msg["method"],
        msg["path"],
        json=body,
        quota_kind=_PLAYGROUND_ROUTES[route],
        idempotency_key=uuid4().hex if notification else None,
        content_type=msg.get("content_type"),
        raw=True,
    )
    connection.send_result(msg["id"], {"status": status, "body": answer})
