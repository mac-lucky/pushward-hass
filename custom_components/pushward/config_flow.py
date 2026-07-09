"""Config flow and subentry flow for PushWard integration."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    AttributeSelector,
    AttributeSelectorConfig,
    BooleanSelector,
    ColorRGBSelector,
    EntitySelector,
    EntitySelectorConfig,
    IconSelector,
    IconSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import PushWardApiClient, PushWardApiError, PushWardAuthError
from .const import (
    APP_STORE_URL,
    BOARD_MAX_TILES,
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ACTIVITY_NAME,
    CONF_ALARM,
    CONF_BACKGROUND_COLOR,
    CONF_BACKGROUND_COLOR_ATTRIBUTE,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_CURRENT_STEP_ENTITY,
    CONF_DECIMALS,
    CONF_END_STATES,
    CONF_ENDED_TTL,
    CONF_ENTITY_ID,
    CONF_FIRED_AT_ATTRIBUTE,
    CONF_FIRED_AT_ENTITY,
    CONF_HISTORY_PERIOD,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_INTEGRATION_KEY,
    CONF_LABEL,
    CONF_LABEL_ATTRIBUTE,
    CONF_LIVE_PROGRESS,
    CONF_LOG_COLUMNS,
    CONF_LOG_LEVEL_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PRIMARY_SERIES,
    CONF_PRIORITY,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ATTR,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SCALE,
    CONF_SECONDARY_URL,
    CONF_SECONDARY_URL_FOREGROUND,
    CONF_SECONDARY_URL_TITLE,
    CONF_SERIES,
    CONF_SERIES_ENTITIES,
    CONF_SERVER_URL,
    CONF_SEVERITY,
    CONF_SEVERITY_LABEL,
    CONF_SLUG,
    CONF_SMOOTHING,
    CONF_SNOOZE_SECONDS,
    CONF_SOUND,
    CONF_STALE_TTL,
    CONF_START_STATES,
    CONF_STAT_ROWS,
    CONF_STATE_LABELS,
    CONF_STEP_LABELS,
    CONF_STEP_ROWS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_SUBTITLE_ENTITY,
    CONF_TAP_ACTION_FOREGROUND,
    CONF_TAP_ACTION_URL,
    CONF_TEMPLATE,
    CONF_TEXT_COLOR,
    CONF_TEXT_COLOR_ATTRIBUTE,
    CONF_THRESHOLDS,
    CONF_TILES,
    CONF_TOTAL_STEPS,
    CONF_UNIT,
    CONF_UNITS,
    CONF_UPDATE_INTERVAL,
    CONF_URL,
    CONF_URL_FOREGROUND,
    CONF_URL_TITLE,
    CONF_VALUE_ATTRIBUTE,
    CONF_VALUE_ENTITY,
    CONF_WARNING_THRESHOLD,
    CONF_WIDGET_NAME,
    CONF_WIDGET_POLL_INTERVAL,
    CONF_WIDGET_TEMPLATE,
    CONF_WIDGET_TRIGGER_MODE,
    DANGEROUS_URL_SCHEMES,
    DEFAULT_DECIMALS,
    DEFAULT_HISTORY_PERIOD,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    DEFAULT_PRIORITY,
    DEFAULT_SCALE,
    DEFAULT_SERVER_URL,
    DEFAULT_SEVERITY,
    DEFAULT_TAP_ACTION_FOREGROUND,
    DEFAULT_TOTAL_STEPS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_WIDGET_POLL_INTERVAL,
    DOMAIN,
    LIVE_PROGRESS_TEMPLATES,
    LOG_MAX_COLUMNS,
    MAX_LONG_TEXT_LEN,
    MAX_SLUG_LEN,
    MAX_TAP_ACTION_TITLE_LEN,
    MAX_TEXT_LEN,
    MAX_URL_LEN,
    PRIORITY_MAX,
    PRIORITY_MIN,
    SCALES,
    SEVERITIES,
    SNOOZE_SECONDS_MAX,
    SNOOZE_SECONDS_MIN,
    SOUNDS,
    SUBENTRY_TYPE_ENTITY,
    SUBENTRY_TYPE_WIDGET,
    TEMPLATES,
    TIMELINE_MAX_SERIES,
    TIMELINE_SERIES_LABEL_MAX,
    TOTAL_STEPS_MAX,
    UPDATE_INTERVAL_MIN,
    WARNING_THRESHOLD_MAX,
    WIDGET_LABEL_MAX,
    WIDGET_MAX_STAT_ROWS,
    WIDGET_NAME_MAX,
    WIDGET_POLL_INTERVAL_MAX,
    WIDGET_POLL_INTERVAL_MIN,
    WIDGET_SEVERITIES,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_PROGRESS,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_STATUS,
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TEMPLATES,
    WIDGET_TRIGGER_EVENT,
    WIDGET_TRIGGER_MODES,
    WIDGET_UNIT_MAX,
    normalize_slug,
)
from .content_mapper import get_domain_defaults, sanitize_slug

_LOGGER = logging.getLogger(__name__)

_INTEGRATION_KEY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_INTEGRATION_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }
)

_TTL_MIN = 1
_TTL_MAX = 2592000  # 30 days


async def _validate_integration_key(
    hass: HomeAssistant,
    key: str,
    context: str,
    server_url: str = DEFAULT_SERVER_URL,
) -> dict[str, str]:
    """Validate an integration key against the PushWard API.

    Returns an error dict (empty on success).
    """
    session = async_get_clientsession(hass)
    client = PushWardApiClient(session, server_url, key)
    try:
        await client.validate_connection()
    except PushWardAuthError:
        return {"base": "invalid_auth"}
    except (PushWardApiError, aiohttp.ClientError, TimeoutError, OSError) as err:
        _LOGGER.warning("PushWard %s failed: %s", context, err)
        return {"base": "cannot_connect"}
    return {}


def _entity_domain(entity_id: str) -> str:
    """Extract the domain from an entity_id (e.g. 'sensor.temp' -> 'sensor')."""
    return entity_id.split(".")[0] if "." in entity_id else ""


def _entity_template_schema(defaults: dict | None = None) -> vol.Schema:
    """Build step-1 schema: entity picker + template."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_ENTITY_ID,
                default=d.get(CONF_ENTITY_ID, ""),
            ): EntitySelector(EntitySelectorConfig()),
            vol.Optional(
                CONF_TEMPLATE,
                default=d.get(CONF_TEMPLATE, "generic"),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=TEMPLATES,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _collect_entity_states(hass: HomeAssistant | None, entity_id: str, domain: str) -> list[str]:
    """Collect known state options for an entity from HA runtime data."""
    states: list[str] = []
    if hass is None:
        return states

    state_obj = hass.states.get(entity_id)
    if state_obj is None:
        return states

    # Current state
    if state_obj.state not in ("unavailable", "unknown"):
        states.append(state_obj.state)

    # select / input_select entities expose their options attribute
    if domain in ("select", "input_select"):
        options = state_obj.attributes.get("options", [])
        if isinstance(options, list):
            for opt in options:
                if isinstance(opt, str) and opt not in states:
                    states.append(opt)

    return states


# Device classes where gauge is the natural template.
_GAUGE_DEVICE_CLASSES = frozenset(
    {
        "temperature",
        "humidity",
        "battery",
        "power",
        "energy",
        "voltage",
        "current",
        "pressure",
        "illuminance",
        "speed",
        "wind_speed",
        "signal_strength",
        "moisture",
        "pm25",
        "pm10",
        "carbon_dioxide",
        "carbon_monoxide",
        "distance",
        "weight",
        "volume",
        "data_rate",
        "data_size",
        "frequency",
        "sound_pressure",
        "irradiance",
        "precipitation_intensity",
    }
)


def _suggest_template(hass: HomeAssistant | None, entity_id: str) -> str:
    """Suggest the best template for an entity based on domain/device_class/state_class."""
    if not entity_id or hass is None:
        return "generic"

    domain = _entity_domain(entity_id)

    if domain == "timer":
        return "countdown"
    if domain == "light":
        return "gauge"

    state_obj = hass.states.get(entity_id)
    if state_obj is None:
        return "generic"

    attrs = state_obj.attributes
    if domain in ("sensor", "number"):
        if attrs.get("state_class") in ("measurement", "total"):
            return "gauge"
        if attrs.get("device_class", "") in _GAUGE_DEVICE_CLASSES:
            return "gauge"

    return "generic"


def _details_schema(
    entity_id: str,
    template: str,
    defaults: dict | None = None,
    hass: HomeAssistant | None = None,
) -> vol.Schema:
    """Build step-2 schema with all config fields and dynamic selectors."""
    d = defaults or {}
    domain = _entity_domain(entity_id)
    domain_defs = get_domain_defaults(domain)

    # State options: domain defaults + entity runtime states + previously saved
    start_opts = list(domain_defs.get("start_states", []))
    end_opts = list(domain_defs.get("end_states", []))

    entity_states = _collect_entity_states(hass, entity_id, domain)
    for s in entity_states:
        if s not in start_opts:
            start_opts.append(s)
        if s not in end_opts:
            end_opts.append(s)

    saved_start = d.get(CONF_START_STATES, [])
    saved_end = d.get(CONF_END_STATES, [])
    if isinstance(saved_start, list):
        for s in saved_start:
            if s not in start_opts:
                start_opts.append(s)
    if isinstance(saved_end, list):
        for s in saved_end:
            if s not in end_opts:
                end_opts.append(s)

    start_default = d.get(CONF_START_STATES) if d.get(CONF_START_STATES) else domain_defs.get("start_states", [])
    end_default = d.get(CONF_END_STATES) if d.get(CONF_END_STATES) else domain_defs.get("end_states", [])

    attr_selector = AttributeSelector(AttributeSelectorConfig(entity_id=entity_id))
    entity_selector = EntitySelector(EntitySelectorConfig())

    # ColorRGBSelector requires a valid [r,g,b] default — omit if no color saved
    accent_key = _color_vol_key(CONF_ACCENT_COLOR, d)
    bg_color_key = _color_vol_key(CONF_BACKGROUND_COLOR, d)
    text_color_key = _color_vol_key(CONF_TEXT_COLOR, d)

    # TTL defaults: only set default when valid value exists
    ended_ttl_val = d.get(CONF_ENDED_TTL)
    ended_ttl_key = (
        vol.Optional(CONF_ENDED_TTL, default=ended_ttl_val)
        if ended_ttl_val is not None
        else vol.Optional(CONF_ENDED_TTL)
    )
    stale_ttl_val = d.get(CONF_STALE_TTL)
    stale_ttl_key = (
        vol.Optional(CONF_STALE_TTL, default=stale_ttl_val)
        if stale_ttl_val is not None
        else vol.Optional(CONF_STALE_TTL)
    )

    fields: dict = {}

    # --- Start/end states (multi-select with custom values) ---
    fields[vol.Optional(CONF_START_STATES, default=start_default)] = SelectSelector(
        SelectSelectorConfig(
            options=start_opts,
            multiple=True,
            custom_value=True,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )
    fields[vol.Optional(CONF_END_STATES, default=end_default)] = SelectSelector(
        SelectSelectorConfig(
            options=end_opts,
            multiple=True,
            custom_value=True,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )

    # --- Template-specific fields ---
    if template in ("generic", "steps"):
        fields[_entity_source_key(CONF_PROGRESS_ENTITY, d)] = entity_selector
        fields[
            vol.Optional(
                CONF_PROGRESS_ATTRIBUTE,
                description={"suggested_value": d.get(CONF_PROGRESS_ATTRIBUTE, "")},
            )
        ] = attr_selector
    # live_progress derives its window from the remaining-time source, so every
    # template offering the toggle below must offer the source here too.
    if template == "countdown" or template in LIVE_PROGRESS_TEMPLATES:
        fields[_entity_source_key(CONF_REMAINING_TIME_ENTITY, d)] = entity_selector
        fields[
            vol.Optional(
                CONF_REMAINING_TIME_ATTR,
                description={"suggested_value": d.get(CONF_REMAINING_TIME_ATTR, "")},
            )
        ] = attr_selector
    if template in LIVE_PROGRESS_TEMPLATES:
        # Interpolate the bar to full and count down an ETA on the device. On steps
        # the remaining time is read as the time left in the current step, so the
        # bar fills that step rather than the whole run.
        fields[
            vol.Optional(
                CONF_LIVE_PROGRESS,
                default=d.get(CONF_LIVE_PROGRESS, False),
            )
        ] = BooleanSelector()
    if template == "steps":
        fields[
            vol.Optional(
                CONF_TOTAL_STEPS,
                default=d.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=1, max=TOTAL_STEPS_MAX))
        fields[_entity_source_key(CONF_CURRENT_STEP_ENTITY, d)] = entity_selector
        fields[
            vol.Optional(
                CONF_CURRENT_STEP_ATTR,
                description={"suggested_value": d.get(CONF_CURRENT_STEP_ATTR, "")},
            )
        ] = attr_selector
        fields[
            vol.Optional(
                CONF_STEP_LABELS,
                default=d.get(CONF_STEP_LABELS, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))
        fields[
            vol.Optional(
                CONF_STEP_ROWS,
                default=d.get(CONF_STEP_ROWS, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_TEXT_LEN))
    if template == "alert":
        fields[
            vol.Optional(
                CONF_SEVERITY,
                default=d.get(CONF_SEVERITY, DEFAULT_SEVERITY),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=SEVERITIES,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        fields[
            vol.Optional(
                CONF_SEVERITY_LABEL,
                default=d.get(CONF_SEVERITY_LABEL, ""),
            )
        ] = TextSelector()
        fields[_entity_source_key(CONF_FIRED_AT_ENTITY, d)] = entity_selector
        fields[
            vol.Optional(
                CONF_FIRED_AT_ATTRIBUTE,
                description={"suggested_value": d.get(CONF_FIRED_AT_ATTRIBUTE, "")},
            )
        ] = attr_selector
    if template == "gauge":
        fields[_entity_source_key(CONF_VALUE_ENTITY, d)] = entity_selector
        fields[
            vol.Optional(
                CONF_VALUE_ATTRIBUTE,
                description={"suggested_value": d.get(CONF_VALUE_ATTRIBUTE, "")},
            )
        ] = attr_selector
        fields[
            vol.Required(
                CONF_MIN_VALUE,
                default=d.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE),
            )
        ] = vol.Coerce(float)
        fields[
            vol.Required(
                CONF_MAX_VALUE,
                default=d.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE),
            )
        ] = vol.Coerce(float)
        fields[
            vol.Optional(
                CONF_UNIT,
                default=d.get(CONF_UNIT, ""),
            )
        ] = vol.All(str, vol.Length(max=32))
    if template == "timeline":
        fields[
            vol.Optional(
                CONF_SERIES,
                default=d.get(CONF_SERIES, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))
        # Bind separate entities as named series (mirrors the board-tile string).
        # Format: '[Label=]entity_id[:attribute]' comma-separated.
        fields[
            vol.Optional(
                CONF_SERIES_ENTITIES,
                default=d.get(CONF_SERIES_ENTITIES, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))
        fields[
            vol.Optional(
                CONF_UNITS,
                default=d.get(CONF_UNITS, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))
        fields[
            vol.Optional(
                CONF_PRIMARY_SERIES,
                default=d.get(CONF_PRIMARY_SERIES, ""),
            )
        ] = vol.All(str, vol.Length(max=32))
        fields[_entity_source_key(CONF_VALUE_ENTITY, d)] = entity_selector
        fields[
            vol.Optional(
                CONF_VALUE_ATTRIBUTE,
                description={"suggested_value": d.get(CONF_VALUE_ATTRIBUTE, "")},
            )
        ] = attr_selector
        fields[
            vol.Optional(
                CONF_UNIT,
                default=d.get(CONF_UNIT, ""),
            )
        ] = vol.All(str, vol.Length(max=32))
        fields[
            vol.Optional(
                CONF_SCALE,
                default=d.get(CONF_SCALE, DEFAULT_SCALE),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=SCALES,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        fields[
            vol.Optional(
                CONF_DECIMALS,
                default=d.get(CONF_DECIMALS, DEFAULT_DECIMALS),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=0, max=10))
        fields[
            vol.Optional(
                CONF_SMOOTHING,
                default=d.get(CONF_SMOOTHING, False),
            )
        ] = BooleanSelector()
        fields[
            vol.Optional(
                CONF_THRESHOLDS,
                default=d.get(CONF_THRESHOLDS, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))
        fields[
            vol.Optional(
                CONF_HISTORY_PERIOD,
                default=d.get(CONF_HISTORY_PERIOD, DEFAULT_HISTORY_PERIOD),
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=1440,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="minutes",
            )
        )
    if template == "board":
        # Tiles are configured as a structured string (mirrors widget stat_rows).
        # Format: 'label=entity_id[:attribute[:unit[:icon]]]' comma-separated.
        # The anchor entity (step 1) drives start/end; tiles read separate entities.
        fields[
            vol.Required(
                CONF_TILES,
                default=d.get(CONF_TILES, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))
    if template == "log":
        # Optional extra columns composed into each line's text. Freeform string
        # mirroring the board-tile format: '[Label=]source[|unit]' comma-separated,
        # where source is a tracked-entity attribute (brightness), another entity's
        # state (binary_sensor.door), or another entity's attribute (sensor.t:temp).
        fields[
            vol.Optional(
                CONF_LOG_COLUMNS,
                default=d.get(CONF_LOG_COLUMNS, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))
        # Optional attribute on the tracked entity supplying each line's level
        # (info/warn/error); the line text is the formatted state.
        fields[
            vol.Optional(
                CONF_LOG_LEVEL_ATTRIBUTE,
                description={"suggested_value": d.get(CONF_LOG_LEVEL_ATTRIBUTE, "")},
            )
        ] = attr_selector

    # --- Identity fields ---
    fields[vol.Optional(CONF_SLUG, default=d.get(CONF_SLUG, ""))] = vol.All(str, vol.Length(max=MAX_SLUG_LEN))
    fields[
        vol.Optional(
            CONF_ACTIVITY_NAME,
            default=d.get(CONF_ACTIVITY_NAME, ""),
        )
    ] = vol.All(str, vol.Length(max=MAX_TEXT_LEN))
    fields[
        vol.Optional(
            CONF_ICON,
            description={"suggested_value": d.get(CONF_ICON, "")},
        )
    ] = IconSelector(IconSelectorConfig())
    fields[
        vol.Optional(
            CONF_ICON_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_ICON_ATTRIBUTE, "")},
        )
    ] = attr_selector
    fields[
        vol.Optional(
            CONF_PRIORITY,
            default=d.get(CONF_PRIORITY, DEFAULT_PRIORITY),
        )
    ] = vol.All(vol.Coerce(int), vol.Range(min=PRIORITY_MIN, max=PRIORITY_MAX))
    fields[
        vol.Optional(
            CONF_SOUND,
            default=d.get(CONF_SOUND, ""),
        )
    ] = SelectSelector(
        SelectSelectorConfig(
            options=["", *list(SOUNDS)],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )
    fields[
        vol.Optional(
            CONF_UPDATE_INTERVAL,
            default=d.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        )
    ] = vol.All(vol.Coerce(int), vol.Range(min=UPDATE_INTERVAL_MIN))

    # --- Optional fields ---
    fields[_entity_source_key(CONF_SUBTITLE_ENTITY, d)] = entity_selector
    fields[
        vol.Optional(
            CONF_SUBTITLE_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_SUBTITLE_ATTRIBUTE, "")},
        )
    ] = attr_selector
    fields[
        vol.Optional(
            CONF_STATE_LABELS,
            default=d.get(CONF_STATE_LABELS, ""),
        )
    ] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))
    if template == "countdown":
        fields[
            vol.Optional(
                CONF_COMPLETION_MESSAGE,
                default=d.get(CONF_COMPLETION_MESSAGE, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))
        fields[
            vol.Optional(
                CONF_WARNING_THRESHOLD,
                description={"suggested_value": d.get(CONF_WARNING_THRESHOLD)},
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=WARNING_THRESHOLD_MAX,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="seconds",
            )
        )
        fields[
            vol.Optional(
                CONF_ALARM,
                default=d.get(CONF_ALARM, False),
            )
        ] = BooleanSelector()
        fields[
            vol.Optional(
                CONF_SNOOZE_SECONDS,
                description={"suggested_value": d.get(CONF_SNOOZE_SECONDS)},
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=SNOOZE_SECONDS_MIN,
                max=SNOOZE_SECONDS_MAX,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="seconds",
            )
        )
    fields[accent_key] = ColorRGBSelector()
    fields[
        vol.Optional(
            CONF_ACCENT_COLOR_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_ACCENT_COLOR_ATTRIBUTE, "")},
        )
    ] = attr_selector
    fields[bg_color_key] = ColorRGBSelector()
    fields[
        vol.Optional(
            CONF_BACKGROUND_COLOR_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_BACKGROUND_COLOR_ATTRIBUTE, "")},
        )
    ] = attr_selector
    fields[text_color_key] = ColorRGBSelector()
    fields[
        vol.Optional(
            CONF_TEXT_COLOR_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_TEXT_COLOR_ATTRIBUTE, "")},
        )
    ] = attr_selector
    fields[
        vol.Optional(
            CONF_TAP_ACTION_URL,
            default=d.get(CONF_TAP_ACTION_URL, ""),
        )
    ] = vol.All(str, vol.Length(max=MAX_URL_LEN))
    fields[
        vol.Optional(
            CONF_TAP_ACTION_FOREGROUND,
            default=d.get(CONF_TAP_ACTION_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND),
        )
    ] = BooleanSelector()
    if template in ("steps", "alert"):
        fields[
            vol.Optional(
                CONF_URL,
                default=d.get(CONF_URL, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_URL_LEN))
        fields[
            vol.Optional(
                CONF_URL_FOREGROUND,
                default=d.get(CONF_URL_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND),
            )
        ] = BooleanSelector()
        fields[
            vol.Optional(
                CONF_URL_TITLE,
                default=d.get(CONF_URL_TITLE, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_TAP_ACTION_TITLE_LEN))
        fields[
            vol.Optional(
                CONF_SECONDARY_URL,
                default=d.get(CONF_SECONDARY_URL, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_URL_LEN))
        fields[
            vol.Optional(
                CONF_SECONDARY_URL_FOREGROUND,
                default=d.get(CONF_SECONDARY_URL_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND),
            )
        ] = BooleanSelector()
        fields[
            vol.Optional(
                CONF_SECONDARY_URL_TITLE,
                default=d.get(CONF_SECONDARY_URL_TITLE, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_TAP_ACTION_TITLE_LEN))
    fields[ended_ttl_key] = NumberSelector(
        NumberSelectorConfig(
            min=_TTL_MIN,
            max=_TTL_MAX,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="seconds",
        )
    )
    fields[stale_ttl_key] = NumberSelector(
        NumberSelectorConfig(
            min=_TTL_MIN,
            max=_TTL_MAX,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="seconds",
        )
    )

    return vol.Schema(fields)


def _tap_action_url_error(url: str, foreground: bool) -> str | None:
    """Return the error code for an invalid tap-action URL, or None when valid.

    Empty URL is valid (optional field). Foreground=True accepts http(s) or any
    custom scheme not on the security blocklist. Foreground=False (silent webhook)
    requires http(s) — custom schemes are no-ops on iOS without an HTTP shape
    (see pushward-server fa4a98f).
    """
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme:
        return "invalid_url"
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https"):
        return None if parsed.netloc else "invalid_url"
    if scheme in DANGEROUS_URL_SCHEMES:
        return "invalid_url"
    if not foreground:
        return "silent_requires_http"
    return None


def _raise_url_errors(checks: list[tuple[str, str, bool]]) -> None:
    """Validate a batch of (field, url, foreground) tuples. Raises `vol.Invalid`
    with one error code and all matching field paths so the form lights up every
    bad field at once. Different codes are reported one batch at a time, most
    specific first.
    """
    grouped: dict[str, list[str]] = {}
    for field, url, foreground in checks:
        code = _tap_action_url_error(url, foreground)
        if code is None:
            continue
        grouped.setdefault(code, []).append(field)
    if not grouped:
        return
    # silent_requires_http is more specific than invalid_url; surface it first.
    for preferred in ("silent_requires_http", "invalid_url"):
        if preferred in grouped:
            raise vol.Invalid(preferred, path=grouped[preferred])
    # Fall back to whatever code came in (future-proofing for new codes).
    code, fields = next(iter(grouped.items()))
    raise vol.Invalid(code, path=fields)


def _coerce_gauge_range(user_input: dict, *, is_gauge: bool) -> tuple[float, float]:
    """Coerce min/max value pair; raise invalid_gauge_range if min >= max for gauge templates."""
    min_v = float(user_input.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE))
    max_v = float(user_input.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE))
    if is_gauge and min_v >= max_v:
        raise vol.Invalid("invalid_gauge_range", path=[CONF_MIN_VALUE])
    return min_v, max_v


def _parse_board_tiles(raw: object) -> list[dict]:
    """Parse board tiles from a string ('label=entity_id[:attr[:unit[:icon]]], ...') or list.

    Mirrors ``_parse_widget_stat_rows``. Capped at BOARD_MAX_TILES. Each parsed tile
    is ``{label, entity_id, value_attribute?, unit?, icon?}``.
    """
    if isinstance(raw, list):
        tiles = [t for t in raw if isinstance(t, dict) and t.get(CONF_ENTITY_ID) and t.get(CONF_LABEL)]
        return tiles[:BOARD_MAX_TILES]
    if not isinstance(raw, str) or not raw.strip():
        return []
    tiles = []
    for entry in raw.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        label, rest = entry.split("=", 1)
        label = label.strip()
        # maxsplit=3 keeps the icon (4th field) intact even when it contains a
        # colon, e.g. an "mdi:cpu" MDI icon — otherwise the prefix would be lost.
        parts = [p.strip() for p in rest.split(":", 3)]
        if not label or not parts or not parts[0]:
            continue
        tile: dict = {CONF_LABEL: label, CONF_ENTITY_ID: parts[0]}
        if len(parts) > 1 and parts[1]:
            tile[CONF_VALUE_ATTRIBUTE] = parts[1]
        if len(parts) > 2 and parts[2]:
            tile[CONF_UNIT] = parts[2]
        if len(parts) > 3 and parts[3]:
            tile[CONF_ICON] = parts[3]
        tiles.append(tile)
        if len(tiles) >= BOARD_MAX_TILES:
            break
    return tiles


def _serialize_board_tiles(tiles: list[dict]) -> str:
    """Serialize board tiles back to 'label=entity_id[:attr[:unit[:icon]]], ...' for editing."""
    parts: list[str] = []
    for tile in tiles or []:
        label = tile.get(CONF_LABEL, "")
        entity_id = tile.get(CONF_ENTITY_ID, "")
        if not label or not entity_id:
            continue
        s = f"{label}={entity_id}"
        attr = tile.get(CONF_VALUE_ATTRIBUTE) or ""
        unit = tile.get(CONF_UNIT) or ""
        icon = tile.get(CONF_ICON) or ""
        if attr or unit or icon:
            s += f":{attr}"
        if unit or icon:
            s += f":{unit}"
        if icon:
            s += f":{icon}"
        parts.append(s)
    return ", ".join(parts)


def _parse_log_columns(raw: object) -> list[dict]:
    """Parse log columns from a string ('[Label=]source[|unit], ...') or list.

    Mirrors ``_parse_board_tiles``. Capped at LOG_MAX_COLUMNS. ``source`` disambiguates:
      - ``brightness`` (no dot)           → an attribute of the tracked entity
      - ``binary_sensor.door`` (has a dot) → another entity's state
      - ``sensor.temp:temperature``        → another entity's attribute
    ``|`` splits off an optional unit suffix; ``=`` splits off an optional label.
    Each parsed column is ``{label?, entity_id?, attribute?, unit?}``.
    """
    if isinstance(raw, list):
        cols = [c for c in raw if isinstance(c, dict) and (c.get(CONF_ENTITY_ID) or c.get("attribute"))]
        return cols[:LOG_MAX_COLUMNS]
    if not isinstance(raw, str) or not raw.strip():
        return []
    columns: list[dict] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        label = ""
        if "=" in entry:
            label, entry = (part.strip() for part in entry.split("=", 1))
        unit = ""
        if "|" in entry:
            entry, unit = (part.strip() for part in entry.split("|", 1))
        source = entry
        if not source:
            continue
        column: dict = {}
        if label:
            column[CONF_LABEL] = label
        if ":" in source:
            entity_id, attr = (part.strip() for part in source.split(":", 1))
            if not entity_id:
                continue
            column[CONF_ENTITY_ID] = entity_id
            if attr:
                column["attribute"] = attr
        elif "." in source:
            column[CONF_ENTITY_ID] = source
        else:
            column["attribute"] = source
        if unit:
            column[CONF_UNIT] = unit
        columns.append(column)
        if len(columns) >= LOG_MAX_COLUMNS:
            break
    return columns


def _serialize_log_columns(columns: list[dict]) -> str:
    """Serialize log columns back to '[Label=]source[|unit], ...' for editing."""
    parts: list[str] = []
    for column in columns or []:
        if not isinstance(column, dict):
            continue
        entity_id = column.get(CONF_ENTITY_ID) or ""
        attr = column.get("attribute") or ""
        if entity_id and attr:
            source = f"{entity_id}:{attr}"
        elif entity_id:
            source = entity_id
        elif attr:
            source = attr
        else:
            continue
        s = source
        label = column.get(CONF_LABEL) or ""
        if label:
            s = f"{label}={s}"
        unit = column.get(CONF_UNIT) or ""
        if unit:
            s = f"{s}|{unit}"
        parts.append(s)
    return ", ".join(parts)


def _parse_series_entities(raw: object) -> list[dict]:
    """Parse timeline series entities from a string ('[Label=]entity_id[:attribute], ...') or list.

    Mirrors ``_parse_board_tiles``. Each series binds a separate entity as a line:
    the entity's state, or one of its attributes ('entity_id:attribute'). ``source``
    must be an entity_id (contains a dot); a bare word is not a series and is
    skipped. The optional label is left raw here and frozen later by
    ``_resolve_series_entity_labels``. Capped at TIMELINE_MAX_SERIES. Each parsed
    series is ``{label?, entity_id, attribute?}``.
    """
    if isinstance(raw, list):
        series = [s for s in raw if isinstance(s, dict) and s.get(CONF_ENTITY_ID)]
        return series[:TIMELINE_MAX_SERIES]
    if not isinstance(raw, str) or not raw.strip():
        return []
    result: list[dict] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        label = ""
        if "=" in entry:
            label, entry = (part.strip() for part in entry.split("=", 1))
        source = entry
        if not source:
            continue
        series: dict = {}
        if ":" in source:
            entity_id, attr = (part.strip() for part in source.split(":", 1))
            if not entity_id or "." not in entity_id:
                continue
            series[CONF_ENTITY_ID] = entity_id
            if attr:
                series["attribute"] = attr
        elif "." in source:
            series[CONF_ENTITY_ID] = source
        else:
            continue
        if label:
            series[CONF_LABEL] = label
        result.append(series)
        if len(result) >= TIMELINE_MAX_SERIES:
            break
    return result


def _serialize_series_entities(series_entities: list[dict]) -> str:
    """Serialize timeline series entities back to '[Label=]entity_id[:attribute], ...' for editing."""
    parts: list[str] = []
    for series in series_entities or []:
        if not isinstance(series, dict):
            continue
        entity_id = series.get(CONF_ENTITY_ID) or ""
        if not entity_id:
            continue
        source = entity_id
        attr = series.get("attribute") or ""
        if attr:
            source = f"{entity_id}:{attr}"
        label = series.get(CONF_LABEL) or ""
        parts.append(f"{label}={source}" if label else source)
    return ", ".join(parts)


def _entity_friendly_name(hass: HomeAssistant | None, entity_id: str) -> str:
    """Return an entity's friendly name, falling back to the entity_id."""
    if hass is not None:
        state = hass.states.get(entity_id)
        if state is not None:
            name = state.attributes.get("friendly_name")
            if name:
                return str(name)
    return entity_id


def _dedupe_label(label: str, used: set[str]) -> str:
    """Return ``label`` (or ``label 2``/``label 3``/...) not already in ``used``.

    The base is re-truncated to make room for the suffix so the result never
    exceeds TIMELINE_SERIES_LABEL_MAX.
    """
    candidate = label
    n = 1
    while candidate in used:
        n += 1
        suffix = f" {n}"
        candidate = f"{label[: TIMELINE_SERIES_LABEL_MAX - len(suffix)]}{suffix}"
    return candidate


def _resolve_series_entity_labels(series_entities: list[dict], hass: HomeAssistant | None) -> list[dict]:
    """Freeze each timeline series-entity's label at config time.

    A label given in the config is used as-is; an unlabeled series defaults to the
    source entity's friendly name, with the attribute name appended when it reads
    an attribute (so two attributes of one entity don't collide). Labels are
    truncated to TIMELINE_SERIES_LABEL_MAX and de-duplicated with a numeric suffix.
    Freezing matters because the server merges timeline series by label (RFC 7396):
    a render-time friendly-name change would strand the old series as a flat line.
    """
    resolved: list[dict] = []
    used: set[str] = set()
    for series in series_entities:
        entity_id = series.get(CONF_ENTITY_ID)
        if not entity_id:
            continue
        attr = series.get("attribute")
        label = (series.get(CONF_LABEL) or "").strip()
        if not label:
            label = _entity_friendly_name(hass, entity_id)
            if attr:
                label = f"{label} {attr}"
        label = _dedupe_label(label[:TIMELINE_SERIES_LABEL_MAX], used)
        used.add(label)
        out: dict = {CONF_LABEL: label, CONF_ENTITY_ID: entity_id}
        if attr:
            out["attribute"] = attr
        resolved.append(out)
    return resolved


def _parse_entity_input(user_input: dict, hass: HomeAssistant | None = None) -> dict:
    """Normalize user input into an entity config dict."""
    entity_id = user_input[CONF_ENTITY_ID]
    raw_slug = user_input.get(CONF_SLUG, "").strip()
    slug = (normalize_slug(raw_slug) if raw_slug else "") or sanitize_slug(entity_id)

    domain = _entity_domain(entity_id)
    defaults = get_domain_defaults(domain)

    start_raw = user_input.get(CONF_START_STATES, [])
    end_raw = user_input.get(CONF_END_STATES, [])

    # Handle both list (from SelectSelector) and string (legacy fallback)
    if isinstance(start_raw, str):
        start_states = _parse_csv(start_raw)
    elif isinstance(start_raw, list):
        start_states = [s.strip() for s in start_raw if isinstance(s, str) and s.strip()]
    else:
        start_states = []

    if isinstance(end_raw, str):
        end_states = _parse_csv(end_raw)
    elif isinstance(end_raw, list):
        end_states = [s.strip() for s in end_raw if isinstance(s, str) and s.strip()]
    else:
        end_states = []

    # Parse TTLs: NumberSelector returns float, convert to int or None
    ended_ttl = user_input.get(CONF_ENDED_TTL)
    stale_ttl = user_input.get(CONF_STALE_TTL)

    # Validate URLs (allow http/https + custom schemes; silent mode requires http(s))
    tap_action_url = user_input.get(CONF_TAP_ACTION_URL, "").strip()
    tap_action_foreground = bool(user_input.get(CONF_TAP_ACTION_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND))
    url = user_input.get(CONF_URL, "").strip()
    url_foreground = bool(user_input.get(CONF_URL_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND))
    url_title = user_input.get(CONF_URL_TITLE, "").strip()
    secondary_url = user_input.get(CONF_SECONDARY_URL, "").strip()
    secondary_url_foreground = bool(user_input.get(CONF_SECONDARY_URL_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND))
    secondary_url_title = user_input.get(CONF_SECONDARY_URL_TITLE, "").strip()

    _raise_url_errors(
        [
            (CONF_TAP_ACTION_URL, tap_action_url, tap_action_foreground),
            (CONF_URL, url, url_foreground),
            (CONF_SECONDARY_URL, secondary_url, secondary_url_foreground),
        ]
    )

    min_v, max_v = _coerce_gauge_range(user_input, is_gauge=user_input.get(CONF_TEMPLATE) == "gauge")

    # Parse timeline fields
    series_raw = user_input.get(CONF_SERIES, "")
    series = _parse_state_labels(series_raw) if isinstance(series_raw, str) else series_raw or {}
    series_entities = _resolve_series_entity_labels(
        _parse_series_entities(user_input.get(CONF_SERIES_ENTITIES, "")), hass
    )
    if len(series) + len(series_entities) > TIMELINE_MAX_SERIES:
        raise vol.Invalid("too_many_series", path=[CONF_SERIES_ENTITIES])
    thresholds_raw = user_input.get(CONF_THRESHOLDS, "")
    thresholds = _parse_thresholds(thresholds_raw) if isinstance(thresholds_raw, str) else thresholds_raw or []
    history_period_raw = user_input.get(CONF_HISTORY_PERIOD, DEFAULT_HISTORY_PERIOD)

    # Board tiles: a board needs at least one tile to render.
    tiles = _parse_board_tiles(user_input.get(CONF_TILES, ""))
    if user_input.get(CONF_TEMPLATE) == "board" and not tiles:
        raise vol.Invalid("tiles_required", path=[CONF_TILES])

    return {
        CONF_ENTITY_ID: entity_id,
        CONF_SLUG: slug,
        CONF_ACTIVITY_NAME: user_input.get(CONF_ACTIVITY_NAME, "") or entity_id,
        CONF_ICON: user_input.get(CONF_ICON, ""),
        CONF_ICON_ATTRIBUTE: user_input.get(CONF_ICON_ATTRIBUTE, ""),
        CONF_PRIORITY: user_input.get(CONF_PRIORITY, DEFAULT_PRIORITY),
        CONF_TEMPLATE: user_input.get(CONF_TEMPLATE, "generic"),
        CONF_START_STATES: start_states or defaults.get("start_states", []),
        CONF_END_STATES: end_states or defaults.get("end_states", []),
        CONF_UPDATE_INTERVAL: user_input.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        CONF_PROGRESS_ATTRIBUTE: user_input.get(CONF_PROGRESS_ATTRIBUTE, ""),
        CONF_PROGRESS_ENTITY: user_input.get(CONF_PROGRESS_ENTITY, ""),
        CONF_REMAINING_TIME_ATTR: user_input.get(CONF_REMAINING_TIME_ATTR, ""),
        CONF_REMAINING_TIME_ENTITY: user_input.get(CONF_REMAINING_TIME_ENTITY, ""),
        CONF_LIVE_PROGRESS: bool(user_input.get(CONF_LIVE_PROGRESS, False)),
        CONF_SUBTITLE_ATTRIBUTE: user_input.get(CONF_SUBTITLE_ATTRIBUTE, ""),
        CONF_SUBTITLE_ENTITY: user_input.get(CONF_SUBTITLE_ENTITY, ""),
        CONF_STATE_LABELS: _parse_state_labels(user_input.get(CONF_STATE_LABELS, "")),
        CONF_COMPLETION_MESSAGE: user_input.get(CONF_COMPLETION_MESSAGE, ""),
        CONF_TOTAL_STEPS: user_input.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS),
        CONF_CURRENT_STEP_ATTR: user_input.get(CONF_CURRENT_STEP_ATTR, ""),
        CONF_CURRENT_STEP_ENTITY: user_input.get(CONF_CURRENT_STEP_ENTITY, ""),
        CONF_SEVERITY: user_input.get(CONF_SEVERITY, DEFAULT_SEVERITY),
        CONF_SEVERITY_LABEL: user_input.get(CONF_SEVERITY_LABEL, ""),
        CONF_VALUE_ATTRIBUTE: user_input.get(CONF_VALUE_ATTRIBUTE, ""),
        CONF_VALUE_ENTITY: user_input.get(CONF_VALUE_ENTITY, ""),
        CONF_MIN_VALUE: min_v,
        CONF_MAX_VALUE: max_v,
        CONF_UNIT: user_input.get(CONF_UNIT, ""),
        CONF_ACCENT_COLOR: _rgb_to_hex(user_input.get(CONF_ACCENT_COLOR)),
        CONF_ACCENT_COLOR_ATTRIBUTE: user_input.get(CONF_ACCENT_COLOR_ATTRIBUTE, ""),
        CONF_URL: url,
        CONF_URL_FOREGROUND: url_foreground,
        CONF_URL_TITLE: url_title,
        CONF_SECONDARY_URL: secondary_url,
        CONF_SECONDARY_URL_FOREGROUND: secondary_url_foreground,
        CONF_SECONDARY_URL_TITLE: secondary_url_title,
        CONF_TAP_ACTION_URL: tap_action_url,
        CONF_TAP_ACTION_FOREGROUND: tap_action_foreground,
        CONF_ENDED_TTL: int(ended_ttl) if ended_ttl is not None else None,
        CONF_STALE_TTL: int(stale_ttl) if stale_ttl is not None else None,
        CONF_SERIES: series,
        CONF_SERIES_ENTITIES: series_entities,
        CONF_PRIMARY_SERIES: (user_input.get(CONF_PRIMARY_SERIES) or "").strip(),
        CONF_SCALE: user_input.get(CONF_SCALE, DEFAULT_SCALE),
        CONF_DECIMALS: user_input.get(CONF_DECIMALS, DEFAULT_DECIMALS),
        CONF_SMOOTHING: user_input.get(CONF_SMOOTHING, False),
        CONF_THRESHOLDS: thresholds,
        CONF_HISTORY_PERIOD: int(history_period_raw) if history_period_raw is not None else DEFAULT_HISTORY_PERIOD,
        CONF_SOUND: user_input.get(CONF_SOUND, ""),
        CONF_WARNING_THRESHOLD: int(user_input[CONF_WARNING_THRESHOLD])
        if user_input.get(CONF_WARNING_THRESHOLD) is not None
        else None,
        CONF_ALARM: bool(user_input.get(CONF_ALARM, False)),
        CONF_SNOOZE_SECONDS: int(user_input[CONF_SNOOZE_SECONDS])
        if user_input.get(CONF_SNOOZE_SECONDS) is not None
        else None,
        CONF_STEP_LABELS: _parse_state_labels(user_input.get(CONF_STEP_LABELS, "")),
        CONF_STEP_ROWS: _parse_int_list(user_input.get(CONF_STEP_ROWS, "")),
        CONF_FIRED_AT_ATTRIBUTE: user_input.get(CONF_FIRED_AT_ATTRIBUTE, ""),
        CONF_FIRED_AT_ENTITY: user_input.get(CONF_FIRED_AT_ENTITY, ""),
        CONF_UNITS: _parse_state_labels(user_input.get(CONF_UNITS, "")),
        CONF_BACKGROUND_COLOR: _rgb_to_hex(user_input.get(CONF_BACKGROUND_COLOR)),
        CONF_BACKGROUND_COLOR_ATTRIBUTE: user_input.get(CONF_BACKGROUND_COLOR_ATTRIBUTE, ""),
        CONF_TEXT_COLOR: _rgb_to_hex(user_input.get(CONF_TEXT_COLOR)),
        CONF_TEXT_COLOR_ATTRIBUTE: user_input.get(CONF_TEXT_COLOR_ATTRIBUTE, ""),
        CONF_TILES: tiles,
        CONF_LOG_LEVEL_ATTRIBUTE: user_input.get(CONF_LOG_LEVEL_ATTRIBUTE, ""),
        CONF_LOG_COLUMNS: _parse_log_columns(user_input.get(CONF_LOG_COLUMNS, "")),
    }


class PushWardConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial PushWard configuration."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the initial setup step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await _validate_integration_key(self.hass, user_input[CONF_INTEGRATION_KEY], "setup")
            if not errors:
                return self.async_create_entry(
                    title="PushWard",
                    data={
                        CONF_SERVER_URL: DEFAULT_SERVER_URL,
                        CONF_INTEGRATION_KEY: user_input[CONF_INTEGRATION_KEY],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_INTEGRATION_KEY_SCHEMA,
            errors=errors,
            description_placeholders={"app_store_url": APP_STORE_URL},
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle reconfiguration of the integration key."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await _validate_integration_key(self.hass, user_input[CONF_INTEGRATION_KEY], "reconfigure")
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        CONF_SERVER_URL: DEFAULT_SERVER_URL,
                        CONF_INTEGRATION_KEY: user_input[CONF_INTEGRATION_KEY],
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_INTEGRATION_KEY_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> config_entries.ConfigFlowResult:
        """Handle reauth when the integration key becomes invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask user for a new integration key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            entry = self._get_reauth_entry()
            server_url = entry.data[CONF_SERVER_URL]
            errors = await _validate_integration_key(
                self.hass, user_input[CONF_INTEGRATION_KEY], "reauth", server_url=server_url
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_INTEGRATION_KEY: user_input[CONF_INTEGRATION_KEY],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_INTEGRATION_KEY_SCHEMA,
            errors=errors,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Return supported subentry types."""
        return {
            SUBENTRY_TYPE_ENTITY: PushWardEntitySubentryFlow,
            SUBENTRY_TYPE_WIDGET: PushWardWidgetSubentryFlow,
        }


class PushWardEntitySubentryFlow(config_entries.ConfigSubentryFlow):
    """Handle adding and reconfiguring tracked entities (two-step flow)."""

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()
        self._step1_input: dict[str, Any] = {}
        self._is_reconfigure: bool = False
        self._details_defaults: dict[str, Any] = {}
        self._suggestion_offered: bool = False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.SubentryFlowResult:
        """Step 1: Entity + template."""
        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID]
            template = user_input.get(CONF_TEMPLATE, "generic")

            # Suggest a better template if the user left the default
            if template == "generic" and not self._suggestion_offered:
                suggested = _suggest_template(self.hass, entity_id)
                if suggested != "generic":
                    self._suggestion_offered = True
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_entity_template_schema(
                            defaults={CONF_ENTITY_ID: entity_id, CONF_TEMPLATE: suggested}
                        ),
                    )

            self._step1_input = user_input
            self._is_reconfigure = False
            return await self.async_step_details()

        return self.async_show_form(
            step_id="user",
            data_schema=_entity_template_schema(),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Step 1 (reconfigure): Entity + template with pre-filled values."""
        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            self._step1_input = user_input
            self._is_reconfigure = True
            # Prepare defaults for step 2 from existing config
            current = dict(subentry.data)
            labels = current.get(CONF_STATE_LABELS)
            if isinstance(labels, dict):
                current[CONF_STATE_LABELS] = _serialize_key_value_pairs(labels)
            series = current.get(CONF_SERIES)
            if isinstance(series, dict):
                current[CONF_SERIES] = _serialize_key_value_pairs(series)
            series_entities = current.get(CONF_SERIES_ENTITIES)
            if isinstance(series_entities, list):
                current[CONF_SERIES_ENTITIES] = _serialize_series_entities(series_entities)
            thresholds = current.get(CONF_THRESHOLDS)
            if isinstance(thresholds, list):
                current[CONF_THRESHOLDS] = _serialize_thresholds(thresholds)
            step_labels = current.get(CONF_STEP_LABELS)
            if isinstance(step_labels, dict):
                current[CONF_STEP_LABELS] = _serialize_key_value_pairs(step_labels)
            step_rows = current.get(CONF_STEP_ROWS)
            if isinstance(step_rows, list):
                current[CONF_STEP_ROWS] = _serialize_int_list(step_rows)
            units = current.get(CONF_UNITS)
            if isinstance(units, dict):
                current[CONF_UNITS] = _serialize_key_value_pairs(units)
            tiles = current.get(CONF_TILES)
            if isinstance(tiles, list):
                current[CONF_TILES] = _serialize_board_tiles(tiles)
            log_columns = current.get(CONF_LOG_COLUMNS)
            if isinstance(log_columns, list):
                current[CONF_LOG_COLUMNS] = _serialize_log_columns(log_columns)
            self._details_defaults = current
            return await self.async_step_details()

        current = dict(subentry.data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_entity_template_schema(defaults=current),
        )

    async def async_step_details(self, user_input: dict[str, Any] | None = None) -> config_entries.SubentryFlowResult:
        """Step 2: All configuration details with dynamic selectors."""
        entity_id = self._step1_input.get(CONF_ENTITY_ID, "")
        template = self._step1_input.get(CONF_TEMPLATE, "generic")
        errors: dict[str, str] = {}

        if user_input is not None:
            merged = {**self._step1_input, **user_input}
            try:
                entity_cfg = _parse_entity_input(merged, hass=self.hass)
            except vol.Invalid as exc:
                for field in exc.path:
                    errors[field] = str(exc.msg)
            else:
                if self._is_reconfigure:
                    entry = self._get_entry()
                    subentry = self._get_reconfigure_subentry()
                    return self.async_update_and_abort(
                        entry,
                        subentry,
                        data=entity_cfg,
                        title=entity_cfg[CONF_ACTIVITY_NAME],
                    )
                return self.async_create_entry(
                    title=entity_cfg[CONF_ACTIVITY_NAME],
                    data=entity_cfg,
                    unique_id=entity_cfg[CONF_ENTITY_ID],
                )

        defaults = self._details_defaults if self._is_reconfigure else None
        return self.async_show_form(
            step_id="details",
            data_schema=_details_schema(entity_id, template, defaults=defaults, hass=self.hass),
            errors=errors,
        )


# --- Widget subentry flow ---


def _widget_step1_schema(defaults: dict | None = None) -> vol.Schema:
    """Step-1 schema: entity picker + template + (optional) slug override."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_ENTITY_ID,
                default=d.get(CONF_ENTITY_ID, ""),
            ): EntitySelector(EntitySelectorConfig()),
            vol.Required(
                CONF_WIDGET_TEMPLATE,
                default=d.get(CONF_WIDGET_TEMPLATE, WIDGET_TEMPLATE_VALUE),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=WIDGET_TEMPLATES,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_SLUG,
                default=d.get(CONF_SLUG, ""),
            ): vol.All(str, vol.Length(max=MAX_SLUG_LEN)),
        }
    )


def _widget_details_schema(
    entity_id: str,
    template: str,
    defaults: dict | None = None,
) -> vol.Schema:
    """Step-2 schema: template-specific fields + cosmetics + trigger mode."""
    d = defaults or {}

    attr_selector = AttributeSelector(AttributeSelectorConfig(entity_id=entity_id))

    accent_key = _color_vol_key(CONF_ACCENT_COLOR, d)
    bg_color_key = _color_vol_key(CONF_BACKGROUND_COLOR, d)
    text_color_key = _color_vol_key(CONF_TEXT_COLOR, d)

    fields: dict = {}

    fields[
        vol.Optional(
            CONF_WIDGET_NAME,
            default=d.get(CONF_WIDGET_NAME, ""),
        )
    ] = vol.All(str, vol.Length(max=WIDGET_NAME_MAX))

    # Template-specific
    if template in (WIDGET_TEMPLATE_VALUE, WIDGET_TEMPLATE_PROGRESS, WIDGET_TEMPLATE_GAUGE):
        fields[
            vol.Optional(
                CONF_VALUE_ATTRIBUTE,
                description={"suggested_value": d.get(CONF_VALUE_ATTRIBUTE, "")},
            )
        ] = attr_selector
        fields[
            vol.Optional(
                CONF_UNIT,
                default=d.get(CONF_UNIT, ""),
            )
        ] = vol.All(str, vol.Length(max=WIDGET_UNIT_MAX))

    if template == WIDGET_TEMPLATE_GAUGE:
        fields[
            vol.Required(
                CONF_MIN_VALUE,
                default=d.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE),
            )
        ] = vol.Coerce(float)
        fields[
            vol.Required(
                CONF_MAX_VALUE,
                default=d.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE),
            )
        ] = vol.Coerce(float)

    if template == WIDGET_TEMPLATE_STATUS:
        fields[
            vol.Optional(
                CONF_SEVERITY,
                default=d.get(CONF_SEVERITY, ""),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=WIDGET_SEVERITIES,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

    if template == WIDGET_TEMPLATE_STAT_LIST:
        # stat_rows are configured as a structured string until HA gains a
        # repeating-row selector. Format: 'label=entity_id[:attribute[:unit]]'
        # comma-separated. Capped by WIDGET_MAX_STAT_ROWS.
        fields[
            vol.Required(
                CONF_STAT_ROWS,
                default=d.get(CONF_STAT_ROWS, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))

    # Cosmetic fields (all templates)
    fields[
        vol.Optional(
            CONF_LABEL,
            default=d.get(CONF_LABEL, ""),
        )
    ] = vol.All(str, vol.Length(max=WIDGET_LABEL_MAX))
    fields[
        vol.Optional(
            CONF_LABEL_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_LABEL_ATTRIBUTE, "")},
        )
    ] = attr_selector
    fields[
        vol.Optional(
            CONF_SUBTITLE_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_SUBTITLE_ATTRIBUTE, "")},
        )
    ] = attr_selector
    fields[
        vol.Optional(
            CONF_ICON,
            description={"suggested_value": d.get(CONF_ICON, "")},
        )
    ] = IconSelector(IconSelectorConfig())
    fields[
        vol.Optional(
            CONF_ICON_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_ICON_ATTRIBUTE, "")},
        )
    ] = attr_selector
    fields[accent_key] = ColorRGBSelector()
    fields[
        vol.Optional(
            CONF_ACCENT_COLOR_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_ACCENT_COLOR_ATTRIBUTE, "")},
        )
    ] = attr_selector
    fields[bg_color_key] = ColorRGBSelector()
    fields[text_color_key] = ColorRGBSelector()

    # Widget-wide tap action (universal across all templates)
    fields[
        vol.Optional(
            CONF_TAP_ACTION_URL,
            default=d.get(CONF_TAP_ACTION_URL, ""),
        )
    ] = vol.All(str, vol.Length(max=MAX_URL_LEN))
    fields[
        vol.Optional(
            CONF_TAP_ACTION_FOREGROUND,
            default=d.get(CONF_TAP_ACTION_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND),
        )
    ] = BooleanSelector()

    # Trigger mode + interval
    fields[
        vol.Required(
            CONF_WIDGET_TRIGGER_MODE,
            default=d.get(CONF_WIDGET_TRIGGER_MODE, WIDGET_TRIGGER_EVENT),
        )
    ] = SelectSelector(
        SelectSelectorConfig(
            options=WIDGET_TRIGGER_MODES,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )
    fields[
        vol.Optional(
            CONF_WIDGET_POLL_INTERVAL,
            default=d.get(CONF_WIDGET_POLL_INTERVAL, DEFAULT_WIDGET_POLL_INTERVAL),
        )
    ] = NumberSelector(
        NumberSelectorConfig(
            min=WIDGET_POLL_INTERVAL_MIN,
            max=WIDGET_POLL_INTERVAL_MAX,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="seconds",
        )
    )

    return vol.Schema(fields)


def _parse_widget_stat_rows(raw: object) -> list[dict]:
    """Parse stat_rows from string ('label=entity_id[:attr[:unit]], ...') or list."""
    if isinstance(raw, list):
        rows = [row for row in raw if isinstance(row, dict) and row.get(CONF_ENTITY_ID) and row.get(CONF_LABEL)]
        return rows[:WIDGET_MAX_STAT_ROWS]
    if not isinstance(raw, str) or not raw.strip():
        return []
    rows: list[dict] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        label, rest = entry.split("=", 1)
        label = label.strip()
        parts = [p.strip() for p in rest.split(":")]
        if not label or not parts or not parts[0]:
            continue
        row: dict = {CONF_LABEL: label, CONF_ENTITY_ID: parts[0]}
        if len(parts) > 1 and parts[1]:
            row[CONF_VALUE_ATTRIBUTE] = parts[1]
        if len(parts) > 2 and parts[2]:
            row[CONF_UNIT] = parts[2]
        rows.append(row)
        if len(rows) >= WIDGET_MAX_STAT_ROWS:
            break
    return rows


def _serialize_widget_stat_rows(rows: list[dict]) -> str:
    parts: list[str] = []
    for row in rows or []:
        label = row.get(CONF_LABEL, "")
        entity_id = row.get(CONF_ENTITY_ID, "")
        if not label or not entity_id:
            continue
        s = f"{label}={entity_id}"
        attr = row.get(CONF_VALUE_ATTRIBUTE) or ""
        unit = row.get(CONF_UNIT) or ""
        if attr or unit:
            s += f":{attr}"
        if unit:
            s += f":{unit}"
        parts.append(s)
    return ", ".join(parts)


def _parse_widget_input(user_input: dict, step1: dict) -> dict:
    """Build the persisted subentry data from step-1 + step-2 inputs."""
    entity_id = step1[CONF_ENTITY_ID]
    template = step1[CONF_WIDGET_TEMPLATE]
    raw_slug = (step1.get(CONF_SLUG) or "").strip()
    slug = (normalize_slug(raw_slug) if raw_slug else "") or sanitize_slug(entity_id)

    min_v, max_v = _coerce_gauge_range(user_input, is_gauge=template == WIDGET_TEMPLATE_GAUGE)

    stat_rows = _parse_widget_stat_rows(user_input.get(CONF_STAT_ROWS, ""))
    if template == WIDGET_TEMPLATE_STAT_LIST and not stat_rows:
        raise vol.Invalid("stat_rows_required", path=[CONF_STAT_ROWS])

    poll_interval = int(user_input.get(CONF_WIDGET_POLL_INTERVAL, DEFAULT_WIDGET_POLL_INTERVAL))
    poll_interval = max(WIDGET_POLL_INTERVAL_MIN, min(WIDGET_POLL_INTERVAL_MAX, poll_interval))

    trigger = user_input.get(CONF_WIDGET_TRIGGER_MODE) or WIDGET_TRIGGER_EVENT
    if trigger not in WIDGET_TRIGGER_MODES:
        trigger = WIDGET_TRIGGER_EVENT

    tap_action_url = (user_input.get(CONF_TAP_ACTION_URL) or "").strip()
    tap_action_foreground = bool(user_input.get(CONF_TAP_ACTION_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND))
    _raise_url_errors([(CONF_TAP_ACTION_URL, tap_action_url, tap_action_foreground)])

    return {
        CONF_ENTITY_ID: entity_id,
        CONF_SLUG: slug,
        CONF_WIDGET_NAME: user_input.get(CONF_WIDGET_NAME, "") or "",
        CONF_WIDGET_TEMPLATE: template,
        CONF_WIDGET_TRIGGER_MODE: trigger,
        CONF_WIDGET_POLL_INTERVAL: poll_interval,
        CONF_VALUE_ATTRIBUTE: user_input.get(CONF_VALUE_ATTRIBUTE, "") or "",
        CONF_UNIT: user_input.get(CONF_UNIT, "") or "",
        CONF_MIN_VALUE: min_v,
        CONF_MAX_VALUE: max_v,
        CONF_SEVERITY: user_input.get(CONF_SEVERITY, "") or "",
        CONF_STAT_ROWS: stat_rows,
        CONF_LABEL: user_input.get(CONF_LABEL, "") or "",
        CONF_LABEL_ATTRIBUTE: user_input.get(CONF_LABEL_ATTRIBUTE, "") or "",
        CONF_SUBTITLE_ATTRIBUTE: user_input.get(CONF_SUBTITLE_ATTRIBUTE, "") or "",
        CONF_ICON: user_input.get(CONF_ICON, "") or "",
        CONF_ICON_ATTRIBUTE: user_input.get(CONF_ICON_ATTRIBUTE, "") or "",
        CONF_ACCENT_COLOR: _rgb_to_hex(user_input.get(CONF_ACCENT_COLOR)),
        CONF_ACCENT_COLOR_ATTRIBUTE: user_input.get(CONF_ACCENT_COLOR_ATTRIBUTE, "") or "",
        CONF_BACKGROUND_COLOR: _rgb_to_hex(user_input.get(CONF_BACKGROUND_COLOR)),
        CONF_TEXT_COLOR: _rgb_to_hex(user_input.get(CONF_TEXT_COLOR)),
        CONF_TAP_ACTION_URL: tap_action_url,
        CONF_TAP_ACTION_FOREGROUND: tap_action_foreground,
    }


class PushWardWidgetSubentryFlow(config_entries.ConfigSubentryFlow):
    """Two-step flow for adding / reconfiguring a tracked widget."""

    def __init__(self) -> None:
        super().__init__()
        self._step1_input: dict[str, Any] = {}
        self._is_reconfigure: bool = False
        self._details_defaults: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.SubentryFlowResult:
        """Step 1: entity + template + slug."""
        if user_input is not None:
            self._step1_input = user_input
            self._is_reconfigure = False
            return await self.async_step_details()
        return self.async_show_form(
            step_id="user",
            data_schema=_widget_step1_schema(),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            self._step1_input = user_input
            self._is_reconfigure = True
            current = dict(subentry.data)
            stat_rows = current.get(CONF_STAT_ROWS)
            if isinstance(stat_rows, list):
                current[CONF_STAT_ROWS] = _serialize_widget_stat_rows(stat_rows)
            self._details_defaults = current
            return await self.async_step_details()
        current = dict(subentry.data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_widget_step1_schema(defaults=current),
        )

    async def async_step_details(self, user_input: dict[str, Any] | None = None) -> config_entries.SubentryFlowResult:
        entity_id = self._step1_input.get(CONF_ENTITY_ID, "")
        template = self._step1_input.get(CONF_WIDGET_TEMPLATE, WIDGET_TEMPLATE_VALUE)
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                widget_cfg = _parse_widget_input(user_input, self._step1_input)
            except vol.Invalid as exc:
                for field in exc.path:
                    errors[field] = str(exc.msg)
            else:
                title = widget_cfg.get(CONF_WIDGET_NAME) or widget_cfg[CONF_SLUG]
                if self._is_reconfigure:
                    entry = self._get_entry()
                    subentry = self._get_reconfigure_subentry()
                    return self.async_update_and_abort(
                        entry,
                        subentry,
                        data=widget_cfg,
                        title=title,
                    )
                return self.async_create_entry(
                    title=title,
                    data=widget_cfg,
                    unique_id=widget_cfg[CONF_SLUG],
                )

        defaults = self._details_defaults if self._is_reconfigure else None
        return self.async_show_form(
            step_id="details",
            data_schema=_widget_details_schema(entity_id, template, defaults=defaults),
            errors=errors,
        )


def _rgb_to_hex(rgb: list[int] | None) -> str:
    """Convert an [R, G, B] list to a '#rrggbb' hex string."""
    if not rgb or not isinstance(rgb, list) or len(rgb) != 3:
        return ""
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _hex_to_rgb(hex_color: str) -> list[int] | None:
    """Convert a '#rrggbb' hex string back to [R, G, B] for the color picker."""
    if not hex_color or not hex_color.startswith("#") or len(hex_color) != 7:
        return None
    try:
        return [int(hex_color[i : i + 2], 16) for i in (1, 3, 5)]
    except ValueError:
        return None


def _color_vol_key(conf_key: str, current: dict) -> vol.Optional:
    """Build a vol.Optional key for a ColorRGBSelector, omitting the default if no valid hex is stored."""
    rgb = _hex_to_rgb(current.get(conf_key, ""))
    return vol.Optional(conf_key, default=rgb) if rgb is not None else vol.Optional(conf_key)


def _entity_source_key(conf_key: str, current: dict) -> vol.Optional:
    """Build a vol.Optional key for a companion-entity EntitySelector.

    Pre-fills the saved entity_id when present; otherwise leaves the field empty
    so an unset companion submits as absent rather than an invalid empty entity.
    """
    saved = current.get(conf_key)
    if saved:
        return vol.Optional(conf_key, description={"suggested_value": saved})
    return vol.Optional(conf_key)


def _parse_csv(value: str) -> list[str]:
    """Parse a comma-separated string into a list of stripped, non-empty items."""
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def _parse_int_list(value: str) -> list[int]:
    """Parse '1,2,3' into [1, 2, 3], silently skipping non-integer tokens."""
    result: list[int] = []
    for token in _parse_csv(value):
        with contextlib.suppress(ValueError):
            result.append(int(token))
    return result


def _serialize_int_list(values: list[int]) -> str:
    """Serialize a list of ints to '1, 2, 3' text for UI editing."""
    return ", ".join(str(v) for v in values) if values else ""


def _parse_state_labels(value: str) -> dict[str, str]:
    """Parse 'key=value, key2=value2' into a dict."""
    if not value:
        return {}
    result: dict[str, str] = {}
    for pair in value.split(","):
        pair = pair.strip()
        if "=" in pair:
            key, val = pair.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key and val:
                result[key] = val
    return result


def _serialize_key_value_pairs(pairs: dict[str, str]) -> str:
    """Serialize a dict to 'key=value, key2=value2' text for UI editing."""
    if not pairs:
        return ""
    return ", ".join(f"{k}={v}" for k, v in pairs.items())


def _parse_thresholds(value: str) -> list[dict]:
    """Parse 'value:color:label, ...' into threshold dicts.

    Format: value[:color[:label]], ...
    Example: '25:red:Hot, 18:blue:Cold, 20'
    """
    if not value:
        return []
    result: list[dict] = []
    for entry in value.split(","):
        parts = [p.strip() for p in entry.strip().split(":")]
        if not parts or not parts[0]:
            continue
        try:
            threshold: dict = {"value": float(parts[0])}
        except ValueError:
            continue
        if len(parts) > 1 and parts[1]:
            threshold["color"] = parts[1]
        if len(parts) > 2 and parts[2]:
            threshold["label"] = parts[2]
        result.append(threshold)
    return result[:5]


def _serialize_thresholds(thresholds: list[dict]) -> str:
    """Serialize threshold dicts back to 'value:color:label, ...' text for editing."""
    if not thresholds:
        return ""
    parts: list[str] = []
    for t in thresholds:
        s = str(t.get("value", ""))
        color = t.get("color", "")
        label = t.get("label", "")
        if color or label:
            s += f":{color}"
        if label:
            s += f":{label}"
        parts.append(s)
    return ", ".join(parts)
