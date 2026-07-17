"""Config flow and subentry flow for PushWard integration."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
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
    ObjectSelector,
    ObjectSelectorConfig,
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
    BOARD_TILE_ICON_MAX,
    BOARD_TILE_LABEL_MAX,
    BOARD_TILE_UNIT_MAX,
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
    CONF_DISMISSAL_TTL,
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
    CONF_STEP_COLORS,
    CONF_STEP_LABELS,
    CONF_STEP_ROWS,
    CONF_STEP_WEIGHTS,
    CONF_STEPS_EDITOR,
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
    CONF_VALUE_SCALE,
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
    DEFAULT_VALUE_SCALE,
    DEFAULT_WIDGET_POLL_INTERVAL,
    DISMISSAL_TTL_MAX,
    DISMISSAL_TTL_MIN,
    DOMAIN,
    LIVE_PROGRESS_TEMPLATES,
    LOG_COLUMN_LABEL_MAX,
    LOG_MAX_COLUMNS,
    MAX_LONG_TEXT_LEN,
    MAX_SEVERITY_LABEL_LEN,
    MAX_SLUG_LEN,
    MAX_TAP_ACTION_TITLE_LEN,
    MAX_TEXT_LEN,
    MAX_URL_LEN,
    NAMED_COLORS,
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
    THRESHOLD_LABEL_MAX,
    THRESHOLDS_MAX,
    TIMELINE_MAX_SERIES,
    TIMELINE_SERIES_LABEL_MAX,
    TOTAL_STEPS_MAX,
    UPDATE_INTERVAL_MIN,
    VALUE_SCALES,
    WARNING_THRESHOLD_MAX,
    WIDGET_LABEL_MAX,
    WIDGET_MAX_STAT_ROWS,
    WIDGET_NAME_MAX,
    WIDGET_POLL_INTERVAL_MAX,
    WIDGET_POLL_INTERVAL_MIN,
    WIDGET_SEVERITIES,
    WIDGET_STAT_LABEL_MAX,
    WIDGET_STAT_UNIT_MAX,
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
from .content_mapper import get_domain_defaults, is_valid_color, sanitize_slug

_LOGGER = logging.getLogger(__name__)

_INTEGRATION_KEY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_INTEGRATION_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }
)

_TTL_MIN = 1
_TTL_MAX = 2592000  # 30 days

# translation_key -> the ordered wire values each SelectSelector renders. The
# `selector.<key>.options` block in every translations/<lang>.json must carry a
# label for every value here (guarded by test_config_flow_translations). Keep the
# keys in sync with the translation_key= passed on each SelectSelectorConfig below.
SELECT_TRANSLATION_KEYS: dict[str, tuple[str, ...]] = {
    "activity_template": tuple(TEMPLATES),
    "severity": tuple(SEVERITIES),
    "timeline_scale": tuple(SCALES),
    "sound": ("", *SOUNDS),
    "widget_template": tuple(WIDGET_TEMPLATES),
    "value_scale": tuple(VALUE_SCALES),
    "widget_severity": tuple(WIDGET_SEVERITIES),
    "widget_trigger_mode": tuple(WIDGET_TRIGGER_MODES),
    # Nested inside the board-tile ObjectSelector (per-tile color); custom_value
    # also accepts a hex string. Reused by the timeline thresholds editor later.
    "named_color": tuple(NAMED_COLORS),
}

# Object-selector row editors for the two Required structured fields (board tiles,
# widget stat_rows). Field keys equal the stored dict keys so a stored list feeds
# straight back as the editor's value on reconfigure - no serialize/parse round
# trip. The sub-field selectors are dict configs (not Selector instances): the
# ObjectSelector re-wraps each through the selector() factory when it validates a
# submitted row.
#
# That re-wrap matters: on HA >= 2025.8 ObjectSelector.__call__ is NOT a
# passthrough - it runs every present sub-field through its own selector and
# rejects a bad value with a raw, untranslated message BEFORE our step handler
# runs (FlowManager validates data_schema first). So entity_id is a plain text
# field, NOT {"entity": {}}: the entity selector rejects an empty string with
# "Entity  is neither a valid entity ID..." - exactly the partial-row / YAML
# fallback case - which would surface an untranslated error on the field. A text
# field passes the value straight through, keeping requiredness and the
# length/color/url caps in the parse layer (invalid_tile / invalid_stat_row) with
# full locale parity. An unknown top-level key still trips the selector's own
# "Field X is not allowed", but the form only ever submits the keys defined here.
_ROW_TEXT = {"text": {}}
_ROW_ICON = {"icon": {}}
_ROW_URL = {"text": {"type": "url"}}
_ROW_NAMED_COLOR = {
    "select": {
        "options": list(NAMED_COLORS),
        "custom_value": True,
        "mode": "dropdown",
        "translation_key": "named_color",
    }
}

_BOARD_TILES_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_LABEL: {"label": "Label", "selector": _ROW_TEXT},
            CONF_ENTITY_ID: {"label": "Entity", "selector": _ROW_TEXT},
            CONF_VALUE_ATTRIBUTE: {"label": "Attribute", "selector": _ROW_TEXT},
            CONF_UNIT: {"label": "Unit", "selector": _ROW_TEXT},
            CONF_ICON: {"label": "Icon", "selector": _ROW_ICON},
            "color": {"label": "Color", "selector": _ROW_NAMED_COLOR},
            "url_action": {"label": "URL", "selector": _ROW_URL},
        },
        multiple=True,
        label_field=CONF_LABEL,
        description_field=CONF_ENTITY_ID,
    )
)

_WIDGET_STAT_ROWS_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_LABEL: {"label": "Label", "selector": _ROW_TEXT},
            CONF_ENTITY_ID: {"label": "Entity", "selector": _ROW_TEXT},
            CONF_VALUE_ATTRIBUTE: {"label": "Attribute", "selector": _ROW_TEXT},
            CONF_UNIT: {"label": "Unit", "selector": _ROW_TEXT},
        },
        multiple=True,
        label_field=CONF_LABEL,
        description_field=CONF_ENTITY_ID,
    )
)

# Timeline series entities (each row binds a separate entity as a named line). Same
# text-field-for-entity rationale as the board tiles above: an empty entity string
# reaches the parse layer as our translated invalid_series_entity instead of a raw
# schema-side rejection. The stored dicts use the bare "attribute" key (not
# CONF_VALUE_ATTRIBUTE), matching _parse_series_entities / content_mapper.
_ROW_NUMBER_ANY = {"number": {"mode": "box", "step": "any"}}

_SERIES_ENTITIES_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_LABEL: {"label": "Label", "selector": _ROW_TEXT},
            CONF_ENTITY_ID: {"label": "Entity", "selector": _ROW_TEXT},
            "attribute": {"label": "Attribute", "selector": _ROW_TEXT},
        },
        multiple=True,
        label_field=CONF_LABEL,
        description_field=CONF_ENTITY_ID,
    )
)

# Timeline thresholds: a numeric value (number box so the frontend shows a numeric
# field), an optional named/hex color (the same custom_value select as the tile
# color), and an optional short label. A row with no value key is skipped by the
# ObjectSelector and rejected as invalid_threshold in the parse layer.
_THRESHOLDS_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            "value": {"label": "Value", "selector": _ROW_NUMBER_ANY},
            "color": {"label": "Color", "selector": _ROW_NAMED_COLOR},
            "label": {"label": "Label", "selector": _ROW_TEXT},
        },
        multiple=True,
        label_field="label",
        description_field="value",
    )
)

# Log columns: label, an optional entity, an optional attribute, and an optional
# unit. Entity is a text field (leaving it empty is the common bare-attribute case,
# which an entity selector would reject with a raw untranslated error). Source
# semantics are governed by the parse layer: empty entity + attribute = a tracked-
# entity attribute; entity + empty attribute = another entity's state; both = that
# entity's attribute.
_LOG_COLUMNS_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_LABEL: {"label": "Label", "selector": _ROW_TEXT},
            CONF_ENTITY_ID: {"label": "Entity", "selector": _ROW_TEXT},
            "attribute": {"label": "Attribute", "selector": _ROW_TEXT},
            CONF_UNIT: {"label": "Unit", "selector": _ROW_TEXT},
        },
        multiple=True,
        label_field=CONF_LABEL,
        description_field=CONF_ENTITY_ID,
    )
)

# Two-column key/value map editors. Storage stays the existing {key: value} dicts;
# small lossless adapters (_kv_rows_to_map / _map_to_kv_rows) sit between these rows
# and the stored dict, and _kv_rows_to_map still accepts a legacy 'k=v, k2=v2'
# string so stored-string edge cases and non-form callers keep working.
_STATE_LABELS_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            "state": {"label": "State", "selector": _ROW_TEXT},
            CONF_LABEL: {"label": "Label", "selector": _ROW_TEXT},
        },
        multiple=True,
        label_field="state",
        description_field=CONF_LABEL,
    )
)

# Timeline series attribute map: each row maps an attribute of the tracked entity to
# a series label (stored as {attribute: label}).
_SERIES_MAP_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            "attribute": {"label": "Attribute", "selector": _ROW_TEXT},
            CONF_LABEL: {"label": "Label", "selector": _ROW_TEXT},
        },
        multiple=True,
        label_field="attribute",
        description_field=CONF_LABEL,
    )
)

# Timeline per-series units: each row maps a series label to its unit (stored as
# {series_label: unit}).
_UNITS_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            "series": {"label": "Series", "selector": _ROW_TEXT},
            CONF_UNIT: {"label": "Unit", "selector": _ROW_TEXT},
        },
        multiple=True,
        label_field="series",
        description_field=CONF_UNIT,
    )
)

# Unified steps editor: one row per step in step order. A form-only field
# (CONF_STEPS_EDITOR) decomposed in _parse_entity_input into the four stored step
# keys, so the storage shape and content_mapper are untouched. Row height is a 1-10
# number; weight is any positive number; color is a named/hex value (blank leaves
# that step on the accent color). The row count must be 0 or exactly total_steps.
_ROW_STEP_NUMBER = {"number": {"min": 1, "max": 10, "mode": "box"}}

_STEPS_EDITOR_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_LABEL: {"label": "Label", "selector": _ROW_TEXT},
            "row": {"label": "Row", "selector": _ROW_STEP_NUMBER},
            "weight": {"label": "Weight", "selector": _ROW_NUMBER_ANY},
            "color": {"label": "Color", "selector": _ROW_NAMED_COLOR},
        },
        multiple=True,
        label_field=CONF_LABEL,
        description_field="weight",
    )
)


def _object_rows_key(conf_key: str, current: dict, *, required: bool) -> vol.Marker:
    """Build the vol key for an ObjectSelector row list.

    The stored list becomes the field default so it rehydrates verbatim on
    reconfigure (and satisfies the Required marker); an add flow starts empty.
    """
    stored = current.get(conf_key)
    default = stored if isinstance(stored, list) else []
    marker = vol.Required if required else vol.Optional
    return marker(conf_key, default=default)


# Collapsible-section layout for the two subentry detail forms. Each field a
# template renders lands in exactly one section here or stays top-level (the
# tables below); the schema builders filter these per template and drop a section
# that has no field for the current template. Translations live under
# step.details.sections.<key>.data[_description], with sections.<key>.name for the
# header. A field key can be top-level for one template and sectioned for another
# (remaining_time on countdown vs generic/steps) - its label then needs an entry
# in BOTH step.details.data and the section's data block.
ENTITY_SECTIONS: dict[str, tuple[str, ...]] = {
    "data_sources": (
        CONF_PROGRESS_ENTITY,
        CONF_PROGRESS_ATTRIBUTE,
        CONF_REMAINING_TIME_ENTITY,
        CONF_REMAINING_TIME_ATTR,
        CONF_LIVE_PROGRESS,
        CONF_VALUE_ENTITY,
        CONF_VALUE_ATTRIBUTE,
        CONF_FIRED_AT_ENTITY,
        CONF_FIRED_AT_ATTRIBUTE,
        CONF_SUBTITLE_ENTITY,
        CONF_SUBTITLE_ATTRIBUTE,
        CONF_SERIES,
        CONF_LOG_LEVEL_ATTRIBUTE,
    ),
    "steps_options": (CONF_STEPS_EDITOR,),
    "timeline_options": (
        CONF_UNITS,
        CONF_PRIMARY_SERIES,
        CONF_SCALE,
        CONF_DECIMALS,
        CONF_SMOOTHING,
        CONF_THRESHOLDS,
        CONF_HISTORY_PERIOD,
    ),
    "display_options": (
        CONF_SLUG,
        CONF_ACTIVITY_NAME,
        CONF_ICON,
        CONF_ICON_ATTRIBUTE,
        CONF_PRIORITY,
        CONF_SOUND,
        CONF_UPDATE_INTERVAL,
        CONF_STATE_LABELS,
    ),
    "colors": (
        CONF_ACCENT_COLOR,
        CONF_ACCENT_COLOR_ATTRIBUTE,
        CONF_BACKGROUND_COLOR,
        CONF_BACKGROUND_COLOR_ATTRIBUTE,
        CONF_TEXT_COLOR,
        CONF_TEXT_COLOR_ATTRIBUTE,
    ),
    "tap_actions": (
        CONF_TAP_ACTION_URL,
        CONF_TAP_ACTION_FOREGROUND,
        CONF_URL,
        CONF_URL_FOREGROUND,
        CONF_URL_TITLE,
        CONF_SECONDARY_URL,
        CONF_SECONDARY_URL_FOREGROUND,
        CONF_SECONDARY_URL_TITLE,
    ),
    "advanced": (
        CONF_ENDED_TTL,
        CONF_STALE_TTL,
        CONF_DISMISSAL_TTL,
    ),
}

# Fields the entity detail form keeps visible per template, on top of the always
# top-level start/end states. remaining_time is top-level only on countdown; on
# generic/steps it drops into data_sources.
_ENTITY_TOPLEVEL_EXTRA: dict[str, tuple[str, ...]] = {
    "generic": (),
    "countdown": (
        CONF_REMAINING_TIME_ENTITY,
        CONF_REMAINING_TIME_ATTR,
        CONF_COMPLETION_MESSAGE,
        CONF_WARNING_THRESHOLD,
        CONF_ALARM,
        CONF_SNOOZE_SECONDS,
    ),
    "alert": (CONF_SEVERITY, CONF_SEVERITY_LABEL),
    "steps": (CONF_TOTAL_STEPS, CONF_CURRENT_STEP_ENTITY, CONF_CURRENT_STEP_ATTR),
    "gauge": (CONF_MIN_VALUE, CONF_MAX_VALUE, CONF_UNIT),
    "timeline": (CONF_SERIES_ENTITIES, CONF_UNIT),
    "board": (CONF_TILES,),
    "log": (CONF_LOG_COLUMNS,),
}

WIDGET_SECTIONS: dict[str, tuple[str, ...]] = {
    "display_options": (
        CONF_LABEL,
        CONF_LABEL_ATTRIBUTE,
        CONF_SUBTITLE_ATTRIBUTE,
        CONF_ICON,
        CONF_ICON_ATTRIBUTE,
    ),
    "colors": (
        CONF_ACCENT_COLOR,
        CONF_ACCENT_COLOR_ATTRIBUTE,
        CONF_BACKGROUND_COLOR,
        CONF_TEXT_COLOR,
    ),
    "tap_actions": (
        CONF_TAP_ACTION_URL,
        CONF_TAP_ACTION_FOREGROUND,
    ),
    "refresh": (
        CONF_WIDGET_TRIGGER_MODE,
        CONF_WIDGET_POLL_INTERVAL,
    ),
}

# Widget top-level = the widget name plus each template's essential value fields.
# None of these overlap a widget section, so a flat set (no per-template table) works.
_WIDGET_TOPLEVEL: frozenset[str] = frozenset(
    {
        CONF_WIDGET_NAME,
        CONF_VALUE_ATTRIBUTE,
        CONF_UNIT,
        CONF_VALUE_SCALE,
        CONF_MIN_VALUE,
        CONF_MAX_VALUE,
        CONF_SEVERITY,
        CONF_STAT_ROWS,
    }
)


def _entity_toplevel_fields(template: str) -> set[str]:
    """Fields the entity detail form keeps uncollapsed for `template`."""
    return {CONF_START_STATES, CONF_END_STATES, *_ENTITY_TOPLEVEL_EXTRA.get(template, ())}


def _sectioned_schema(
    fields: dict,
    sections_def: dict[str, tuple[str, ...]],
    toplevel: set[str] | frozenset[str],
    expand: set[str],
) -> vol.Schema:
    """Partition an ordered {vol_key: selector} mapping into a top-level schema plus
    collapsible ``section()`` groups.

    ``sections_def`` maps section key -> the field names that belong to it; a field
    listed in ``toplevel`` stays visible even when it has a section membership. A
    section with no field for this form is dropped. ``expand`` names sections that
    render open (used to reveal a section holding a field that failed validation).
    Per-field defaults/suggested values already baked into the vol keys carry into
    the section schemas unchanged, so reconfigure prefill needs no extra nesting.
    """
    field_section: dict[str, str] = {}
    for sec, names in sections_def.items():
        for name in names:
            field_section.setdefault(name, sec)

    top: dict = {}
    grouped: dict[str, dict] = {sec: {} for sec in sections_def}
    for key, selector in fields.items():
        name = key.schema if isinstance(key, vol.Marker) else key
        sec = field_section.get(name)
        if sec is not None and name not in toplevel:
            grouped[sec][key] = selector
        else:
            top[key] = selector

    schema: dict = dict(top)
    for sec, inner in grouped.items():
        if inner:
            schema[vol.Required(sec)] = section(vol.Schema(inner), {"collapsed": sec not in expand})
    return vol.Schema(schema)


def _flatten_section_input(user_input: dict, section_keys) -> dict:
    """Lift ``section()`` groups back out to a flat field dict.

    HA submits a section's fields nested under its key ({"colors": {...}}); this
    unwraps only the known section keys so the managers/mappers keep seeing flat
    storage. Any other dict-valued field (state_labels, series, ...) is left as-is,
    and already-flat input passes through unchanged (idempotent).
    """
    flat: dict = {}
    for key, value in user_input.items():
        if key in section_keys and isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


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
                    translation_key="activity_template",
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


# Domains whose primary state reads as an on/off-style status widget.
_STATUS_WIDGET_DOMAINS = frozenset({"binary_sensor", "lock", "switch", "cover"})


def _suggest_widget_template(hass: HomeAssistant | None, entity_id: str) -> str:
    """Suggest the best widget template for an entity based on domain/device_class/state_class."""
    if not entity_id or hass is None:
        return WIDGET_TEMPLATE_VALUE

    domain = _entity_domain(entity_id)
    if domain in _STATUS_WIDGET_DOMAINS:
        return WIDGET_TEMPLATE_STATUS

    state_obj = hass.states.get(entity_id)
    if state_obj is None:
        return WIDGET_TEMPLATE_VALUE

    attrs = state_obj.attributes
    if domain in ("sensor", "number"):
        if attrs.get("state_class") in ("measurement", "total"):
            return WIDGET_TEMPLATE_GAUGE
        if attrs.get("device_class", "") in _GAUGE_DEVICE_CLASSES:
            return WIDGET_TEMPLATE_GAUGE

    return WIDGET_TEMPLATE_VALUE


def _details_schema(
    entity_id: str,
    template: str,
    defaults: dict | None = None,
    hass: HomeAssistant | None = None,
    expand: set[str] | None = None,
) -> vol.Schema:
    """Build step-2 schema with all config fields and dynamic selectors.

    Fields are grouped into collapsible sections per ENTITY_SECTIONS; only each
    template's essentials stay top-level. ``expand`` opens named sections (so a
    section holding a validation error re-renders uncollapsed).
    """
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
    dismissal_ttl_val = d.get(CONF_DISMISSAL_TTL)
    dismissal_ttl_key = (
        vol.Optional(CONF_DISMISSAL_TTL, default=dismissal_ttl_val)
        if dismissal_ttl_val is not None
        else vol.Optional(CONF_DISMISSAL_TTL)
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
        ] = NumberSelector(NumberSelectorConfig(min=1, max=TOTAL_STEPS_MAX, mode=NumberSelectorMode.BOX))
        fields[_entity_source_key(CONF_CURRENT_STEP_ENTITY, d)] = entity_selector
        fields[
            vol.Optional(
                CONF_CURRENT_STEP_ATTR,
                description={"suggested_value": d.get(CONF_CURRENT_STEP_ATTR, "")},
            )
        ] = attr_selector
        # One row per step (label / row height / weight / color) in step order. A
        # legacy per-key comma string is still accepted by _parse_entity_input.
        fields[_object_rows_key(CONF_STEPS_EDITOR, d, required=False)] = _STEPS_EDITOR_SELECTOR
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
                translation_key="severity",
            )
        )
        fields[
            vol.Optional(
                CONF_SEVERITY_LABEL,
                default=d.get(CONF_SEVERITY_LABEL, ""),
            )
        ] = vol.All(str, vol.Length(max=MAX_SEVERITY_LABEL_LEN))
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
        ] = NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any"))
        fields[
            vol.Required(
                CONF_MAX_VALUE,
                default=d.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE),
            )
        ] = NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any"))
        fields[
            vol.Optional(
                CONF_UNIT,
                default=d.get(CONF_UNIT, ""),
            )
        ] = vol.All(str, vol.Length(max=32))
    if template == "timeline":
        # Map attributes of the tracked entity to named series (a two-column row
        # editor: attribute -> label). A legacy 'attr=Label, ...' string is still accepted.
        fields[_object_rows_key(CONF_SERIES, d, required=False)] = _SERIES_MAP_SELECTOR
        # Bind separate entities as named series (a row editor): each row is a
        # label (optional; defaults to the entity's friendly name), an entity, and
        # an optional attribute. A legacy comma string is still accepted.
        fields[_object_rows_key(CONF_SERIES_ENTITIES, d, required=False)] = _SERIES_ENTITIES_SELECTOR
        # Per-series unit overrides (a two-column row editor: series label -> unit).
        # A legacy 'Series=unit, ...' string is still accepted.
        fields[_object_rows_key(CONF_UNITS, d, required=False)] = _UNITS_SELECTOR
        # Primary series: pick from the configured series labels, or type a custom
        # value. Options are the stored labels on reconfigure and empty on first add
        # (custom_value lets the user type one before any series is saved); the
        # authoritative check stays unknown_primary_series in _parse_entity_input.
        fields[
            vol.Optional(
                CONF_PRIMARY_SERIES,
                default=d.get(CONF_PRIMARY_SERIES, ""),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=_primary_series_options(d),
                custom_value=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
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
                translation_key="timeline_scale",
            )
        )
        fields[
            vol.Optional(
                CONF_DECIMALS,
                default=d.get(CONF_DECIMALS, DEFAULT_DECIMALS),
            )
        ] = NumberSelector(NumberSelectorConfig(min=0, max=10, mode=NumberSelectorMode.BOX))
        fields[
            vol.Optional(
                CONF_SMOOTHING,
                default=d.get(CONF_SMOOTHING, False),
            )
        ] = BooleanSelector()
        # Threshold reference lines (a row editor): value + optional color + label.
        # A legacy 'value:color:label' comma string is still accepted.
        fields[_object_rows_key(CONF_THRESHOLDS, d, required=False)] = _THRESHOLDS_SELECTOR
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
        # Tiles are a row editor (ObjectSelector): each row binds a separate entity
        # (label, entity, attribute, unit, icon, color, url). The anchor entity
        # (step 1) drives start/end; tiles read the companion entities.
        fields[_object_rows_key(CONF_TILES, d, required=True)] = _BOARD_TILES_SELECTOR
    if template == "log":
        # Optional extra columns composed into each line's text (a row editor). Each
        # row is a label, an optional entity, an optional attribute, and an optional
        # unit: an attribute with no entity reads the tracked entity; an entity with
        # no attribute reads that entity's state; both read that entity's attribute.
        # A legacy '[Label=]source[|unit]' comma string is still accepted.
        fields[_object_rows_key(CONF_LOG_COLUMNS, d, required=False)] = _LOG_COLUMNS_SELECTOR
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
    ] = NumberSelector(NumberSelectorConfig(min=PRIORITY_MIN, max=PRIORITY_MAX, mode=NumberSelectorMode.SLIDER))
    fields[
        vol.Optional(
            CONF_SOUND,
            default=d.get(CONF_SOUND, ""),
        )
    ] = SelectSelector(
        SelectSelectorConfig(
            options=["", *list(SOUNDS)],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="sound",
        )
    )
    fields[
        vol.Optional(
            CONF_UPDATE_INTERVAL,
            default=d.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        )
    ] = NumberSelector(
        NumberSelectorConfig(min=UPDATE_INTERVAL_MIN, mode=NumberSelectorMode.BOX, unit_of_measurement="seconds")
    )

    # --- Optional fields ---
    fields[_entity_source_key(CONF_SUBTITLE_ENTITY, d)] = entity_selector
    fields[
        vol.Optional(
            CONF_SUBTITLE_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_SUBTITLE_ATTRIBUTE, "")},
        )
    ] = attr_selector
    # Custom display text per state (a two-column row editor: state -> label). A
    # legacy 'state=Label, ...' string is still accepted by _parse_entity_input.
    fields[_object_rows_key(CONF_STATE_LABELS, d, required=False)] = _STATE_LABELS_SELECTOR
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
    fields[dismissal_ttl_key] = NumberSelector(
        NumberSelectorConfig(
            min=DISMISSAL_TTL_MIN,
            max=DISMISSAL_TTL_MAX,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="seconds",
        )
    )

    return _sectioned_schema(fields, ENTITY_SECTIONS, _entity_toplevel_fields(template), expand or set())


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
    """Coerce min/max value pair; raise invalid_gauge_range if min >= max for gauge templates.

    nan/inf are caught first: a nan never trips the min >= max check (every
    comparison with nan is False) so it would otherwise slip through.
    """
    min_v = float(user_input.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE))
    max_v = float(user_input.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE))
    bad = [key for key, val in ((CONF_MIN_VALUE, min_v), (CONF_MAX_VALUE, max_v)) if not math.isfinite(val)]
    if bad:
        raise vol.Invalid("invalid_number", path=bad)
    if is_gauge and min_v >= max_v:
        raise vol.Invalid("invalid_gauge_range", path=[CONF_MIN_VALUE])
    return min_v, max_v


def _strict_board_tile(item: object) -> dict:
    """Validate + normalize one board-tile row dict, raising on any problem.

    Enforces the requiredness of label/entity_id and the per-field length caps,
    color rule, and URL rule that the removed whole-blob length check used to
    approximate. Returns a tile carrying only the non-empty storage keys.
    """
    if not isinstance(item, dict):
        raise vol.Invalid("invalid_tile", path=[CONF_TILES])
    label = str(item.get(CONF_LABEL, "") or "").strip()
    entity_id = str(item.get(CONF_ENTITY_ID, "") or "").strip()
    if not label or not entity_id:
        raise vol.Invalid("invalid_tile", path=[CONF_TILES])
    attr = str(item.get(CONF_VALUE_ATTRIBUTE, "") or "").strip()
    unit = str(item.get(CONF_UNIT, "") or "").strip()
    icon = str(item.get(CONF_ICON, "") or "").strip()
    color = str(item.get("color", "") or "").strip()
    url_action = str(item.get("url_action", "") or "").strip()
    if len(label) > BOARD_TILE_LABEL_MAX or len(unit) > BOARD_TILE_UNIT_MAX or len(icon) > BOARD_TILE_ICON_MAX:
        raise vol.Invalid("invalid_tile", path=[CONF_TILES])
    if color and not is_valid_color(color):
        raise vol.Invalid("invalid_tile", path=[CONF_TILES])
    if url_action:
        code = _tap_action_url_error(url_action, foreground=True)
        if code is not None:
            raise vol.Invalid(code, path=[CONF_TILES])
    tile: dict = {CONF_LABEL: label, CONF_ENTITY_ID: entity_id}
    if attr:
        tile[CONF_VALUE_ATTRIBUTE] = attr
    if unit:
        tile[CONF_UNIT] = unit
    if icon:
        tile[CONF_ICON] = icon
    if color:
        tile["color"] = color
    if url_action:
        tile["url_action"] = url_action
    return tile


def _parse_board_tiles(raw: object, *, strict: bool = False) -> list[dict]:
    """Parse board tiles from a row-editor list (or the legacy comma string).

    From the form each row is already a dict with the storage keys: ``{label,
    entity_id, value_attribute?, unit?, icon?, color?, url_action?}``. The legacy
    string form ('label=entity_id[:attr[:unit[:icon]]], ...') stays accepted for
    stored-string edge cases and non-form callers. Capped at BOARD_MAX_TILES.
    Lenient (default) skips malformed rows/entries and truncates past the cap;
    strict (form paths) raises invalid_tile / too_many_tiles (and applies the
    per-field length, color, and URL rules) instead.
    """
    if isinstance(raw, list):
        if not strict:
            kept = [t for t in raw if isinstance(t, dict) and t.get(CONF_ENTITY_ID) and t.get(CONF_LABEL)]
            return kept[:BOARD_MAX_TILES]
        tiles = [_strict_board_tile(item) for item in raw]
        if len(tiles) > BOARD_MAX_TILES:
            raise vol.Invalid("too_many_tiles", path=[CONF_TILES])
        return tiles
    if not isinstance(raw, str) or not raw.strip():
        return []
    tiles = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            if strict:
                raise vol.Invalid("invalid_tile", path=[CONF_TILES])
            continue
        label, rest = entry.split("=", 1)
        label = label.strip()
        # maxsplit=3 keeps the icon (4th field) intact even when it contains a
        # colon, e.g. an "mdi:cpu" MDI icon - otherwise the prefix would be lost.
        parts = [p.strip() for p in rest.split(":", 3)]
        if not label or not parts or not parts[0]:
            if strict:
                raise vol.Invalid("invalid_tile", path=[CONF_TILES])
            continue
        tile: dict = {CONF_LABEL: label, CONF_ENTITY_ID: parts[0]}
        if len(parts) > 1 and parts[1]:
            tile[CONF_VALUE_ATTRIBUTE] = parts[1]
        if len(parts) > 2 and parts[2]:
            tile[CONF_UNIT] = parts[2]
        if len(parts) > 3 and parts[3]:
            tile[CONF_ICON] = parts[3]
        tiles.append(tile)
        if not strict and len(tiles) >= BOARD_MAX_TILES:
            break
    if strict and len(tiles) > BOARD_MAX_TILES:
        raise vol.Invalid("too_many_tiles", path=[CONF_TILES])
    return tiles[:BOARD_MAX_TILES]


def _strict_log_column(item: object) -> dict:
    """Validate + normalize one log-column row dict, raising on any problem.

    A column reads a source: an attribute of the tracked entity (entity empty,
    attribute set), another entity's state (entity set, attribute empty), or that
    entity's attribute (both set). A row carrying neither is meaningless and bounces
    with invalid_log_column; the label is capped at LOG_COLUMN_LABEL_MAX. Returns a
    column carrying only the non-empty storage keys.
    """
    if not isinstance(item, dict):
        raise vol.Invalid("invalid_log_column", path=[CONF_LOG_COLUMNS])
    entity_id = str(item.get(CONF_ENTITY_ID, "") or "").strip()
    attr = str(item.get("attribute", "") or "").strip()
    if not entity_id and not attr:
        raise vol.Invalid("invalid_log_column", path=[CONF_LOG_COLUMNS])
    label = str(item.get(CONF_LABEL, "") or "").strip()
    unit = str(item.get(CONF_UNIT, "") or "").strip()
    if len(label) > LOG_COLUMN_LABEL_MAX:
        raise vol.Invalid("invalid_log_column", path=[CONF_LOG_COLUMNS])
    column: dict = {}
    if label:
        column[CONF_LABEL] = label
    if entity_id:
        column[CONF_ENTITY_ID] = entity_id
    if attr:
        column["attribute"] = attr
    if unit:
        column[CONF_UNIT] = unit
    return column


def _parse_log_columns(raw: object, *, strict: bool = False) -> list[dict]:
    """Parse log columns from a row-editor list (or the legacy comma string).

    From the form each row is a dict with the storage keys: ``{label?, entity_id?,
    attribute?, unit?}``. The legacy string form ('[Label=]source[|unit], ...') stays
    accepted for stored-string edge cases and non-form callers. Capped at
    LOG_MAX_COLUMNS. ``source`` disambiguates in the string form:
      - ``brightness`` (no dot)            an attribute of the tracked entity
      - ``binary_sensor.door`` (has a dot) another entity's state
      - ``sensor.temp:temperature``        another entity's attribute
    Lenient (default) skips malformed rows/entries and truncates past the cap; strict
    (form paths) raises invalid_log_column / too_many_log_columns (and applies the
    label length cap) instead.
    """
    if isinstance(raw, list):
        if not strict:
            kept = [c for c in raw if isinstance(c, dict) and (c.get(CONF_ENTITY_ID) or c.get("attribute"))]
            return kept[:LOG_MAX_COLUMNS]
        columns = [_strict_log_column(item) for item in raw]
        if len(columns) > LOG_MAX_COLUMNS:
            raise vol.Invalid("too_many_log_columns", path=[CONF_LOG_COLUMNS])
        return columns
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
            if strict:
                raise vol.Invalid("invalid_log_column", path=[CONF_LOG_COLUMNS])
            continue
        column: dict = {}
        if label:
            column[CONF_LABEL] = label
        if ":" in source:
            entity_id, attr = (part.strip() for part in source.split(":", 1))
            if not entity_id:
                if strict:
                    raise vol.Invalid("invalid_log_column", path=[CONF_LOG_COLUMNS])
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
        if not strict and len(columns) >= LOG_MAX_COLUMNS:
            break
    if strict and len(columns) > LOG_MAX_COLUMNS:
        raise vol.Invalid("too_many_log_columns", path=[CONF_LOG_COLUMNS])
    return columns[:LOG_MAX_COLUMNS]


def _strict_series_entity(item: object) -> dict:
    """Validate + normalize one timeline series-entity row dict, raising on any problem.

    The entity is required (a series binds a separate entity); the optional label is
    capped at TIMELINE_SERIES_LABEL_MAX. The label is left raw here and frozen later
    by ``_resolve_series_entity_labels``. Returns a series carrying only the non-empty
    storage keys.
    """
    if not isinstance(item, dict):
        raise vol.Invalid("invalid_series_entity", path=[CONF_SERIES_ENTITIES])
    entity_id = str(item.get(CONF_ENTITY_ID, "") or "").strip()
    if not entity_id:
        raise vol.Invalid("invalid_series_entity", path=[CONF_SERIES_ENTITIES])
    attr = str(item.get("attribute", "") or "").strip()
    label = str(item.get(CONF_LABEL, "") or "").strip()
    if len(label) > TIMELINE_SERIES_LABEL_MAX:
        raise vol.Invalid("invalid_series_entity", path=[CONF_SERIES_ENTITIES])
    series: dict = {CONF_ENTITY_ID: entity_id}
    if attr:
        series["attribute"] = attr
    if label:
        series[CONF_LABEL] = label
    return series


def _parse_series_entities(raw: object, *, strict: bool = False) -> list[dict]:
    """Parse timeline series entities from a row-editor list (or the legacy comma string).

    From the form each row is a dict with the storage keys: ``{label?, entity_id,
    attribute?}``. The legacy string form ('[Label=]entity_id[:attribute], ...') stays
    accepted for stored-string edge cases and non-form callers; in that form ``source``
    must be an entity_id (contains a dot) and a bare word is not a series. The optional
    label is left raw here and frozen later by ``_resolve_series_entity_labels``. The
    lenient (default) list/string path skips malformed rows and truncates past
    TIMELINE_MAX_SERIES; strict (form paths) raises invalid_series_entity per row and
    leaves the over-cap check to the combined series + series_entities guard in
    ``_parse_entity_input`` (so a lone over-cap list still trips too_many_series).
    """
    if isinstance(raw, list):
        if not strict:
            series = [s for s in raw if isinstance(s, dict) and s.get(CONF_ENTITY_ID)]
            return series[:TIMELINE_MAX_SERIES]
        return [_strict_series_entity(item) for item in raw]
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
            if strict:
                raise vol.Invalid("invalid_series_entity", path=[CONF_SERIES_ENTITIES])
            continue
        series: dict = {}
        if ":" in source:
            entity_id, attr = (part.strip() for part in source.split(":", 1))
            if not entity_id or "." not in entity_id:
                if strict:
                    raise vol.Invalid("invalid_series_entity", path=[CONF_SERIES_ENTITIES])
                continue
            series[CONF_ENTITY_ID] = entity_id
            if attr:
                series["attribute"] = attr
        elif "." in source:
            series[CONF_ENTITY_ID] = source
        else:
            if strict:
                raise vol.Invalid("invalid_series_entity", path=[CONF_SERIES_ENTITIES])
            continue
        if label:
            series[CONF_LABEL] = label
        result.append(series)
        if len(result) >= TIMELINE_MAX_SERIES:
            break
    return result


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


def _timeline_series_labels(
    series: dict, series_entities: list[dict], entity_id: str, hass: HomeAssistant | None
) -> set[str]:
    """Collect every label a timeline primary_series may legitimately name.

    Mirrors how content_mapper labels series: CONF_SERIES values, each resolved
    CONF_SERIES_ENTITIES label, and - when neither source is configured - the
    single default series labelled with the tracked entity's friendly name (see
    _get_timeline_values).
    """
    labels: set[str] = set(series.values())
    labels.update(s[CONF_LABEL] for s in series_entities if s.get(CONF_LABEL))
    if not series and not series_entities:
        labels.add(_entity_friendly_name(hass, entity_id))
    return labels


def _primary_series_options(config: dict) -> list[str]:
    """Series labels offered by the primary_series dropdown (custom_value still allows typing).

    Sourced from the stored CONF_SERIES map values (or its row-editor rows on
    reconfigure) and each resolved CONF_SERIES_ENTITIES label, de-duplicated in
    order. Empty on a fresh add, which is fine: custom_value lets the user type one.
    """
    options: list[str] = []
    series = config.get(CONF_SERIES)
    if isinstance(series, dict):
        options.extend(str(v) for v in series.values() if v)
    elif isinstance(series, list):
        for row in series:
            if isinstance(row, dict) and row.get(CONF_LABEL):
                options.append(str(row[CONF_LABEL]))
    for entry in config.get(CONF_SERIES_ENTITIES) or []:
        if isinstance(entry, dict) and entry.get(CONF_LABEL):
            options.append(str(entry[CONF_LABEL]))
    seen: set[str] = set()
    result: list[str] = []
    for label in options:
        if label not in seen:
            seen.add(label)
            result.append(label)
    return result


def _parse_entity_input(user_input: dict, hass: HomeAssistant | None = None) -> dict:
    """Normalize user input into an entity config dict.

    Called only from the two subentry form paths, so the DSL parsers run in
    strict mode: malformed tokens raise vol.Invalid instead of being silently
    dropped, and the details step maps each exc.path field to its error code.
    """
    user_input = _flatten_section_input(user_input, ENTITY_SECTIONS)
    entity_id = user_input[CONF_ENTITY_ID]
    template = user_input.get(CONF_TEMPLATE, "generic")
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
    dismissal_ttl = user_input.get(CONF_DISMISSAL_TTL)

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

    min_v, max_v = _coerce_gauge_range(user_input, is_gauge=template == "gauge")

    # Parse timeline fields (series map: attribute -> label)
    series = _kv_rows_to_map(user_input.get(CONF_SERIES, ""), "attribute", CONF_LABEL)
    series_entities = _resolve_series_entity_labels(
        _parse_series_entities(user_input.get(CONF_SERIES_ENTITIES, ""), strict=True), hass
    )
    if len(series) + len(series_entities) > TIMELINE_MAX_SERIES:
        raise vol.Invalid("too_many_series", path=[CONF_SERIES_ENTITIES])
    primary_series = (user_input.get(CONF_PRIMARY_SERIES) or "").strip()
    if template == "timeline" and primary_series:
        known_labels = _timeline_series_labels(series, series_entities, entity_id, hass)
        if primary_series not in known_labels:
            raise vol.Invalid("unknown_primary_series", path=[CONF_PRIMARY_SERIES])
    thresholds = _parse_thresholds(user_input.get(CONF_THRESHOLDS, ""), strict=True)
    history_period_raw = user_input.get(CONF_HISTORY_PERIOD, DEFAULT_HISTORY_PERIOD)

    # Board tiles: a board needs at least one tile to render.
    tiles = _parse_board_tiles(user_input.get(CONF_TILES, ""), strict=True)
    if template == "board" and not tiles:
        raise vol.Invalid("tiles_required", path=[CONF_TILES])

    # Steps: the unified editor submits one row per step, decomposed here into the
    # four stored keys. The row count must be 0 or exactly total_steps (the server
    # drops a wrong-length list, so surface it here rather than silently).
    total_steps = int(user_input.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS))
    step_fields = _steps_fields_from_input(user_input, total_steps, strict=template == "steps")
    log_columns = _parse_log_columns(user_input.get(CONF_LOG_COLUMNS, ""), strict=True)

    return {
        CONF_ENTITY_ID: entity_id,
        CONF_SLUG: slug,
        CONF_ACTIVITY_NAME: user_input.get(CONF_ACTIVITY_NAME, "") or entity_id,
        CONF_ICON: user_input.get(CONF_ICON, ""),
        CONF_ICON_ATTRIBUTE: user_input.get(CONF_ICON_ATTRIBUTE, ""),
        CONF_PRIORITY: int(user_input.get(CONF_PRIORITY, DEFAULT_PRIORITY)),
        CONF_TEMPLATE: user_input.get(CONF_TEMPLATE, "generic"),
        CONF_START_STATES: start_states or defaults.get("start_states", []),
        CONF_END_STATES: end_states or defaults.get("end_states", []),
        CONF_UPDATE_INTERVAL: int(user_input.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
        CONF_PROGRESS_ATTRIBUTE: user_input.get(CONF_PROGRESS_ATTRIBUTE, ""),
        CONF_PROGRESS_ENTITY: user_input.get(CONF_PROGRESS_ENTITY, ""),
        CONF_REMAINING_TIME_ATTR: user_input.get(CONF_REMAINING_TIME_ATTR, ""),
        CONF_REMAINING_TIME_ENTITY: user_input.get(CONF_REMAINING_TIME_ENTITY, ""),
        CONF_LIVE_PROGRESS: bool(user_input.get(CONF_LIVE_PROGRESS, False)),
        CONF_SUBTITLE_ATTRIBUTE: user_input.get(CONF_SUBTITLE_ATTRIBUTE, ""),
        CONF_SUBTITLE_ENTITY: user_input.get(CONF_SUBTITLE_ENTITY, ""),
        CONF_STATE_LABELS: _kv_rows_to_map(user_input.get(CONF_STATE_LABELS, ""), "state", CONF_LABEL),
        CONF_COMPLETION_MESSAGE: user_input.get(CONF_COMPLETION_MESSAGE, ""),
        CONF_TOTAL_STEPS: int(user_input.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)),
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
        CONF_DISMISSAL_TTL: int(dismissal_ttl) if dismissal_ttl is not None else None,
        CONF_SERIES: series,
        CONF_SERIES_ENTITIES: series_entities,
        CONF_PRIMARY_SERIES: primary_series,
        CONF_SCALE: user_input.get(CONF_SCALE, DEFAULT_SCALE),
        CONF_DECIMALS: int(user_input.get(CONF_DECIMALS, DEFAULT_DECIMALS)),
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
        CONF_STEP_LABELS: step_fields[CONF_STEP_LABELS],
        CONF_STEP_ROWS: step_fields[CONF_STEP_ROWS],
        CONF_STEP_WEIGHTS: step_fields[CONF_STEP_WEIGHTS],
        CONF_STEP_COLORS: step_fields[CONF_STEP_COLORS],
        CONF_FIRED_AT_ATTRIBUTE: user_input.get(CONF_FIRED_AT_ATTRIBUTE, ""),
        CONF_FIRED_AT_ENTITY: user_input.get(CONF_FIRED_AT_ENTITY, ""),
        CONF_UNITS: _kv_rows_to_map(user_input.get(CONF_UNITS, ""), "series", CONF_UNIT),
        CONF_BACKGROUND_COLOR: _rgb_to_hex(user_input.get(CONF_BACKGROUND_COLOR)),
        CONF_BACKGROUND_COLOR_ATTRIBUTE: user_input.get(CONF_BACKGROUND_COLOR_ATTRIBUTE, ""),
        CONF_TEXT_COLOR: _rgb_to_hex(user_input.get(CONF_TEXT_COLOR)),
        CONF_TEXT_COLOR_ATTRIBUTE: user_input.get(CONF_TEXT_COLOR_ATTRIBUTE, ""),
        CONF_TILES: tiles,
        CONF_LOG_LEVEL_ATTRIBUTE: user_input.get(CONF_LOG_LEVEL_ATTRIBUTE, ""),
        CONF_LOG_COLUMNS: log_columns,
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
            # Prepare defaults for step 2 from existing config. The map fields
            # (state_labels, series, units) rehydrate their two-column row editors
            # from the stored dict; the unified steps editor composes one row per
            # step from the four stored step keys, which stay untouched in storage.
            current = dict(subentry.data)
            labels = current.get(CONF_STATE_LABELS)
            if isinstance(labels, dict):
                current[CONF_STATE_LABELS] = _map_to_kv_rows(labels, "state", CONF_LABEL)
            series = current.get(CONF_SERIES)
            if isinstance(series, dict):
                current[CONF_SERIES] = _map_to_kv_rows(series, "attribute", CONF_LABEL)
            units = current.get(CONF_UNITS)
            if isinstance(units, dict):
                current[CONF_UNITS] = _map_to_kv_rows(units, "series", CONF_UNIT)
            current[CONF_STEPS_EDITOR] = _compose_steps_rows(current)
            # series_entities, thresholds, tiles and log_columns stay lists: their
            # ObjectSelector row editors rehydrate from the stored list directly.
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
        # Reveal any section that holds a field that failed validation.
        toplevel = _entity_toplevel_fields(template)
        expand = {sec for sec, fs in ENTITY_SECTIONS.items() if set(errors) & (set(fs) - toplevel)}
        schema = _details_schema(entity_id, template, defaults=defaults, hass=self.hass, expand=expand)
        if errors and user_input is not None:
            # Re-fill the form with what the user just submitted instead of dropping it.
            # user_input is still nested per section; add_suggested_values recurses into
            # sections, so the re-fill lands on the right keys.
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="details",
            data_schema=schema,
            errors=errors,
            last_step=True,
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
                    translation_key="widget_template",
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
    expand: set[str] | None = None,
) -> vol.Schema:
    """Step-2 schema: template-specific fields + cosmetics + trigger mode.

    Cosmetics, colors, tap action, and the trigger/poll pair collapse into
    sections (WIDGET_SECTIONS); each template's value fields stay top-level.
    """
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

    if template == WIDGET_TEMPLATE_PROGRESS:
        fields[
            vol.Optional(
                CONF_VALUE_SCALE,
                default=d.get(CONF_VALUE_SCALE, DEFAULT_VALUE_SCALE),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=VALUE_SCALES,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="value_scale",
            )
        )

    if template == WIDGET_TEMPLATE_GAUGE:
        fields[
            vol.Required(
                CONF_MIN_VALUE,
                default=d.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE),
            )
        ] = NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any"))
        fields[
            vol.Required(
                CONF_MAX_VALUE,
                default=d.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE),
            )
        ] = NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any"))

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
                translation_key="widget_severity",
            )
        )

    if template == WIDGET_TEMPLATE_STAT_LIST:
        # stat_rows are a row editor (ObjectSelector): each row binds a separate
        # entity (label, entity, attribute, unit). Capped by WIDGET_MAX_STAT_ROWS.
        fields[_object_rows_key(CONF_STAT_ROWS, d, required=True)] = _WIDGET_STAT_ROWS_SELECTOR

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

    # Trigger mode + interval. Optional (not Required) so the parser's own default
    # applies when the collapsed "refresh" section is submitted without touching it.
    fields[
        vol.Optional(
            CONF_WIDGET_TRIGGER_MODE,
            default=d.get(CONF_WIDGET_TRIGGER_MODE, WIDGET_TRIGGER_EVENT),
        )
    ] = SelectSelector(
        SelectSelectorConfig(
            options=WIDGET_TRIGGER_MODES,
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="widget_trigger_mode",
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

    return _sectioned_schema(fields, WIDGET_SECTIONS, _WIDGET_TOPLEVEL, expand or set())


def _strict_stat_row(item: object) -> dict:
    """Validate + normalize one widget stat_row dict, raising on any problem.

    Enforces requiredness of label/entity_id and the per-field length caps the
    removed whole-blob length check used to approximate. Returns a row carrying
    only the non-empty storage keys.
    """
    if not isinstance(item, dict):
        raise vol.Invalid("invalid_stat_row", path=[CONF_STAT_ROWS])
    label = str(item.get(CONF_LABEL, "") or "").strip()
    entity_id = str(item.get(CONF_ENTITY_ID, "") or "").strip()
    if not label or not entity_id:
        raise vol.Invalid("invalid_stat_row", path=[CONF_STAT_ROWS])
    attr = str(item.get(CONF_VALUE_ATTRIBUTE, "") or "").strip()
    unit = str(item.get(CONF_UNIT, "") or "").strip()
    if len(label) > WIDGET_STAT_LABEL_MAX or len(unit) > WIDGET_STAT_UNIT_MAX:
        raise vol.Invalid("invalid_stat_row", path=[CONF_STAT_ROWS])
    row: dict = {CONF_LABEL: label, CONF_ENTITY_ID: entity_id}
    if attr:
        row[CONF_VALUE_ATTRIBUTE] = attr
    if unit:
        row[CONF_UNIT] = unit
    return row


def _parse_widget_stat_rows(raw: object, *, strict: bool = False) -> list[dict]:
    """Parse stat_rows from a row-editor list (or the legacy comma string).

    From the form each row is a dict with the storage keys: ``{label, entity_id,
    value_attribute?, unit?}``. The legacy string form
    ('label=entity_id[:attr[:unit]], ...') stays accepted for stored-string edge
    cases and non-form callers. Lenient (default) skips malformed rows/entries and
    truncates past the cap; strict (form paths) raises invalid_stat_row /
    too_many_stat_rows (and applies the per-field length caps) instead.
    """
    if isinstance(raw, list):
        if not strict:
            kept = [r for r in raw if isinstance(r, dict) and r.get(CONF_ENTITY_ID) and r.get(CONF_LABEL)]
            return kept[:WIDGET_MAX_STAT_ROWS]
        rows = [_strict_stat_row(item) for item in raw]
        if len(rows) > WIDGET_MAX_STAT_ROWS:
            raise vol.Invalid("too_many_stat_rows", path=[CONF_STAT_ROWS])
        return rows
    if not isinstance(raw, str) or not raw.strip():
        return []
    rows: list[dict] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            if strict:
                raise vol.Invalid("invalid_stat_row", path=[CONF_STAT_ROWS])
            continue
        label, rest = entry.split("=", 1)
        label = label.strip()
        parts = [p.strip() for p in rest.split(":")]
        if not label or not parts or not parts[0]:
            if strict:
                raise vol.Invalid("invalid_stat_row", path=[CONF_STAT_ROWS])
            continue
        row: dict = {CONF_LABEL: label, CONF_ENTITY_ID: parts[0]}
        if len(parts) > 1 and parts[1]:
            row[CONF_VALUE_ATTRIBUTE] = parts[1]
        if len(parts) > 2 and parts[2]:
            row[CONF_UNIT] = parts[2]
        rows.append(row)
        if not strict and len(rows) >= WIDGET_MAX_STAT_ROWS:
            break
    if strict and len(rows) > WIDGET_MAX_STAT_ROWS:
        raise vol.Invalid("too_many_stat_rows", path=[CONF_STAT_ROWS])
    return rows[:WIDGET_MAX_STAT_ROWS]


def _parse_widget_input(user_input: dict, step1: dict) -> dict:
    """Build the persisted subentry data from step-1 + step-2 inputs."""
    user_input = _flatten_section_input(user_input, WIDGET_SECTIONS)
    entity_id = step1[CONF_ENTITY_ID]
    template = step1[CONF_WIDGET_TEMPLATE]
    raw_slug = (step1.get(CONF_SLUG) or "").strip()
    slug = (normalize_slug(raw_slug) if raw_slug else "") or sanitize_slug(entity_id)

    min_v, max_v = _coerce_gauge_range(user_input, is_gauge=template == WIDGET_TEMPLATE_GAUGE)

    stat_rows = _parse_widget_stat_rows(user_input.get(CONF_STAT_ROWS, ""), strict=True)
    if template == WIDGET_TEMPLATE_STAT_LIST and not stat_rows:
        raise vol.Invalid("stat_rows_required", path=[CONF_STAT_ROWS])

    poll_interval = int(user_input.get(CONF_WIDGET_POLL_INTERVAL, DEFAULT_WIDGET_POLL_INTERVAL))
    poll_interval = max(WIDGET_POLL_INTERVAL_MIN, min(WIDGET_POLL_INTERVAL_MAX, poll_interval))

    trigger = user_input.get(CONF_WIDGET_TRIGGER_MODE) or WIDGET_TRIGGER_EVENT
    if trigger not in WIDGET_TRIGGER_MODES:
        trigger = WIDGET_TRIGGER_EVENT

    value_scale = user_input.get(CONF_VALUE_SCALE) or DEFAULT_VALUE_SCALE
    if value_scale not in VALUE_SCALES:
        value_scale = DEFAULT_VALUE_SCALE

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
        CONF_VALUE_SCALE: value_scale,
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
        self._suggestion_offered: bool = False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.SubentryFlowResult:
        """Step 1: entity + template + slug."""
        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID]
            template = user_input.get(CONF_WIDGET_TEMPLATE, WIDGET_TEMPLATE_VALUE)

            # Suggest a better template if the user left the default
            if template == WIDGET_TEMPLATE_VALUE and not self._suggestion_offered:
                suggested = _suggest_widget_template(self.hass, entity_id)
                if suggested != WIDGET_TEMPLATE_VALUE:
                    self._suggestion_offered = True
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_widget_step1_schema(
                            defaults={
                                CONF_ENTITY_ID: entity_id,
                                CONF_WIDGET_TEMPLATE: suggested,
                                CONF_SLUG: user_input.get(CONF_SLUG, ""),
                            }
                        ),
                    )

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
            # stat_rows stay a list: the ObjectSelector row editor rehydrates from it directly.
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
        expand = {sec for sec, fs in WIDGET_SECTIONS.items() if set(errors) & (set(fs) - _WIDGET_TOPLEVEL)}
        schema = _widget_details_schema(entity_id, template, defaults=defaults, expand=expand)
        if errors and user_input is not None:
            # Re-fill the form with what the user just submitted instead of dropping it.
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="details",
            data_schema=schema,
            errors=errors,
            last_step=True,
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


def _parse_int_list(value: str, *, strict: bool = False) -> list[int]:
    """Parse '1,2,3' into [1, 2, 3].

    Lenient (default) silently skips non-integer tokens; strict (form paths)
    raises invalid_step_rows, since a dropped token shifts every later row by one.
    """
    result: list[int] = []
    for token in _parse_csv(value):
        try:
            result.append(int(token))
        except ValueError:
            if strict:
                raise vol.Invalid("invalid_step_rows", path=[CONF_STEP_ROWS]) from None
    return result


def _parse_float_list(value: str, *, strict: bool = False) -> list[float]:
    """Parse '1, 2.5, 3' into [1.0, 2.5, 3.0], skipping tokens float() rejects.

    "nan" and "inf" are not among them - float() takes both. Lenient (default)
    keeps them; _clean_step_weights drops them before they reach the server.
    Strict (form paths) rejects any unparseable, non-finite, or non-positive
    weight with invalid_step_weights - a weight is a relative step size, so zero
    or negative has no meaning.
    """
    result: list[float] = []
    for token in _parse_csv(value):
        try:
            parsed = float(token)
        except ValueError:
            if strict:
                raise vol.Invalid("invalid_step_weights", path=[CONF_STEP_WEIGHTS]) from None
            continue
        if strict and (not math.isfinite(parsed) or parsed <= 0):
            raise vol.Invalid("invalid_step_weights", path=[CONF_STEP_WEIGHTS])
        result.append(parsed)
    return result


def _parse_color_list(value: str, *, strict: bool = False) -> list[str]:
    """Parse 'red,,blue' into ['red', '', 'blue'], keeping empty entries.

    Positional, unlike _parse_csv: the server matches step_colors to steps by
    index and requires exactly total_steps entries, so dropping the empty token
    that leaves a step on the accent color would shift every later color by one
    and fail the length check. Strict (form paths) rejects a non-empty token that
    is neither a named color nor 6/8-digit hex with invalid_step_colors; blank
    tokens stay legal (positional slots).
    """
    if not value:
        return []
    tokens = [s.strip() for s in value.split(",")]
    if strict:
        for token in tokens:
            if token and not is_valid_color(token):
                raise vol.Invalid("invalid_step_colors", path=[CONF_STEP_COLORS])
    return tokens


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


def _kv_rows_to_map(raw: object, key_field: str, value_field: str) -> dict[str, str]:
    """Adapt a two-column row editor (or a legacy 'k=v, k2=v2' string) into a {k: v} dict.

    Each row is {key_field: k, value_field: v}; rows missing either side are skipped.
    The legacy string branch defers to _parse_state_labels so stored-string edge cases
    and non-form callers keep working; an already-stored dict passes through.
    """
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for row in raw:
            if not isinstance(row, dict):
                continue
            key = str(row.get(key_field, "") or "").strip()
            value = str(row.get(value_field, "") or "").strip()
            if key and value:
                result[key] = value
        return result
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return _parse_state_labels(raw)
    return {}


def _map_to_kv_rows(mapping: object, key_field: str, value_field: str) -> list[dict]:
    """Adapt a stored {k: v} dict into two-column editor rows for reconfigure prefill."""
    if not isinstance(mapping, dict):
        return []
    return [{key_field: k, value_field: v} for k, v in mapping.items()]


def _blank(value: object) -> bool:
    """True for a missing/empty editor cell (None or an all-whitespace string)."""
    return value is None or (isinstance(value, str) and not value.strip())


def _decompose_steps_rows(raw: object, total_steps: int, *, strict: bool = False) -> dict:
    """Split the unified steps-editor rows into the four stored step keys.

    Each row is one step in order: {label?, row?, weight?, color?}. Returns
    {step_labels: {str(i): label}, step_rows: [...], step_weights: [...],
    step_colors: [...]}. In strict (form) mode the row count must be 0 or exactly
    total_steps (step_length_mismatch otherwise). row/weight are all-or-none across
    rows - a partial column is dropped, since the server needs exactly total_steps
    entries; color keeps a positional blank for each uncolored row.
    """
    empty = {CONF_STEP_LABELS: {}, CONF_STEP_ROWS: [], CONF_STEP_WEIGHTS: [], CONF_STEP_COLORS: []}
    if not isinstance(raw, list):
        return empty
    rows = [r for r in raw if isinstance(r, dict)]
    if not rows:
        return empty
    if strict and len(rows) != total_steps:
        raise vol.Invalid("step_length_mismatch", path=[CONF_STEPS_EDITOR])

    labels: dict[str, str] = {}
    row_heights: list[int] = []
    weights: list[float] = []
    colors: list[str] = []
    have_rows = True
    have_weights = True
    have_color = False

    for index, row in enumerate(rows, start=1):
        label = str(row.get(CONF_LABEL, "") or "").strip()
        if label:
            labels[str(index)] = label

        row_val = row.get("row")
        if _blank(row_val):
            have_rows = False
        else:
            try:
                row_heights.append(int(float(row_val)))
            except (TypeError, ValueError):
                if strict:
                    raise vol.Invalid("invalid_step_rows", path=[CONF_STEPS_EDITOR]) from None
                have_rows = False

        weight_val = row.get("weight")
        if _blank(weight_val):
            have_weights = False
        else:
            try:
                weight = float(weight_val)
            except (TypeError, ValueError):
                if strict:
                    raise vol.Invalid("invalid_step_weights", path=[CONF_STEPS_EDITOR]) from None
                have_weights = False
            else:
                if strict and (not math.isfinite(weight) or weight <= 0):
                    raise vol.Invalid("invalid_step_weights", path=[CONF_STEPS_EDITOR])
                weights.append(weight)

        color = str(row.get("color", "") or "").strip()
        if color:
            if strict and not is_valid_color(color):
                raise vol.Invalid("invalid_step_colors", path=[CONF_STEPS_EDITOR])
            have_color = True
        colors.append(color)

    return {
        CONF_STEP_LABELS: labels,
        CONF_STEP_ROWS: row_heights if have_rows else [],
        CONF_STEP_WEIGHTS: weights if have_weights else [],
        CONF_STEP_COLORS: colors if have_color else [],
    }


def _compose_steps_rows(config: dict) -> list[dict]:
    """Compose unified steps-editor rows from the four stored step keys.

    One row per step, ordered: label from step_labels[str(i)], row/weight/color
    positional. Returns [] when none of the four keys hold data (nothing to edit).
    The row count covers every configured position so nothing stored is hidden.
    """
    labels = config.get(CONF_STEP_LABELS) or {}
    rows = config.get(CONF_STEP_ROWS) or []
    weights = config.get(CONF_STEP_WEIGHTS) or []
    colors = config.get(CONF_STEP_COLORS) or []
    if not (labels or rows or weights or colors):
        return []
    label_max = 0
    if isinstance(labels, dict):
        label_max = max((int(k) for k in labels if str(k).isdigit()), default=0)
    total = max(
        int(config.get(CONF_TOTAL_STEPS) or DEFAULT_TOTAL_STEPS),
        len(rows),
        len(weights),
        len(colors),
        label_max,
    )
    out: list[dict] = []
    for i in range(1, total + 1):
        row: dict = {}
        label = labels.get(str(i)) if isinstance(labels, dict) else None
        if label:
            row[CONF_LABEL] = label
        if i - 1 < len(rows):
            row["row"] = rows[i - 1]
        if i - 1 < len(weights):
            row["weight"] = weights[i - 1]
        if i - 1 < len(colors) and colors[i - 1]:
            row["color"] = colors[i - 1]
        out.append(row)
    return out


def _steps_fields_from_input(user_input: dict, total_steps: int, *, strict: bool) -> dict:
    """Resolve the four stored step keys from the form editor or legacy input.

    The steps form submits one row per step under CONF_STEPS_EDITOR, decomposed here.
    When that key is absent (non-form callers, stored-string edge cases) the four
    legacy keys are read leniently instead, so older callers keep working.
    """
    if CONF_STEPS_EDITOR in user_input:
        return _decompose_steps_rows(user_input[CONF_STEPS_EDITOR], total_steps, strict=strict)
    labels_raw = user_input.get(CONF_STEP_LABELS, "")
    return {
        CONF_STEP_LABELS: labels_raw if isinstance(labels_raw, dict) else _parse_state_labels(labels_raw),
        CONF_STEP_ROWS: _legacy_step_list(user_input.get(CONF_STEP_ROWS, ""), _parse_int_list),
        CONF_STEP_WEIGHTS: _legacy_step_list(user_input.get(CONF_STEP_WEIGHTS, ""), _parse_float_list),
        CONF_STEP_COLORS: _legacy_step_list(user_input.get(CONF_STEP_COLORS, ""), _parse_color_list),
    }


def _legacy_step_list(raw: object, parser) -> list:
    """Parse one legacy step list leniently, passing an already-stored list through."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        return parser(raw)
    return []


def _is_number(value: object) -> bool:
    """True for a real int/float (a JSON bool is not a number here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _strict_threshold(item: object) -> dict:
    """Validate + normalize one timeline threshold row dict, raising on any problem.

    The value is required and must parse to a finite number; the optional color must
    be a named colour or hex, and the optional label is capped at THRESHOLD_LABEL_MAX.
    Returns a threshold carrying only the non-empty storage keys.
    """
    if not isinstance(item, dict):
        raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS])
    raw_value = item.get("value")
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS])
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS]) from None
    if not math.isfinite(value):
        raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS])
    color = str(item.get("color", "") or "").strip()
    label = str(item.get("label", "") or "").strip()
    if color and not is_valid_color(color):
        raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS])
    if len(label) > THRESHOLD_LABEL_MAX:
        raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS])
    threshold: dict = {"value": value}
    if color:
        threshold["color"] = color
    if label:
        threshold["label"] = label
    return threshold


def _parse_thresholds(raw: object, *, strict: bool = False) -> list[dict]:
    """Parse timeline thresholds from a row-editor list (or the legacy comma string).

    From the form each row is a dict with the storage keys: ``{value, color?, label?}``.
    The legacy string form ('value[:color[:label]], ...', e.g. '25:red:Hot, 20') stays
    accepted for stored-string edge cases and non-form callers. Capped at THRESHOLDS_MAX.
    Lenient (default) skips a non-numeric value and truncates past the cap; strict
    (form paths) raises invalid_threshold / too_many_thresholds (and applies the colour
    rule and label length cap) instead.
    """
    if isinstance(raw, list):
        if not strict:
            kept = [t for t in raw if isinstance(t, dict) and _is_number(t.get("value"))]
            return kept[:THRESHOLDS_MAX]
        thresholds = [_strict_threshold(item) for item in raw]
        if len(thresholds) > THRESHOLDS_MAX:
            raise vol.Invalid("too_many_thresholds", path=[CONF_THRESHOLDS])
        return thresholds
    if not isinstance(raw, str) or not raw.strip():
        return []
    result: list[dict] = []
    for entry in raw.split(","):
        parts = [p.strip() for p in entry.strip().split(":")]
        if not parts or not parts[0]:
            if strict and entry.strip():
                raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS])
            continue
        try:
            threshold: dict = {"value": float(parts[0])}
        except ValueError:
            if strict:
                raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS]) from None
            continue
        if len(parts) > 1 and parts[1]:
            threshold["color"] = parts[1]
        if len(parts) > 2 and parts[2]:
            threshold["label"] = parts[2]
        result.append(threshold)
    if strict and len(result) > THRESHOLDS_MAX:
        raise vol.Invalid("too_many_thresholds", path=[CONF_THRESHOLDS])
    return result[:THRESHOLDS_MAX]
