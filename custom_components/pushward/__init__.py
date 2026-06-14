"""PushWard integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from functools import partial

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
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_PRIORITY,
    DOMAIN,
    NOTIFICATION_LEVELS,
    SCALES,
    SEVERITIES,
    SOUNDS,
    SUBENTRY_TYPE_ENTITY,
    SUBENTRY_TYPE_WIDGET,
    TEMPLATES,
    validate_slug,
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

# Composable field-groups for the update services. The shared groups (top-level +
# universal labels/appearance) merge with one template-specific group per service, so each
# per-template schema accepts only that template's fields; the deprecated update_activity
# schema is rebuilt below as the union of all of them.
_UPDATE_TOPLEVEL_FIELDS = {
    vol.Required("slug"): validate_slug,
    vol.Required("state"): vol.In(ACTIVITY_STATES),
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
_COUNTDOWN_TEMPLATE_FIELDS = {
    vol.Optional("end_date"): vol.Coerce(int),
    vol.Optional("warning_threshold"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional("alarm"): cv.boolean,
    vol.Optional("snooze_seconds"): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
}
_STEPS_TEMPLATE_FIELDS = {
    vol.Optional("total_steps"): vol.Coerce(int),
    vol.Optional("current_step"): vol.Coerce(int),
    vol.Optional("step_labels"): list,
    vol.Optional("step_rows"): list,
    vol.Optional("url"): validate_url,
    vol.Optional("secondary_url"): validate_url,
}
_ALERT_TEMPLATE_FIELDS = {
    vol.Optional("severity"): vol.In(SEVERITIES),
    vol.Optional("fired_at"): vol.Coerce(int),
    vol.Optional("url"): validate_url,
    vol.Optional("secondary_url"): validate_url,
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
    vol.Optional("scale"): vol.In(SCALES),
    vol.Optional("decimals"): vol.All(vol.Coerce(int), vol.Range(min=0, max=10)),
    vol.Optional("smoothing"): bool,
    vol.Optional("thresholds"): list,
}


def _update_template_schema(*template_fields: dict) -> vol.Schema:
    """Build an update_activity schema from the shared groups plus the given template groups."""
    merged = {**_UPDATE_TOPLEVEL_FIELDS, **_UNIVERSAL_LABEL_FIELDS, **_UNIVERSAL_APPEARANCE_FIELDS}
    for group in template_fields:
        merged.update(group)
    return vol.Schema(merged)


_UPDATE_TEMPLATE_SCHEMAS = {
    "generic": _update_template_schema(),
    "countdown": _update_template_schema(_COUNTDOWN_TEMPLATE_FIELDS),
    "steps": _update_template_schema(_STEPS_TEMPLATE_FIELDS),
    "alert": _update_template_schema(_ALERT_TEMPLATE_FIELDS),
    "gauge": _update_template_schema(_GAUGE_TEMPLATE_FIELDS),
    "timeline": _update_template_schema(_TIMELINE_TEMPLATE_FIELDS),
}

# The deprecated update_activity accepts every template's fields (plus an explicit
# `template` selector) — i.e. the union of all per-template schemas.
SCHEMA_UPDATE_ACTIVITY = _update_template_schema(
    {vol.Optional("template"): str},
    _COUNTDOWN_TEMPLATE_FIELDS,
    _STEPS_TEMPLATE_FIELDS,
    _ALERT_TEMPLATE_FIELDS,
    _GAUGE_TEMPLATE_FIELDS,
    _TIMELINE_TEMPLATE_FIELDS,
)

SCHEMA_CREATE_ACTIVITY = vol.Schema(
    {
        vol.Required("slug"): validate_slug,
        vol.Required("name"): str,
        vol.Optional("priority", default=DEFAULT_PRIORITY): vol.Coerce(int),
        vol.Optional("ended_ttl"): vol.Coerce(int),
        vol.Optional("stale_ttl"): vol.Coerce(int),
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

SCHEMA_ACTION = vol.Schema(
    {
        vol.Required("id"): vol.All(str, vol.Length(min=1)),
        vol.Required("title"): vol.All(str, vol.Length(min=1)),
        vol.Optional("url"): validate_url,
        vol.Optional("foreground"): cv.boolean,
        vol.Optional("destructive"): cv.boolean,
        vol.Optional("authentication_required"): cv.boolean,
        vol.Optional("icon"): str,
    }
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

SCHEMA_WIDGET_REFRESH = vol.All(
    vol.Schema(
        {
            vol.Exclusive("slug", "widget_target"): validate_slug,
            vol.Exclusive("entity_id", "widget_target"): cv.entity_id,
        }
    ),
    cv.has_at_least_one_key("slug", "entity_id"),
)


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

    sound/priority are top-level PATCH kwargs; every remaining field is content. state_text
    is the user-facing name for the content "state" string. The per-template actions inject
    their template (their schema omits it); the deprecated alias lets the caller pass it.
    """
    api = _get_api(hass)
    content = dict(call.data)
    slug = content.pop("slug")
    state = content.pop("state")
    sound = content.pop("sound", None) or None
    priority = content.pop("priority", None)
    if "state_text" in content:
        content["state"] = content.pop("state_text")
    if template is not None:
        content["template"] = template
    with _surface_api_errors():
        await api.update_activity(slug, state, content, sound=sound, priority=priority)


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
            translation_placeholders={"slug": str(slug), "entity_id": str(entity_id)},
        )


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

    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete persisted history and widget cache when the config entry is removed."""
    await build_history_store(hass, entry.entry_id).async_remove()
    await build_widget_store(hass, entry.entry_id).async_remove()
