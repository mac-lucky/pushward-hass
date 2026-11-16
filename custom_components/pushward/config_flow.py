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
from homeassistant.core import HomeAssistant, State, callback, valid_entity_id
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
    TimeSelector,
)
from homeassistant.helpers.typing import UNDEFINED

from .activity_manager import run_while_rows
from .api import PushWardApiClient, PushWardApiError, PushWardAuthError
from .const import (
    ACTIVITY_TTL_MAX,
    ACTIVITY_TTL_MIN,
    ACTIVITY_UNIT_MAX,
    APP_STORE_URL,
    BOARD_MAX_TILES,
    BOARD_TILE_ICON_MAX,
    BOARD_TILE_LABEL_MAX,
    BOARD_TILE_UNIT_MAX,
    CALENDAR_LEAD_MINUTES_MAX,
    CALENDAR_SUBTITLE_MAX,
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ACCENT_FROM_PROGRESS,
    CONF_ACTIVITY_NAME,
    CONF_ALARM,
    CONF_BACKGROUND_COLOR,
    CONF_BACKGROUND_COLOR_ATTRIBUTE,
    CONF_BATTERY_DEVICES,
    CONF_CALENDAR_LEAD_MINUTES,
    CONF_CALENDAR_SUBTITLE_BEFORE,
    CONF_CALENDAR_SUBTITLE_DURING,
    CONF_CHARGING_ENTITY,
    CONF_COMPACT_LABEL,
    CONF_COMPACT_TRAILING_ICON,
    CONF_COMPACT_TRAILING_LABEL,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_CURRENT_STEP_ENTITY,
    CONF_DECIMALS,
    CONF_DISMISSAL_TTL,
    CONF_E2E_KEY,
    CONF_END_DATE_ATTRIBUTE,
    CONF_END_STATES,
    CONF_ENDED_TTL,
    CONF_ENTITY_ID,
    CONF_EXPIRED_TEXT,
    CONF_FIRED_AT_ATTRIBUTE,
    CONF_FIRED_AT_ENTITY,
    CONF_FLOW_NODES,
    CONF_FLOW_SLOT,
    CONF_HEATMAP_AGGREGATE,
    CONF_HEATMAP_WEEKS,
    CONF_HISTORY_PERIOD,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_ICON_MAP,
    CONF_ICON_MAP_ENTITY,
    CONF_IMAGE_SHAPE,
    CONF_IMAGE_THUMBHASH,
    CONF_IMAGE_URL,
    CONF_INTEGRATION_KEY,
    CONF_LABEL,
    CONF_LABEL_ATTRIBUTE,
    CONF_LEVEL_ENTITY,
    CONF_LIVE_PROGRESS,
    CONF_LOG_COLUMNS,
    CONF_LOG_LEVEL_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MEDIA_CONTROLS,
    CONF_MEDIA_FAVORITE_SCRIPT,
    CONF_MEDIA_TOKEN,
    CONF_MIN_VALUE,
    CONF_NODE_NAME,
    CONF_PRIMARY_SERIES,
    CONF_PRIORITY,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_PROGRESS_ENTITY,
    CONF_PROGRESS_MODE,
    CONF_REMAINING_TIME_ATTR,
    CONF_REMAINING_TIME_ENTITY,
    CONF_REMOVE_ON_END,
    CONF_RUN_WHILE,
    CONF_RUN_WHILE_STATES,
    CONF_SCALE,
    CONF_SCHEDULE_ATTRIBUTES,
    CONF_SCHEDULE_HIGH_MIN,
    CONF_SCHEDULE_LOW_MAX,
    CONF_SCHEDULE_START_KEY,
    CONF_SCHEDULE_VALUE_KEY,
    CONF_SECONDARY_URL,
    CONF_SECONDARY_URL_FOREGROUND,
    CONF_SECONDARY_URL_TITLE,
    CONF_SERIES,
    CONF_SERIES_ATTRIBUTE,
    CONF_SERIES_ENTITIES,
    CONF_SERVER_URL,
    CONF_SEVERITY,
    CONF_SEVERITY_LABEL,
    CONF_SLUG,
    CONF_SMOOTHING,
    CONF_SNOOZE_SECONDS,
    CONF_SOUND,
    CONF_STALE_TTL,
    CONF_START_DATE_ATTRIBUTE,
    CONF_START_STATES,
    CONF_STAT_ROWS,
    CONF_STATE_LABELS,
    CONF_STATE_TEXT_ATTRIBUTE,
    CONF_STATE_TEXT_ENTITY,
    CONF_STATE_TEXT_FORMAT,
    CONF_STATE_TEXT_PATTERN,
    CONF_STEP_COLORS,
    CONF_STEP_LABELS,
    CONF_STEP_ROWS,
    CONF_STEP_WEIGHTS,
    CONF_STEPS_EDITOR,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_SUBTITLE_COLUMNS,
    CONF_SUBTITLE_ENTITY,
    CONF_SUBTITLE_TIMER_ATTRIBUTE,
    CONF_SUBTITLE_TIMER_ENTITY,
    CONF_SUBTITLE_TIMER_STYLE,
    CONF_TAP_ACTION_FOREGROUND,
    CONF_TAP_ACTION_URL,
    CONF_TEMPLATE,
    CONF_TEXT_COLOR,
    CONF_TEXT_COLOR_ATTRIBUTE,
    CONF_THRESHOLDS,
    CONF_TILE_COLOR,
    CONF_TILE_URL_ACTION,
    CONF_TILES,
    CONF_TODO_ALL_DAY_TIME,
    CONF_TODO_DONE_BUTTON,
    CONF_TODO_LEVEL,
    CONF_TODO_MAX_SCHEDULED,
    CONF_TODO_OFFSET_MINUTES,
    CONF_TODO_RETRACT,
    CONF_TOTAL_ENTITY,
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
    CONF_WIDGET_BATTERY_SORT,
    CONF_WIDGET_DENSITY,
    CONF_WIDGET_NAME,
    CONF_WIDGET_POLL_INTERVAL,
    CONF_WIDGET_STALE_AFTER,
    CONF_WIDGET_STYLE,
    CONF_WIDGET_TEMPLATE,
    CONF_WIDGET_TRIGGER_MODE,
    DANGEROUS_URL_SCHEMES,
    DEFAULT_CALENDAR_LEAD_MINUTES,
    DEFAULT_DECIMALS,
    DEFAULT_HEATMAP_WEEKS,
    DEFAULT_HISTORY_PERIOD,
    DEFAULT_IMAGE_SHAPE,
    DEFAULT_MAX_VALUE,
    DEFAULT_MEDIA_CONTROLS,
    DEFAULT_MIN_VALUE,
    DEFAULT_PRIORITY,
    DEFAULT_SCALE,
    DEFAULT_SCHEDULE_START_KEY,
    DEFAULT_SCHEDULE_VALUE_KEY,
    DEFAULT_SERVER_URL,
    DEFAULT_SEVERITY,
    DEFAULT_STATE_TEXT_FORMAT,
    DEFAULT_SUBTITLE_TIMER_STYLE,
    DEFAULT_TAP_ACTION_FOREGROUND,
    DEFAULT_TODO_ALL_DAY_TIME,
    DEFAULT_TODO_DONE_BUTTON,
    DEFAULT_TODO_LEVEL,
    DEFAULT_TODO_MAX_SCHEDULED,
    DEFAULT_TODO_OFFSET_MINUTES,
    DEFAULT_TODO_RETRACT,
    DEFAULT_TOTAL_STEPS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_VALUE_SCALE,
    DEFAULT_WIDGET_POLL_INTERVAL,
    DEFAULT_WIDGET_TREND_PERIOD,
    DISMISSAL_TTL_MAX,
    DISMISSAL_TTL_MIN,
    DOMAIN,
    FLOW_SLOT_INPUT,
    FLOW_SLOTS,
    HEATMAP_AGGREGATE_CHANGE,
    HEATMAP_AGGREGATE_MEAN,
    HEATMAP_AGGREGATES,
    HISTORY_PERIOD_MAX,
    IMAGE_SHAPES,
    IMAGE_TEMPLATES,
    IMAGE_THUMBHASH_MAX,
    IMAGE_URL_MAX,
    LIVE_PROGRESS_TEMPLATES,
    LOG_COLUMN_LABEL_MAX,
    LOG_MAX_COLUMNS,
    MAX_COMPACT_LABEL_LEN,
    MAX_COMPACT_TRAILING_LABEL_LEN,
    MAX_ICON_LEN,
    MAX_LONG_TEXT_LEN,
    MAX_SEVERITY_LABEL_LEN,
    MAX_SLUG_LEN,
    MAX_TAP_ACTION_TITLE_LEN,
    MAX_TEXT_LEN,
    MAX_URL_LEN,
    NAMED_COLORS,
    PLAY_STORE_URL,
    PRIORITY_MAX,
    PRIORITY_MIN,
    PROGRESS_MODE_DIRECT,
    PROGRESS_MODES,
    RUN_WHILE_MAX,
    SCALES,
    SEVERITIES,
    SNOOZE_SECONDS_MAX,
    SNOOZE_SECONDS_MIN,
    SOUNDS,
    STATE_TEXT_FORMATS,
    STATE_TEXT_PATTERN_MAX,
    STEP_LABEL_MAX,
    STEP_ROW_MAX,
    STEP_ROW_MIN,
    SUBENTRY_TYPE_ENTITY,
    SUBENTRY_TYPE_TODO,
    SUBENTRY_TYPE_WIDGET,
    TEMPLATES,
    THRESHOLD_LABEL_MAX,
    THRESHOLDS_MAX,
    TIMELINE_MAX_SERIES,
    TIMELINE_SERIES_LABEL_MAX,
    TIMER_STYLES,
    TODO_LEVELS,
    TODO_MAX_SCHEDULED_LIMIT,
    TODO_OFFSET_MAX_MINUTES,
    TOTAL_STEPS_MAX,
    UPDATE_INTERVAL_MIN,
    VALUE_SCALES,
    WARNING_THRESHOLD_MAX,
    WIDGET_BATTERY_SORTS,
    WIDGET_EXPIRED_TEXT_MAX,
    WIDGET_GROUP_TEMPLATES,
    WIDGET_HEATMAP_WEEKS_MAX,
    WIDGET_LABEL_MAX,
    WIDGET_LOOK_UNSET,
    WIDGET_LOOKS,
    WIDGET_MAX_BATTERY_DEVICES,
    WIDGET_MAX_FLOW_INPUTS,
    WIDGET_MAX_STAT_ROWS,
    WIDGET_NAME_MAX,
    WIDGET_NODE_ICON_MAX,
    WIDGET_NODE_NAME_MAX,
    WIDGET_POLL_INTERVAL_MAX,
    WIDGET_POLL_INTERVAL_MIN,
    WIDGET_SEVERITIES,
    WIDGET_STALE_AFTER_MAX,
    WIDGET_STALE_AFTER_MIN,
    WIDGET_STAT_LABEL_MAX,
    WIDGET_STAT_UNIT_MAX,
    WIDGET_TEMPLATE_BATTERY,
    WIDGET_TEMPLATE_COUNTDOWN,
    WIDGET_TEMPLATE_FLOW,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_HEATMAP,
    WIDGET_TEMPLATE_PROGRESS,
    WIDGET_TEMPLATE_SCHEDULE,
    WIDGET_TEMPLATE_STAT_LIST,
    WIDGET_TEMPLATE_STATUS,
    WIDGET_TEMPLATE_TREND,
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TEMPLATES,
    WIDGET_TRIGGER_EVENT,
    WIDGET_TRIGGER_MODES,
    WIDGET_UNIT_MAX,
    normalize_slug,
    validate_image_url,
    validate_thumbhash,
    widget_look_keys,
    widget_looks,
)
from .content_mapper import get_domain_defaults, is_valid_color, sanitize_slug
from .e2e import E2EError, key_id, parse_key
from .media_control import new_control_token
from .recorder_history import async_statistics_info, statistics_has_mean, statistics_has_min_max

_LOGGER = logging.getLogger(__name__)

_INTEGRATION_KEY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_INTEGRATION_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }
)

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
            CONF_TILE_COLOR: {"label": "Color", "selector": _ROW_NAMED_COLOR},
            CONF_TILE_URL_ACTION: {"label": "URL", "selector": _ROW_URL},
        },
        multiple=True,
        label_field=CONF_LABEL,
        description_field=CONF_ENTITY_ID,
    )
)

# Renders the row's value as a self-updating timer when the value parses as a
# date. custom_value so an existing row without one submits unchanged.
_ROW_TIMER_STYLE = {
    "select": {
        "options": list(TIMER_STYLES),
        "custom_value": True,
        "mode": "dropdown",
        "translation_key": "timer_style",
    }
}

_WIDGET_STAT_ROWS_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_LABEL: {"label": "Label", "selector": _ROW_TEXT},
            CONF_ENTITY_ID: {"label": "Entity", "selector": _ROW_TEXT},
            CONF_VALUE_ATTRIBUTE: {"label": "Attribute", "selector": _ROW_TEXT},
            CONF_UNIT: {"label": "Unit", "selector": _ROW_TEXT},
            "timer_style": {"label": "Timer", "selector": _ROW_TIMER_STYLE},
        },
        multiple=True,
        label_field=CONF_LABEL,
        description_field=CONF_ENTITY_ID,
    )
)

# Battery rings. Entity, charging entity and the level attribute are text fields,
# not entity/attribute selectors - see the note on _BOARD_TILES_SELECTOR above.
_BATTERY_DEVICES_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_NODE_NAME: {"label": "Name", "selector": _ROW_TEXT},
            CONF_ENTITY_ID: {"label": "Entity", "selector": _ROW_TEXT},
            CONF_VALUE_ATTRIBUTE: {"label": "Attribute", "selector": _ROW_TEXT},
            CONF_CHARGING_ENTITY: {"label": "Charging entity", "selector": _ROW_TEXT},
            CONF_ICON: {"label": "Icon", "selector": _ROW_ICON},
            "color": {"label": "Color", "selector": _ROW_NAMED_COLOR},
        },
        multiple=True,
        label_field=CONF_NODE_NAME,
        description_field=CONF_ENTITY_ID,
    )
)

_ROW_FLOW_SLOT = {
    "select": {
        "options": list(FLOW_SLOTS),
        "custom_value": True,
        "mode": "dropdown",
        "translation_key": "flow_slot",
    }
}

# Flow nodes. `slot` decides where the node renders: up to three inputs, one each
# of output / storage / exchange.
_FLOW_NODES_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_FLOW_SLOT: {"label": "Slot", "selector": _ROW_FLOW_SLOT},
            CONF_NODE_NAME: {"label": "Name", "selector": _ROW_TEXT},
            CONF_ENTITY_ID: {"label": "Entity", "selector": _ROW_TEXT},
            CONF_VALUE_ATTRIBUTE: {"label": "Attribute", "selector": _ROW_TEXT},
            CONF_TOTAL_ENTITY: {"label": "Total entity", "selector": _ROW_TEXT},
            CONF_LEVEL_ENTITY: {"label": "Level entity", "selector": _ROW_TEXT},
            CONF_ICON: {"label": "Icon", "selector": _ROW_ICON},
            "color": {"label": "Color", "selector": _ROW_NAMED_COLOR},
        },
        multiple=True,
        label_field=CONF_FLOW_SLOT,
        description_field=CONF_ENTITY_ID,
    )
)

# Timeline series entities (each row binds a separate entity as a named line).
# Text field, not an entity selector - see the note on _BOARD_TILES_SELECTOR above.
_ROW_NUMBER_ANY = {"number": {"mode": "box", "step": "any"}}

_SERIES_ENTITIES_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_LABEL: {"label": "Label", "selector": _ROW_TEXT},
            CONF_ENTITY_ID: {"label": "Entity", "selector": _ROW_TEXT},
            CONF_SERIES_ATTRIBUTE: {"label": "Attribute", "selector": _ROW_TEXT},
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
# unit. Entity is a text field, not an entity selector - see the note on
# _BOARD_TILES_SELECTOR above. Source semantics are governed by the parse layer:
# empty entity + attribute = a tracked-entity attribute; entity + empty attribute =
# another entity's state; both = that entity's attribute.
_LOG_COLUMNS_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_LABEL: {"label": "Label", "selector": _ROW_TEXT},
            CONF_ENTITY_ID: {"label": "Entity", "selector": _ROW_TEXT},
            CONF_SERIES_ATTRIBUTE: {"label": "Attribute", "selector": _ROW_TEXT},
            CONF_UNIT: {"label": "Unit", "selector": _ROW_TEXT},
        },
        multiple=True,
        label_field=CONF_LABEL,
        description_field=CONF_ENTITY_ID,
    )
)

# Run-while conditions: an entity and the comma-separated states it has to be in.
# Text fields, not entity/state selectors - see the note on _BOARD_TILES_SELECTOR above.
# Stored with the states split into a list; reconfigure joins them back.
_RUN_WHILE_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            CONF_ENTITY_ID: {"label": "Entity", "selector": _ROW_TEXT},
            CONF_RUN_WHILE_STATES: {"label": "States", "selector": _ROW_TEXT},
        },
        multiple=True,
        label_field=CONF_ENTITY_ID,
        description_field=CONF_RUN_WHILE_STATES,
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

# Icon per state, the same two-column editor: stored as {state: icon} like state_labels.
_ICON_MAP_SELECTOR = ObjectSelector(
    ObjectSelectorConfig(
        fields={
            "state": {"label": "State", "selector": _ROW_TEXT},
            CONF_ICON: {"label": "Icon", "selector": _ROW_ICON},
        },
        multiple=True,
        label_field="state",
        description_field=CONF_ICON,
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
# keys, so the storage shape and content_mapper are untouched. Row height is a
# STEP_ROW_MIN-STEP_ROW_MAX number; weight is any positive number; color is a
# named/hex value (blank leaves that step on the accent color). The row count must be
# 0 or exactly total_steps. The number box carries no min/max: bounding it at the
# schema layer surfaces a raw untranslated voluptuous error for a legacy out-of-range
# value, so the STEP_ROW range is enforced in _decompose_steps_rows instead (translated
# invalid_step_rows), matching the timeline-threshold row-editor precedent.
_ROW_STEP_NUMBER = {"number": {"mode": "box", "step": 1}}

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


# Free-form numeric box (any step) reused by the gauge min/max fields on both the
# entity and widget detail forms.
_NUMBER_BOX_ANY = NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any"))


def _object_rows_key(conf_key: str, current: dict, *, required: bool) -> vol.Marker:
    """Build the vol key for an ObjectSelector row list.

    The stored list becomes the field default so it rehydrates verbatim on
    reconfigure (and satisfies the Required marker); an add flow starts empty.
    """
    stored = current.get(conf_key)
    default = stored if isinstance(stored, list) else []
    marker = vol.Required if required else vol.Optional
    return marker(conf_key, default=default)


# The tap-actions section is the same on both detail forms: the surface-wide tap
# target plus the two button slots. Shared so the two can't drift apart -
# _section_errors reads these to decide which section shows an error.
_TAP_ACTION_SECTION_FIELDS: tuple[str, ...] = (
    CONF_TAP_ACTION_URL,
    CONF_TAP_ACTION_FOREGROUND,
    CONF_URL,
    CONF_URL_FOREGROUND,
    CONF_URL_TITLE,
    CONF_SECONDARY_URL,
    CONF_SECONDARY_URL_FOREGROUND,
    CONF_SECONDARY_URL_TITLE,
)


# Collapsible-section layout for the two subentry detail forms. Each field a
# template renders lands in exactly one section here or stays top-level (the
# tables below); the schema builders filter these per template and drop a section
# that has no field for the current template. Translations live under
# step.details.sections.<key>.data[_description], with sections.<key>.name for the
# header. A field key can be top-level for one template and sectioned for another
# (remaining_time on countdown vs generic/steps) - its label then needs an entry
# in BOTH step.details.data and the section's data block.
class _TrimmedLength(vol.Length):
    """vol.Length that trims the string first.

    A trailing space on a label sitting at the cap would otherwise fail the form.
    A subclass rather than a str.strip step in vol.All because HA renders the form
    from voluptuous_serialize, which cannot convert a bare callable.
    """

    def __call__(self, v):
        if isinstance(v, str):
            v = v.strip()
        return super().__call__(v)


ENTITY_SECTIONS: dict[str, tuple[str, ...]] = {
    "data_sources": (
        CONF_PROGRESS_ENTITY,
        CONF_PROGRESS_ATTRIBUTE,
        CONF_PROGRESS_MODE,
        CONF_REMAINING_TIME_ENTITY,
        CONF_REMAINING_TIME_ATTR,
        CONF_LIVE_PROGRESS,
        CONF_VALUE_ENTITY,
        CONF_VALUE_ATTRIBUTE,
        CONF_FIRED_AT_ENTITY,
        CONF_FIRED_AT_ATTRIBUTE,
        CONF_SUBTITLE_ENTITY,
        CONF_SUBTITLE_ATTRIBUTE,
        CONF_SUBTITLE_COLUMNS,
        CONF_STATE_TEXT_ENTITY,
        CONF_STATE_TEXT_ATTRIBUTE,
        CONF_STATE_TEXT_FORMAT,
        CONF_STATE_TEXT_PATTERN,
        CONF_SERIES,
        CONF_LOG_LEVEL_ATTRIBUTE,
    ),
    "steps_options": (CONF_STEPS_EDITOR,),
    "media_options": (CONF_MEDIA_CONTROLS, CONF_MEDIA_FAVORITE_SCRIPT),
    "image_options": (CONF_IMAGE_URL, CONF_IMAGE_SHAPE, CONF_IMAGE_THUMBHASH),
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
        CONF_ICON_MAP,
        CONF_ICON_MAP_ENTITY,
        CONF_PRIORITY,
        CONF_SOUND,
        CONF_UPDATE_INTERVAL,
        CONF_STATE_LABELS,
        CONF_COMPACT_LABEL,
        CONF_COMPACT_TRAILING_LABEL,
        CONF_COMPACT_TRAILING_ICON,
    ),
    "colors": (
        CONF_ACCENT_COLOR,
        CONF_ACCENT_COLOR_ATTRIBUTE,
        CONF_ACCENT_FROM_PROGRESS,
        CONF_BACKGROUND_COLOR,
        CONF_BACKGROUND_COLOR_ATTRIBUTE,
        CONF_TEXT_COLOR,
        CONF_TEXT_COLOR_ATTRIBUTE,
    ),
    "tap_actions": _TAP_ACTION_SECTION_FIELDS,
    "advanced": (
        CONF_REMOVE_ON_END,
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
    # media reads everything it shows off the player itself; the two options it
    # does have are both in the media_options section.
    "media": (),
}

WIDGET_SECTIONS: dict[str, tuple[str, ...]] = {
    "look": (*widget_look_keys(CONF_WIDGET_STYLE), *widget_look_keys(CONF_WIDGET_DENSITY)),
    "display_options": (
        CONF_LABEL,
        CONF_LABEL_ATTRIBUTE,
        CONF_SUBTITLE_ATTRIBUTE,
        CONF_SUBTITLE_TIMER_ENTITY,
        CONF_SUBTITLE_TIMER_ATTRIBUTE,
        CONF_SUBTITLE_TIMER_STYLE,
        CONF_ICON,
        CONF_ICON_ATTRIBUTE,
        CONF_WIDGET_BATTERY_SORT,
    ),
    "colors": (
        CONF_ACCENT_COLOR,
        CONF_ACCENT_COLOR_ATTRIBUTE,
        CONF_BACKGROUND_COLOR,
        CONF_TEXT_COLOR,
    ),
    "tap_actions": _TAP_ACTION_SECTION_FIELDS,
    "refresh": (
        CONF_WIDGET_TRIGGER_MODE,
        CONF_WIDGET_POLL_INTERVAL,
        CONF_WIDGET_STALE_AFTER,
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
        CONF_HISTORY_PERIOD,
        CONF_START_DATE_ATTRIBUTE,
        CONF_END_DATE_ATTRIBUTE,
        CONF_EXPIRED_TEXT,
        CONF_BATTERY_DEVICES,
        CONF_SCHEDULE_ATTRIBUTES,
        CONF_SCHEDULE_START_KEY,
        CONF_SCHEDULE_VALUE_KEY,
        CONF_SCHEDULE_LOW_MAX,
        CONF_SCHEDULE_HIGH_MIN,
        CONF_FLOW_NODES,
        CONF_HEATMAP_WEEKS,
        CONF_HEATMAP_AGGREGATE,
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


def _section_errors(
    sections_def: dict[str, tuple[str, ...]],
    toplevel: set[str] | frozenset[str],
    errors: dict[str, str],
) -> dict[str, str]:
    """Key each error of a sectioned field by its section name, keeping the code.

    HA's form shows an error only under a top-level schema item, and a section passes
    none down to its fields, so a field-keyed error inside one would never show.
    base and top-level fields keep their key; one error per section is enough.
    """
    shown: dict[str, str] = {}
    for field, code in errors.items():
        sec = next((sec for sec, fs in sections_def.items() if field in fs and field not in toplevel), field)
        shown.setdefault(sec, code)
    return shown


async def _validate_integration_key(
    hass: HomeAssistant,
    key: str,
    context: str,
    server_url: str = DEFAULT_SERVER_URL,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Validate an integration key against the PushWard API.

    Returns an error dict (empty on success) and the account's /auth/me profile, whose
    id keys the config entry: one entry per PushWard account.
    """
    session = async_get_clientsession(hass)
    client = PushWardApiClient(session, server_url, key)
    try:
        me = await client.get_me()
    except PushWardAuthError:
        return {"base": "invalid_auth"}, {}
    except (PushWardApiError, aiohttp.ClientError, TimeoutError, OSError) as err:
        _LOGGER.warning("PushWard %s failed: %s", context, err)
        return {"base": "cannot_connect"}, {}
    if not me.get("id"):
        _LOGGER.warning("PushWard %s failed: /auth/me returned no account id", context)
        return {"base": "cannot_connect"}, {}
    return {}, me


def _account_title(me: Mapping[str, Any]) -> str:
    """Entry title naming the account, so entries for several accounts tell apart."""
    key = me.get("integration_key")
    name = me.get("nickname") or (key.get("name") if isinstance(key, Mapping) else None)
    return f"PushWard ({name})" if name else "PushWard"


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


def _is_gauge_like(state_obj: State, domain: str) -> bool:
    """True when a sensor/number entity reads as a gauge.

    A gauge-friendly entity has a measurement/total state_class or one of the
    gauge device classes. Shared by both template suggesters.
    """
    if domain not in ("sensor", "number"):
        return False
    attrs = state_obj.attributes
    return (
        attrs.get("state_class") in ("measurement", "total") or attrs.get("device_class", "") in _GAUGE_DEVICE_CLASSES
    )


def _suggest_template(hass: HomeAssistant | None, entity_id: str) -> str:
    """Suggest the best template for an entity based on domain/device_class/state_class."""
    if not entity_id or hass is None:
        return "generic"

    domain = _entity_domain(entity_id)

    if domain in ("timer", "calendar"):
        return "countdown"
    if domain == "light":
        return "gauge"
    if domain == "media_player":
        return "media"

    state_obj = hass.states.get(entity_id)
    if state_obj is None:
        return "generic"

    if _is_gauge_like(state_obj, domain):
        return "gauge"

    return "generic"


# Domains whose primary state reads as an on/off-style status widget.
_STATUS_WIDGET_DOMAINS = frozenset({"binary_sensor", "lock", "switch", "cover"})
# Domains whose state or attributes are a point in time, not a measurement.
_COUNTDOWN_WIDGET_DOMAINS = frozenset({"timer", "input_datetime", "calendar"})


def _suggest_widget_template(hass: HomeAssistant | None, entity_id: str) -> str:
    """Suggest the best widget template for an entity based on domain/device_class/state_class."""
    if not entity_id or hass is None:
        return WIDGET_TEMPLATE_VALUE

    domain = _entity_domain(entity_id)
    if domain in _COUNTDOWN_WIDGET_DOMAINS:
        return WIDGET_TEMPLATE_COUNTDOWN
    if domain in _STATUS_WIDGET_DOMAINS:
        return WIDGET_TEMPLATE_STATUS

    state_obj = hass.states.get(entity_id)
    if state_obj is None:
        return WIDGET_TEMPLATE_VALUE

    device_class = state_obj.attributes.get("device_class")
    if device_class == "timestamp":
        return WIDGET_TEMPLATE_COUNTDOWN
    if device_class == "battery":
        return WIDGET_TEMPLATE_BATTERY

    if _is_gauge_like(state_obj, domain):
        return WIDGET_TEMPLATE_GAUGE

    return WIDGET_TEMPLATE_VALUE


# Per button slot: (url key, foreground key, title key), rendered in that order.
_BUTTON_FIELD_KEYS: tuple[tuple[str, str, str], ...] = (
    (CONF_URL, CONF_URL_FOREGROUND, CONF_URL_TITLE),
    (CONF_SECONDARY_URL, CONF_SECONDARY_URL_FOREGROUND, CONF_SECONDARY_URL_TITLE),
)


def _add_button_action_fields(fields: dict, d: dict) -> None:
    """Append the two button slots to an ordered detail-form ``fields`` mapping.

    Shared by both detail forms so their button rows cannot drift: _sectioned_schema
    partitions on the field name and preserves insertion order, so the same six
    fields land in the same order inside each form's tap_actions section.
    """
    for url_key, foreground_key, title_key in _BUTTON_FIELD_KEYS:
        fields[_attr_suggest_key(url_key, d)] = vol.All(str, vol.Length(max=MAX_URL_LEN))
        fields[vol.Optional(foreground_key, default=d.get(foreground_key, DEFAULT_TAP_ACTION_FOREGROUND))] = (
            BooleanSelector()
        )
        fields[_attr_suggest_key(title_key, d)] = vol.All(str, vol.Length(max=MAX_TAP_ACTION_TITLE_LEN))


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

    ended_ttl_key = _opt_number_key(CONF_ENDED_TTL, d)
    stale_ttl_key = _opt_number_key(CONF_STALE_TTL, d)
    dismissal_ttl_key = _opt_number_key(CONF_DISMISSAL_TTL, d)

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
    if domain == "calendar":
        # No min/max on the box: _parse_calendar_lead_minutes checks the range with a
        # translated error, where a bounded selector would fail with a raw one.
        fields[
            vol.Optional(
                CONF_CALENDAR_LEAD_MINUTES,
                default=d.get(CONF_CALENDAR_LEAD_MINUTES, DEFAULT_CALENDAR_LEAD_MINUTES),
            )
        ] = NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, step=1, unit_of_measurement="minutes"))
        # suggested_value so a cleared subtitle stays cleared; the parse checks the length.
        for key in (CONF_CALENDAR_SUBTITLE_BEFORE, CONF_CALENDAR_SUBTITLE_DURING):
            fields[vol.Optional(key, description={"suggested_value": d.get(key, "")})] = str
    fields[_object_rows_key(CONF_RUN_WHILE, d, required=False)] = _RUN_WHILE_SELECTOR

    # --- Template-specific fields ---
    if template in ("generic", "steps"):
        fields[_entity_source_key(CONF_PROGRESS_ENTITY, d)] = entity_selector
        fields[_attr_suggest_key(CONF_PROGRESS_ATTRIBUTE, d)] = attr_selector
        fields[vol.Optional(CONF_PROGRESS_MODE, default=d.get(CONF_PROGRESS_MODE) or PROGRESS_MODE_DIRECT)] = (
            SelectSelector(
                SelectSelectorConfig(
                    options=list(PROGRESS_MODES),
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="progress_mode",
                )
            )
        )
    # live_progress derives its window from the remaining-time source, so every
    # template offering the toggle below must offer the source here too.
    if template == "countdown" or template in LIVE_PROGRESS_TEMPLATES:
        fields[_entity_source_key(CONF_REMAINING_TIME_ENTITY, d)] = entity_selector
        fields[_attr_suggest_key(CONF_REMAINING_TIME_ATTR, d)] = attr_selector
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
    if template in IMAGE_TEMPLATES:
        # The device downloads image_url itself and will not touch a private host,
        # so the URL is https-only here as it is server-side. The thumbhash is the
        # tier that always renders; leaving it blank lets the integration derive one
        # from the URL, and pushward.generate_thumbhash produces one from any image
        # Home Assistant can read (including plain-http LAN cameras).
        fields[_attr_suggest_key(CONF_IMAGE_URL, d)] = vol.All(str, vol.Length(max=IMAGE_URL_MAX))
        fields[
            vol.Optional(
                CONF_IMAGE_SHAPE,
                default=d.get(CONF_IMAGE_SHAPE) or DEFAULT_IMAGE_SHAPE,
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=list(IMAGE_SHAPES),
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="image_shape",
            )
        )
        fields[_attr_suggest_key(CONF_IMAGE_THUMBHASH, d)] = vol.All(str, vol.Length(max=IMAGE_THUMBHASH_MAX))
    if template == "media":
        # The transport row rides on a callback that cannot ask for a Home Assistant
        # token, so anyone holding the pushed URL can drive the player. Turning this
        # off leaves a display-only card and stops the endpoint answering for it.
        fields[
            vol.Optional(
                CONF_MEDIA_CONTROLS,
                default=d.get(CONF_MEDIA_CONTROLS, DEFAULT_MEDIA_CONTROLS),
            )
        ] = BooleanSelector()
        # There is no favorite in the media_player domain, so the heart runs a
        # script instead - whatever "star this track" means for the app playing it.
        fields[_entity_source_key(CONF_MEDIA_FAVORITE_SCRIPT, d)] = EntitySelector(
            EntitySelectorConfig(domain="script")
        )
    if template == "steps":
        fields[
            vol.Optional(
                CONF_TOTAL_STEPS,
                default=d.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS),
            )
        ] = NumberSelector(NumberSelectorConfig(min=1, max=TOTAL_STEPS_MAX, mode=NumberSelectorMode.BOX))
        fields[_entity_source_key(CONF_CURRENT_STEP_ENTITY, d)] = entity_selector
        fields[_attr_suggest_key(CONF_CURRENT_STEP_ATTR, d)] = attr_selector
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
        fields[_attr_suggest_key(CONF_SEVERITY_LABEL, d)] = vol.All(str, vol.Length(max=MAX_SEVERITY_LABEL_LEN))
        fields[_entity_source_key(CONF_FIRED_AT_ENTITY, d)] = entity_selector
        fields[_attr_suggest_key(CONF_FIRED_AT_ATTRIBUTE, d)] = attr_selector
    if template == "gauge":
        fields[_entity_source_key(CONF_VALUE_ENTITY, d)] = entity_selector
        fields[_attr_suggest_key(CONF_VALUE_ATTRIBUTE, d)] = attr_selector
        fields[
            vol.Required(
                CONF_MIN_VALUE,
                default=d.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE),
            )
        ] = _NUMBER_BOX_ANY
        fields[
            vol.Required(
                CONF_MAX_VALUE,
                default=d.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE),
            )
        ] = _NUMBER_BOX_ANY
        fields[_attr_suggest_key(CONF_UNIT, d)] = vol.All(str, vol.Length(max=ACTIVITY_UNIT_MAX))
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
        fields[_attr_suggest_key(CONF_PRIMARY_SERIES, d)] = SelectSelector(
            SelectSelectorConfig(
                options=_primary_series_options(d),
                custom_value=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        fields[_entity_source_key(CONF_VALUE_ENTITY, d)] = entity_selector
        fields[_attr_suggest_key(CONF_VALUE_ATTRIBUTE, d)] = attr_selector
        fields[_attr_suggest_key(CONF_UNIT, d)] = vol.All(str, vol.Length(max=ACTIVITY_UNIT_MAX))
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
                max=HISTORY_PERIOD_MAX,
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
        fields[_attr_suggest_key(CONF_LOG_LEVEL_ATTRIBUTE, d)] = attr_selector

    # --- Identity fields ---
    fields[_attr_suggest_key(CONF_SLUG, d)] = vol.All(str, vol.Length(max=MAX_SLUG_LEN))
    fields[_attr_suggest_key(CONF_ACTIVITY_NAME, d)] = vol.All(str, vol.Length(max=MAX_TEXT_LEN))
    fields[_attr_suggest_key(CONF_ICON, d)] = IconSelector(IconSelectorConfig())
    fields[_attr_suggest_key(CONF_ICON_ATTRIBUTE, d)] = attr_selector
    # An icon per state (a two-column row editor) of the tracked entity, or of the
    # entity picked next to it; a matching row wins over every other icon source.
    fields[_object_rows_key(CONF_ICON_MAP, d, required=False)] = _ICON_MAP_SELECTOR
    fields[_entity_source_key(CONF_ICON_MAP_ENTITY, d)] = entity_selector
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
    fields[_attr_suggest_key(CONF_SUBTITLE_ATTRIBUTE, d)] = attr_selector
    # Subtitle composed of columns (the log-columns row editor); resolved columns
    # replace the subtitle source above.
    fields[_object_rows_key(CONF_SUBTITLE_COLUMNS, d, required=False)] = _LOG_COLUMNS_SELECTOR
    # State text read from an entity and/or attribute in place of the state label.
    fields[_entity_source_key(CONF_STATE_TEXT_ENTITY, d)] = entity_selector
    fields[_attr_suggest_key(CONF_STATE_TEXT_ATTRIBUTE, d)] = attr_selector
    fields[vol.Optional(CONF_STATE_TEXT_FORMAT, default=d.get(CONF_STATE_TEXT_FORMAT) or DEFAULT_STATE_TEXT_FORMAT)] = (
        SelectSelector(
            SelectSelectorConfig(
                options=list(STATE_TEXT_FORMATS),
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="state_text_format",
            )
        )
    )
    # suggested_value, not default, so a cleared pattern stays cleared (see the island labels).
    # Length is checked in the parse, where it maps to a translated error.
    fields[
        vol.Optional(CONF_STATE_TEXT_PATTERN, description={"suggested_value": d.get(CONF_STATE_TEXT_PATTERN, "")})
    ] = str
    # Custom display text per state (a two-column row editor: state -> label). A
    # legacy 'state=Label, ...' string is still accepted by _parse_entity_input.
    fields[_object_rows_key(CONF_STATE_LABELS, d, required=False)] = _STATE_LABELS_SELECTOR
    # The island labels use suggested_value, not default: the frontend drops a cleared
    # optional string, and a default would put the saved label straight back on reconfigure.
    fields[
        vol.Optional(
            CONF_COMPACT_LABEL,
            description={"suggested_value": d.get(CONF_COMPACT_LABEL, "")},
        )
    ] = vol.All(str, _TrimmedLength(max=MAX_COMPACT_LABEL_LEN))
    fields[
        vol.Optional(
            CONF_COMPACT_TRAILING_LABEL,
            description={"suggested_value": d.get(CONF_COMPACT_TRAILING_LABEL, "")},
        )
    ] = vol.All(str, _TrimmedLength(max=MAX_COMPACT_TRAILING_LABEL_LEN))
    fields[_attr_suggest_key(CONF_COMPACT_TRAILING_ICON, d)] = IconSelector(IconSelectorConfig())
    if template == "countdown":
        fields[_attr_suggest_key(CONF_COMPLETION_MESSAGE, d)] = vol.All(str, vol.Length(max=MAX_LONG_TEXT_LEN))
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
    fields[_attr_suggest_key(CONF_ACCENT_COLOR_ATTRIBUTE, d)] = attr_selector
    if template in ("generic", "steps", "gauge"):
        fields[vol.Optional(CONF_ACCENT_FROM_PROGRESS, default=d.get(CONF_ACCENT_FROM_PROGRESS, False))] = (
            BooleanSelector()
        )
    fields[bg_color_key] = ColorRGBSelector()
    fields[_attr_suggest_key(CONF_BACKGROUND_COLOR_ATTRIBUTE, d)] = attr_selector
    fields[text_color_key] = ColorRGBSelector()
    fields[_attr_suggest_key(CONF_TEXT_COLOR_ATTRIBUTE, d)] = attr_selector
    fields[_attr_suggest_key(CONF_TAP_ACTION_URL, d)] = vol.All(str, vol.Length(max=MAX_URL_LEN))
    fields[
        vol.Optional(
            CONF_TAP_ACTION_FOREGROUND,
            default=d.get(CONF_TAP_ACTION_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND),
        )
    ] = BooleanSelector()
    # Only the two templates that render a button row get the button slots.
    if template in ("steps", "alert"):
        _add_button_action_fields(fields, d)
    fields[vol.Optional(CONF_REMOVE_ON_END, default=d.get(CONF_REMOVE_ON_END, False))] = BooleanSelector()
    fields[ended_ttl_key] = NumberSelector(
        NumberSelectorConfig(
            min=ACTIVITY_TTL_MIN,
            max=ACTIVITY_TTL_MAX,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="seconds",
        )
    )
    fields[stale_ttl_key] = NumberSelector(
        NumberSelectorConfig(
            min=ACTIVITY_TTL_MIN,
            max=ACTIVITY_TTL_MAX,
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


# Every action slot on a detail form: (url key, foreground key, title key or None
# when the slot has no button label), in the order _raise_url_errors reports them.
_ACTION_SLOTS: tuple[tuple[str, str, str | None], ...] = (
    (CONF_TAP_ACTION_URL, CONF_TAP_ACTION_FOREGROUND, None),
    (CONF_URL, CONF_URL_FOREGROUND, CONF_URL_TITLE),
    (CONF_SECONDARY_URL, CONF_SECONDARY_URL_FOREGROUND, CONF_SECONDARY_URL_TITLE),
)


def _parse_action_slots(user_input: dict) -> dict:
    """Read, strip and validate the surface-wide tap action plus both button slots.

    Returns the storage keys ready to splice into a parser's return dict. Raises
    `vol.Invalid` naming every field that failed the URL rules, so the form lights
    all of them up at once.
    """
    parsed: dict = {}
    checks: list[tuple[str, str, bool]] = []
    for url_key, foreground_key, title_key in _ACTION_SLOTS:
        url = (user_input.get(url_key) or "").strip()
        foreground = bool(user_input.get(foreground_key, DEFAULT_TAP_ACTION_FOREGROUND))
        parsed[url_key] = url
        parsed[foreground_key] = foreground
        if title_key is not None:
            parsed[title_key] = (user_input.get(title_key) or "").strip()
        checks.append((url_key, url, foreground))
    _raise_url_errors(checks)
    return parsed


def _parse_image_fields(user_input: dict) -> tuple[str, str, str]:
    """Validate and normalize the image trio, returning (url, shape, thumbhash).

    Both the URL and the hash are checked here rather than in the schema so the user
    gets a translated form error instead of a raw voluptuous message, matching how
    the other structured fields on this form report. An empty URL clears the whole
    trio: a shape or a hash with nothing to frame would only ever confuse the next
    person to open the form.
    """
    url = (user_input.get(CONF_IMAGE_URL) or "").strip()
    if not url:
        return "", "", ""
    try:
        validate_image_url(url)
    except vol.Invalid as err:
        raise vol.Invalid("invalid_image_url", path=[CONF_IMAGE_URL]) from err

    shape = (user_input.get(CONF_IMAGE_SHAPE) or DEFAULT_IMAGE_SHAPE).strip()
    if shape not in IMAGE_SHAPES:
        raise vol.Invalid("invalid_image_shape", path=[CONF_IMAGE_SHAPE])

    thumbhash = (user_input.get(CONF_IMAGE_THUMBHASH) or "").strip()
    if thumbhash:
        try:
            validate_thumbhash(thumbhash)
        except vol.Invalid as err:
            raise vol.Invalid("invalid_image_thumbhash", path=[CONF_IMAGE_THUMBHASH]) from err
    return url, shape, thumbhash


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
    color = str(item.get(CONF_TILE_COLOR, "") or "").strip()
    url_action = str(item.get(CONF_TILE_URL_ACTION, "") or "").strip()
    if len(label) > BOARD_TILE_LABEL_MAX or len(unit) > BOARD_TILE_UNIT_MAX or len(icon) > BOARD_TILE_ICON_MAX:
        raise vol.Invalid("invalid_tile", path=[CONF_TILES])
    if color and not is_valid_color(color):
        raise vol.Invalid("invalid_tile", path=[CONF_TILES])
    if url_action:
        # Length cap first (the server rejects a tap-action URL over MAX_URL_LEN, same
        # as the whole-activity URL slots), then the shared scheme/blocklist validator.
        if len(url_action) > MAX_URL_LEN:
            raise vol.Invalid("invalid_tile", path=[CONF_TILES])
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
        tile[CONF_TILE_COLOR] = color
    if url_action:
        tile[CONF_TILE_URL_ACTION] = url_action
    return tile


def _parse_board_tiles(raw: object) -> list[dict]:
    """Parse board tiles from a row-editor list (or the legacy comma string).

    From the form each row is already a dict with the storage keys: ``{label,
    entity_id, value_attribute?, unit?, icon?, color?, url_action?}``. The legacy
    string form ('label=entity_id[:attr[:unit[:icon]]], ...') stays accepted.
    Raises invalid_tile / too_many_tiles (past BOARD_MAX_TILES), and applies the
    per-field length, color, and URL rules to the rows.
    """
    if isinstance(raw, list):
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
            raise vol.Invalid("invalid_tile", path=[CONF_TILES])
        label, rest = entry.split("=", 1)
        label = label.strip()
        # maxsplit=3 keeps the icon (4th field) intact even when it contains a
        # colon, e.g. an "mdi:cpu" MDI icon - otherwise the prefix would be lost.
        parts = [p.strip() for p in rest.split(":", 3)]
        if not label or not parts or not parts[0]:
            raise vol.Invalid("invalid_tile", path=[CONF_TILES])
        tile: dict = {CONF_LABEL: label, CONF_ENTITY_ID: parts[0]}
        if len(parts) > 1 and parts[1]:
            tile[CONF_VALUE_ATTRIBUTE] = parts[1]
        if len(parts) > 2 and parts[2]:
            tile[CONF_UNIT] = parts[2]
        if len(parts) > 3 and parts[3]:
            tile[CONF_ICON] = parts[3]
        tiles.append(tile)
    if len(tiles) > BOARD_MAX_TILES:
        raise vol.Invalid("too_many_tiles", path=[CONF_TILES])
    return tiles


def _strict_log_column(item: object, path_key: str = CONF_LOG_COLUMNS) -> dict:
    """Validate + normalize one log-column row dict, raising on any problem (at ``path_key``).

    A column reads a source: an attribute of the tracked entity (entity empty,
    attribute set), another entity's state (entity set, attribute empty), or that
    entity's attribute (both set). A row carrying neither is meaningless and bounces
    with invalid_log_column; the label is capped at LOG_COLUMN_LABEL_MAX. Returns a
    column carrying only the non-empty storage keys.
    """
    if not isinstance(item, dict):
        raise vol.Invalid("invalid_log_column", path=[path_key])
    entity_id = str(item.get(CONF_ENTITY_ID, "") or "").strip()
    attr = str(item.get(CONF_SERIES_ATTRIBUTE, "") or "").strip()
    if not entity_id and not attr:
        raise vol.Invalid("invalid_log_column", path=[path_key])
    label = str(item.get(CONF_LABEL, "") or "").strip()
    unit = str(item.get(CONF_UNIT, "") or "").strip()
    if len(label) > LOG_COLUMN_LABEL_MAX:
        raise vol.Invalid("invalid_log_column", path=[path_key])
    column: dict = {}
    if label:
        column[CONF_LABEL] = label
    if entity_id:
        column[CONF_ENTITY_ID] = entity_id
    if attr:
        column[CONF_SERIES_ATTRIBUTE] = attr
    if unit:
        column[CONF_UNIT] = unit
    return column


def _parse_log_columns(raw: object, *, path_key: str = CONF_LOG_COLUMNS) -> list[dict]:
    """Parse log or subtitle columns from a row-editor list (or the legacy comma string).

    From the form each row is a dict with the storage keys: ``{label?, entity_id?,
    attribute?, unit?}``. The legacy string form ('[Label=]source[|unit], ...') stays
    accepted. ``source`` disambiguates in the string form:
      - ``brightness`` (no dot)            an attribute of the tracked entity
      - ``binary_sensor.door`` (has a dot) another entity's state
      - ``sensor.temp:temperature``        another entity's attribute
    Raises invalid_log_column / too_many_log_columns (past LOG_MAX_COLUMNS), and
    applies the label length cap to the rows.
    """
    if isinstance(raw, list):
        columns = [_strict_log_column(item, path_key) for item in raw]
        if len(columns) > LOG_MAX_COLUMNS:
            raise vol.Invalid("too_many_log_columns", path=[path_key])
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
            raise vol.Invalid("invalid_log_column", path=[path_key])
        column: dict = {}
        if label:
            column[CONF_LABEL] = label
        if ":" in source:
            entity_id, attr = (part.strip() for part in source.split(":", 1))
            if not entity_id:
                raise vol.Invalid("invalid_log_column", path=[path_key])
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
    if len(columns) > LOG_MAX_COLUMNS:
        raise vol.Invalid("too_many_log_columns", path=[path_key])
    return columns


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
    attr = str(item.get(CONF_SERIES_ATTRIBUTE, "") or "").strip()
    label = str(item.get(CONF_LABEL, "") or "").strip()
    if len(label) > TIMELINE_SERIES_LABEL_MAX:
        raise vol.Invalid("invalid_series_entity", path=[CONF_SERIES_ENTITIES])
    series: dict = {CONF_ENTITY_ID: entity_id}
    if attr:
        series[CONF_SERIES_ATTRIBUTE] = attr
    if label:
        series[CONF_LABEL] = label
    return series


def _parse_series_entities(raw: object) -> list[dict]:
    """Parse timeline series entities from a row-editor list (or the legacy comma string).

    From the form each row is a dict with the storage keys: ``{label?, entity_id,
    attribute?}``. The legacy string form ('[Label=]entity_id[:attribute], ...') stays
    accepted; in that form ``source`` must be an entity_id (contains a dot) and a bare
    word is not a series. The optional label is left raw here and frozen later by
    ``_resolve_series_entity_labels``. Raises invalid_series_entity per row and leaves
    the over-cap check to the combined series + series_entities guard in
    ``_parse_entity_input`` (so a lone over-cap list still trips too_many_series).
    """
    if isinstance(raw, list):
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
            raise vol.Invalid("invalid_series_entity", path=[CONF_SERIES_ENTITIES])
        series: dict = {}
        if ":" in source:
            entity_id, attr = (part.strip() for part in source.split(":", 1))
            if not entity_id or "." not in entity_id:
                raise vol.Invalid("invalid_series_entity", path=[CONF_SERIES_ENTITIES])
            series[CONF_ENTITY_ID] = entity_id
            if attr:
                series["attribute"] = attr
        elif "." in source:
            series[CONF_ENTITY_ID] = source
        else:
            raise vol.Invalid("invalid_series_entity", path=[CONF_SERIES_ENTITIES])
        if label:
            series[CONF_LABEL] = label
        result.append(series)
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


def _series_label_sources(series: object, series_entities: object) -> list[str]:
    """Ordered series labels drawn from a CONF_SERIES map (or its row-editor rows on
    reconfigure) and each CONF_SERIES_ENTITIES row.

    The single source for both the primary_series dropdown options and the known-
    label validation set, keeping the two in lockstep (this exact label-derivation
    has drifted between paths and caused incidents before). Not de-duplicated -
    callers dedup or set-wrap as they need.
    """
    labels: list[str] = []
    if isinstance(series, dict):
        labels.extend(str(v) for v in series.values() if v)
    elif isinstance(series, list):
        labels.extend(str(row[CONF_LABEL]) for row in series if isinstance(row, dict) and row.get(CONF_LABEL))
    for entry in series_entities or []:
        if isinstance(entry, dict) and entry.get(CONF_LABEL):
            labels.append(str(entry[CONF_LABEL]))
    return labels


def _timeline_series_labels(
    series: dict, series_entities: list[dict], entity_id: str, hass: HomeAssistant | None
) -> set[str]:
    """Collect every label a timeline primary_series may legitimately name.

    Mirrors how content_mapper labels series: CONF_SERIES values, each resolved
    CONF_SERIES_ENTITIES label, and - when neither source is configured - the
    single default series labelled with the tracked entity's friendly name (see
    _get_timeline_values).
    """
    labels: set[str] = set(_series_label_sources(series, series_entities))
    if not series and not series_entities:
        labels.add(_entity_friendly_name(hass, entity_id))
    return labels


def _primary_series_options(config: dict) -> list[str]:
    """Series labels offered by the primary_series dropdown (custom_value still allows typing).

    Sourced from the stored CONF_SERIES map values (or its row-editor rows on
    reconfigure) and each resolved CONF_SERIES_ENTITIES label, de-duplicated in
    order. Empty on a fresh add, which is fine: custom_value lets the user type one.
    """
    return list(dict.fromkeys(_series_label_sources(config.get(CONF_SERIES), config.get(CONF_SERIES_ENTITIES))))


def _strict_run_while_row(item: object) -> dict:
    """Validate + normalize one run_while row, raising invalid_run_while on any problem.

    The entity must be a valid entity_id and the comma-separated states must name at
    least one state. Returns the row with its states as a list.
    """
    rows = run_while_rows([item])
    if not rows or not valid_entity_id(rows[0][CONF_ENTITY_ID]):
        raise vol.Invalid("invalid_run_while", path=[CONF_RUN_WHILE])
    return rows[0]


def _parse_run_while(raw: object, *, strict: bool = False) -> list[dict]:
    """Parse run_while conditions from the row editor ({entity_id, states}, states comma-separated).

    Stored as ``{entity_id, states: [...]}``. Lenient (default) skips malformed rows and
    truncates past RUN_WHILE_MAX; strict (form paths) raises invalid_run_while /
    too_many_run_while instead. The field is new, so there is no older string form.
    """
    if not strict:
        return run_while_rows(raw)
    if not isinstance(raw, list):
        if raw:
            raise vol.Invalid("invalid_run_while", path=[CONF_RUN_WHILE])
        return []
    rows = [_strict_run_while_row(item) for item in raw]
    if len(rows) > RUN_WHILE_MAX:
        raise vol.Invalid("too_many_run_while", path=[CONF_RUN_WHILE])
    return rows


def _parse_calendar_lead_minutes(user_input: dict) -> int:
    """The calendar lead time as whole minutes in 0..CALENDAR_LEAD_MINUTES_MAX, else invalid_calendar_lead_minutes."""
    raw = user_input.get(CONF_CALENDAR_LEAD_MINUTES, DEFAULT_CALENDAR_LEAD_MINUTES)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise vol.Invalid("invalid_calendar_lead_minutes", path=[CONF_CALENDAR_LEAD_MINUTES]) from None
    if not math.isfinite(value) or value != int(value) or not 0 <= value <= CALENDAR_LEAD_MINUTES_MAX:
        raise vol.Invalid("invalid_calendar_lead_minutes", path=[CONF_CALENDAR_LEAD_MINUTES])
    return int(value)


def _parse_calendar_fields(user_input: dict) -> dict:
    """The calendar window keys; a subtitle longer than CALENDAR_SUBTITLE_MAX is invalid_calendar_subtitle."""
    fields: dict = {CONF_CALENDAR_LEAD_MINUTES: _parse_calendar_lead_minutes(user_input)}
    for key in (CONF_CALENDAR_SUBTITLE_BEFORE, CONF_CALENDAR_SUBTITLE_DURING):
        text = str(user_input.get(key) or "").strip()
        if len(text) > CALENDAR_SUBTITLE_MAX:
            raise vol.Invalid("invalid_calendar_subtitle", path=[key])
        fields[key] = text
    return fields


def _parse_entity_input(user_input: dict, hass: HomeAssistant | None = None) -> dict:
    """Normalize user input into an entity config dict.

    Called only from the two subentry form paths: a malformed row or token raises
    vol.Invalid instead of being silently dropped, and the details step maps each
    exc.path field to its error code.
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

    # Validate URLs (allow http/https + custom schemes; silent mode requires http(s)).
    # Parsed here rather than inline in the return so a bad URL still reports ahead
    # of the structured-field checks below.
    action_slots = _parse_action_slots(user_input)

    min_v, max_v = _coerce_gauge_range(user_input, is_gauge=template == "gauge")

    # Parse timeline fields (series map: attribute -> label). Each label becomes a
    # value-map key on the wire, which the server caps at TIMELINE_SERIES_LABEL_MAX
    # runes; cap it here (and again defensively at emission in content_mapper) so an
    # over-length label can't 400 the whole timeline push. Done before the
    # primary_series check so its known-label set matches what gets stored.
    series = {
        attr: label[:TIMELINE_SERIES_LABEL_MAX]
        for attr, label in _kv_rows_to_map(user_input.get(CONF_SERIES, ""), "attribute", CONF_LABEL).items()
    }
    series_entities = _resolve_series_entity_labels(
        _parse_series_entities(user_input.get(CONF_SERIES_ENTITIES, "")), hass
    )
    if len(series) + len(series_entities) > TIMELINE_MAX_SERIES:
        raise vol.Invalid("too_many_series", path=[CONF_SERIES_ENTITIES])
    primary_series = (user_input.get(CONF_PRIMARY_SERIES) or "").strip()
    if template == "timeline" and primary_series:
        known_labels = _timeline_series_labels(series, series_entities, entity_id, hass)
        if primary_series not in known_labels:
            raise vol.Invalid("unknown_primary_series", path=[CONF_PRIMARY_SERIES])
    thresholds = _parse_thresholds(user_input.get(CONF_THRESHOLDS, ""))
    history_period_raw = user_input.get(CONF_HISTORY_PERIOD, DEFAULT_HISTORY_PERIOD)

    # Board tiles: a board needs at least one tile to render.
    tiles = _parse_board_tiles(user_input.get(CONF_TILES, ""))
    if template == "board" and not tiles:
        raise vol.Invalid("tiles_required", path=[CONF_TILES])

    # Steps: the unified editor (only the steps form has it) submits one row per step,
    # decomposed here into the four stored keys. The row count must be 0 or exactly
    # total_steps (the server drops a wrong-length list, so surface it here rather than silently).
    total_steps = int(user_input.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS))
    step_fields = _decompose_steps_rows(user_input.get(CONF_STEPS_EDITOR), total_steps)
    log_columns = _parse_log_columns(user_input.get(CONF_LOG_COLUMNS, ""))
    subtitle_columns = _parse_log_columns(user_input.get(CONF_SUBTITLE_COLUMNS, ""), path_key=CONF_SUBTITLE_COLUMNS)
    icon_map = _kv_rows_to_map(user_input.get(CONF_ICON_MAP, ""), "state", CONF_ICON)
    if any(len(icon) > MAX_ICON_LEN for icon in icon_map.values()):
        raise vol.Invalid("invalid_icon_map", path=[CONF_ICON_MAP])
    state_text_pattern = str(user_input.get(CONF_STATE_TEXT_PATTERN) or "").strip()
    if len(state_text_pattern) > STATE_TEXT_PATTERN_MAX:
        raise vol.Invalid("invalid_state_text_pattern", path=[CONF_STATE_TEXT_PATTERN])
    image_url, image_shape, image_thumbhash = _parse_image_fields(user_input)

    # The picker already filters to scripts; a hand-written config can still name
    # anything, and script.turn_on on a light would fail silently at press time.
    favorite_script = (user_input.get(CONF_MEDIA_FAVORITE_SCRIPT) or "").strip()
    if favorite_script and _entity_domain(favorite_script) != "script":
        raise vol.Invalid("invalid_favorite_script", path=[CONF_MEDIA_FAVORITE_SCRIPT])

    run_while = _parse_run_while(user_input.get(CONF_RUN_WHILE, []), strict=True)
    # Only a calendar gets the key: its presence is what switches the window lifecycle on.
    calendar = _parse_calendar_fields(user_input) if domain == "calendar" else {}

    return {
        CONF_ENTITY_ID: entity_id,
        CONF_SLUG: slug,
        CONF_ACTIVITY_NAME: user_input.get(CONF_ACTIVITY_NAME, "") or entity_id,
        CONF_ICON: user_input.get(CONF_ICON, ""),
        CONF_ICON_ATTRIBUTE: user_input.get(CONF_ICON_ATTRIBUTE, ""),
        CONF_ICON_MAP: icon_map,
        CONF_ICON_MAP_ENTITY: user_input.get(CONF_ICON_MAP_ENTITY, ""),
        CONF_PRIORITY: int(user_input.get(CONF_PRIORITY, DEFAULT_PRIORITY)),
        CONF_TEMPLATE: user_input.get(CONF_TEMPLATE, "generic"),
        CONF_START_STATES: start_states or defaults.get("start_states", []),
        CONF_END_STATES: end_states or defaults.get("end_states", []),
        CONF_UPDATE_INTERVAL: int(user_input.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
        CONF_PROGRESS_ATTRIBUTE: user_input.get(CONF_PROGRESS_ATTRIBUTE, ""),
        CONF_PROGRESS_ENTITY: user_input.get(CONF_PROGRESS_ENTITY, ""),
        CONF_PROGRESS_MODE: user_input.get(CONF_PROGRESS_MODE) or PROGRESS_MODE_DIRECT,
        CONF_REMAINING_TIME_ATTR: user_input.get(CONF_REMAINING_TIME_ATTR, ""),
        CONF_REMAINING_TIME_ENTITY: user_input.get(CONF_REMAINING_TIME_ENTITY, ""),
        CONF_LIVE_PROGRESS: bool(user_input.get(CONF_LIVE_PROGRESS, False)),
        CONF_IMAGE_URL: image_url,
        CONF_IMAGE_SHAPE: image_shape,
        CONF_IMAGE_THUMBHASH: image_thumbhash,
        CONF_SUBTITLE_ATTRIBUTE: user_input.get(CONF_SUBTITLE_ATTRIBUTE, ""),
        CONF_SUBTITLE_ENTITY: user_input.get(CONF_SUBTITLE_ENTITY, ""),
        CONF_SUBTITLE_COLUMNS: subtitle_columns,
        CONF_STATE_TEXT_ENTITY: user_input.get(CONF_STATE_TEXT_ENTITY, ""),
        CONF_STATE_TEXT_ATTRIBUTE: user_input.get(CONF_STATE_TEXT_ATTRIBUTE, ""),
        CONF_STATE_TEXT_FORMAT: user_input.get(CONF_STATE_TEXT_FORMAT) or DEFAULT_STATE_TEXT_FORMAT,
        CONF_STATE_TEXT_PATTERN: state_text_pattern,
        CONF_STATE_LABELS: _kv_rows_to_map(user_input.get(CONF_STATE_LABELS, ""), "state", CONF_LABEL),
        CONF_COMPLETION_MESSAGE: user_input.get(CONF_COMPLETION_MESSAGE, ""),
        CONF_COMPACT_LABEL: user_input.get(CONF_COMPACT_LABEL, ""),
        CONF_COMPACT_TRAILING_LABEL: user_input.get(CONF_COMPACT_TRAILING_LABEL, ""),
        CONF_COMPACT_TRAILING_ICON: user_input.get(CONF_COMPACT_TRAILING_ICON, ""),
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
        CONF_ACCENT_FROM_PROGRESS: bool(user_input.get(CONF_ACCENT_FROM_PROGRESS, False)),
        **action_slots,
        CONF_ENDED_TTL: int(ended_ttl) if ended_ttl is not None else None,
        CONF_STALE_TTL: int(stale_ttl) if stale_ttl is not None else None,
        CONF_DISMISSAL_TTL: int(dismissal_ttl) if dismissal_ttl is not None else None,
        CONF_REMOVE_ON_END: bool(user_input.get(CONF_REMOVE_ON_END, False)),
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
        CONF_MEDIA_CONTROLS: bool(user_input.get(CONF_MEDIA_CONTROLS, DEFAULT_MEDIA_CONTROLS)),
        CONF_MEDIA_FAVORITE_SCRIPT: favorite_script,
        CONF_RUN_WHILE: run_while,
        **calendar,
    }


def _entity_details_to_config(
    step1: dict[str, Any], user_input: dict[str, Any], hass: HomeAssistant | None
) -> tuple[dict | None, dict[str, str]]:
    """The details step's parse: the config to store, or the {field: error} the form shows.

    Shared with the panel preview, so a draft that previews cleanly also saves.
    """
    try:
        return _parse_entity_input({**step1, **user_input}, hass=hass), {}
    except vol.Invalid as exc:
        return None, dict.fromkeys(exc.path, str(exc.msg))


def _entity_reconfigure_defaults(data: Mapping[str, Any]) -> dict[str, Any]:
    """The details form's defaults on reconfigure, prepared from the stored config.

    Shared with the panel preview, so a reconfigure draft fills the defaults its save does.
    """
    # The map fields (state_labels, series, units) rehydrate their two-column row
    # editors from the stored dict; the unified steps editor composes one row per
    # step from the four stored step keys, which stay untouched in storage.
    current = dict(data)
    labels = current.get(CONF_STATE_LABELS)
    if isinstance(labels, dict):
        current[CONF_STATE_LABELS] = _map_to_kv_rows(labels, "state", CONF_LABEL)
    icon_map = current.get(CONF_ICON_MAP)
    if isinstance(icon_map, dict):
        current[CONF_ICON_MAP] = _map_to_kv_rows(icon_map, "state", CONF_ICON)
    series = current.get(CONF_SERIES)
    if isinstance(series, dict):
        current[CONF_SERIES] = _map_to_kv_rows(series, "attribute", CONF_LABEL)
    units = current.get(CONF_UNITS)
    if isinstance(units, dict):
        current[CONF_UNITS] = _map_to_kv_rows(units, "series", CONF_UNIT)
    current[CONF_STEPS_EDITOR] = _compose_steps_rows(current)
    # series_entities and thresholds stay lists: their ObjectSelector row
    # editors rehydrate from the stored list directly. tiles and log_columns
    # also rehydrate as lists, but a pre-0.37 config may hold fields the strict
    # parser now rejects, so sanitize the form defaults (storage untouched):
    # truncate over-cap tile/log fields to their caps.
    if isinstance(current.get(CONF_TILES), list):
        current[CONF_TILES] = _truncate_row_fields(
            current[CONF_TILES],
            {
                CONF_LABEL: BOARD_TILE_LABEL_MAX,
                CONF_UNIT: BOARD_TILE_UNIT_MAX,
                CONF_ICON: BOARD_TILE_ICON_MAX,
                "url_action": MAX_URL_LEN,
            },
        )
    for key in (CONF_LOG_COLUMNS, CONF_SUBTITLE_COLUMNS):
        if isinstance(current.get(key), list):
            current[key] = _truncate_row_fields(current[key], {CONF_LABEL: LOG_COLUMN_LABEL_MAX})
    # run_while stores its states as a list; the row editor edits them as one comma string.
    current[CONF_RUN_WHILE] = [
        {CONF_ENTITY_ID: row[CONF_ENTITY_ID], CONF_RUN_WHILE_STATES: ", ".join(row[CONF_RUN_WHILE_STATES])}
        for row in _parse_run_while(current.get(CONF_RUN_WHILE))
    ]
    # Drop a stored primary_series that names no configured series label
    # (legacy free text, or a series later renamed/removed): the strict parse
    # rejects an unknown value and would otherwise dead-end the reconfigure.
    primary = current.get(CONF_PRIMARY_SERIES)
    if primary and primary not in _primary_series_options(current):
        current[CONF_PRIMARY_SERIES] = ""
    return current


class PushWardConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial PushWard configuration."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the initial setup step: one entry per PushWard account."""
        errors: dict[str, str] = {}

        if user_input is not None:
            key = user_input[CONF_INTEGRATION_KEY]
            errors, me = await _validate_integration_key(self.hass, key, "setup")
            if not errors:
                # An entry from before several accounts were allowed learns its account
                # only once it loads; until then the same key is the sign it is this one.
                self._async_abort_entries_match({CONF_INTEGRATION_KEY: key})
                await self.async_set_unique_id(str(me["id"]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_account_title(me),
                    data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: key},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_INTEGRATION_KEY_SCHEMA,
            errors=errors,
            description_placeholders={"app_store_url": APP_STORE_URL, "play_store_url": PLAY_STORE_URL},
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle reconfiguration of the integration key."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            errors, me = await _validate_integration_key(self.hass, user_input[CONF_INTEGRATION_KEY], "reconfigure")
            if not errors:
                # Reconfigure may move the entry to another account (an organization key
                # swapped for a personal one), just not onto one another entry holds.
                account_id = str(me["id"])
                if self._account_taken(entry, account_id):
                    return self.async_abort(reason="already_configured")
                switched = entry.unique_id not in (DOMAIN, account_id)
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=account_id,
                    title=_account_title(me) if switched else UNDEFINED,
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

    def _account_taken(self, entry: config_entries.ConfigEntry, account_id: str) -> bool:
        """Whether an entry other than this one already holds the account."""
        other = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, account_id)
        return other is not None and other.entry_id != entry.entry_id

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
            errors, me = await _validate_integration_key(
                self.hass, user_input[CONF_INTEGRATION_KEY], "reauth", server_url=server_url
            )
            if not errors:
                # Reauth renews the key of the same account; switching accounts is what
                # Reconfigure is for. An entry from before several accounts were allowed
                # still carries the domain as its id and adopts the key's account.
                account_id = str(me["id"])
                if entry.unique_id not in (DOMAIN, account_id):
                    return self.async_abort(reason="wrong_account")
                if self._account_taken(entry, account_id):
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=account_id,
                    data_updates={
                        CONF_INTEGRATION_KEY: user_input[CONF_INTEGRATION_KEY],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_INTEGRATION_KEY_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> PushWardOptionsFlow:
        """Options: the end-to-end encryption key."""
        return PushWardOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Return supported subentry types."""
        return {
            SUBENTRY_TYPE_ENTITY: PushWardEntitySubentryFlow,
            SUBENTRY_TYPE_WIDGET: PushWardWidgetSubentryFlow,
            SUBENTRY_TYPE_TODO: PushWardTodoSubentryFlow,
        }


# Form-only: ticking it turns encryption off. Never stored.
_E2E_REMOVE = "remove_e2e_key"


def _e2e_key_schema(has_key: bool) -> vol.Schema:
    fields: dict = {vol.Optional(CONF_E2E_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
    if has_key:
        fields[vol.Optional(_E2E_REMOVE, default=False)] = BooleanSelector()
    return vol.Schema(fields)


class PushWardOptionsFlow(config_entries.OptionsFlow):
    """Set, replace or remove the key that encrypts notification text end to end."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """The key field always starts empty, so the stored key never goes back to the browser.

        Empty keeps the key in use; turning encryption off is the separate checkbox.
        """
        current = self.config_entry.options.get(CONF_E2E_KEY)
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = (user_input.get(CONF_E2E_KEY) or "").strip()
            if current and user_input.get(_E2E_REMOVE):
                return self.async_create_entry(data={})
            if not raw:
                return self.async_create_entry(data=dict(self.config_entry.options))
            try:
                key = parse_key(raw)
            except E2EError:
                errors[CONF_E2E_KEY] = (
                    "e2e_key_is_integration_key" if raw.startswith(("hlk_", "hla_")) else "invalid_e2e_key"
                )
            else:
                return self.async_create_entry(data={CONF_E2E_KEY: key.hex()})

        return self.async_show_form(
            step_id="init",
            data_schema=_e2e_key_schema(bool(current)),
            errors=errors,
            description_placeholders={"key_id": key_id(parse_key(current)) if current else "-"},
        )


def _media_control_token(entity_cfg: dict, existing: str | None) -> str:
    """The control secret to store: kept for as long as the subentry stays media.

    Deliberately not a form field. It is generated once when a media player is added
    and then preserved, so reconfiguring a player does not invalidate the buttons on
    a Live Activity that is already on someone's Lock Screen. Reconfiguring AWAY
    from media drops it - a secret nothing can use any more has no business staying
    in storage.
    """
    if entity_cfg.get(CONF_TEMPLATE) != "media":
        return ""
    return existing or new_control_token()


def _tracked_elsewhere(
    entry: config_entries.ConfigEntry, subentry_type: str, key: str, value: str, own_subentry_id: str | None = None
) -> bool:
    """Whether another subentry of this type already stores value under key.

    Read from the data rather than the unique_id: a subentry an older version moved
    to another entity still carries the unique_id of the one it left.
    """
    return any(
        sub.subentry_type == subentry_type and sub.data.get(key) == value and sub.subentry_id != own_subentry_id
        for sub in entry.subentries.values()
    )


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
            self._details_defaults = _entity_reconfigure_defaults(subentry.data)
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
            entity_cfg, errors = _entity_details_to_config(self._step1_input, user_input, self.hass)
            if entity_cfg is not None:
                entry = self._get_entry()
                subentry = self._get_reconfigure_subentry() if self._is_reconfigure else None
                if _tracked_elsewhere(
                    entry,
                    SUBENTRY_TYPE_ENTITY,
                    CONF_ENTITY_ID,
                    entity_cfg[CONF_ENTITY_ID],
                    subentry.subentry_id if subentry else None,
                ):
                    return self.async_abort(reason="already_configured")
                if subentry is not None:
                    entity_cfg[CONF_MEDIA_TOKEN] = _media_control_token(entity_cfg, subentry.data.get(CONF_MEDIA_TOKEN))
                    return self.async_update_and_abort(
                        entry,
                        subentry,
                        data=entity_cfg,
                        title=entity_cfg[CONF_ACTIVITY_NAME],
                        unique_id=entity_cfg[CONF_ENTITY_ID],
                    )
                entity_cfg[CONF_MEDIA_TOKEN] = _media_control_token(entity_cfg, None)
                return self.async_create_entry(
                    title=entity_cfg[CONF_ACTIVITY_NAME],
                    data=entity_cfg,
                    unique_id=entity_cfg[CONF_ENTITY_ID],
                )

        defaults = self._details_defaults if self._is_reconfigure else None
        # Show each error on its section and open that section.
        errors = _section_errors(ENTITY_SECTIONS, _entity_toplevel_fields(template), errors)
        expand = set(errors) & ENTITY_SECTIONS.keys()
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
            _attr_suggest_key(CONF_SLUG, d): vol.All(str, vol.Length(max=MAX_SLUG_LEN)),
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

    fields[_attr_suggest_key(CONF_WIDGET_NAME, d)] = vol.All(str, vol.Length(max=WIDGET_NAME_MAX))

    # Template-specific
    if template in (
        WIDGET_TEMPLATE_VALUE,
        WIDGET_TEMPLATE_PROGRESS,
        WIDGET_TEMPLATE_GAUGE,
        WIDGET_TEMPLATE_TREND,
        WIDGET_TEMPLATE_SCHEDULE,
        WIDGET_TEMPLATE_FLOW,
        WIDGET_TEMPLATE_HEATMAP,
    ):
        if template not in (WIDGET_TEMPLATE_SCHEDULE, WIDGET_TEMPLATE_FLOW, WIDGET_TEMPLATE_HEATMAP):
            fields[_attr_suggest_key(CONF_VALUE_ATTRIBUTE, d)] = attr_selector
        fields[_attr_suggest_key(CONF_UNIT, d)] = vol.All(str, vol.Length(max=WIDGET_UNIT_MAX))

    if template == WIDGET_TEMPLATE_TREND:
        fields[
            vol.Optional(
                CONF_HISTORY_PERIOD,
                default=d.get(CONF_HISTORY_PERIOD, DEFAULT_WIDGET_TREND_PERIOD),
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=HISTORY_PERIOD_MAX,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="minutes",
            )
        )
        fields[_opt_number_key(CONF_MIN_VALUE, d)] = _NUMBER_BOX_ANY
        fields[_opt_number_key(CONF_MAX_VALUE, d)] = _NUMBER_BOX_ANY

    if template == WIDGET_TEMPLATE_HEATMAP:
        fields[
            vol.Optional(
                CONF_HEATMAP_WEEKS,
                default=d.get(CONF_HEATMAP_WEEKS, DEFAULT_HEATMAP_WEEKS),
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=WIDGET_HEATMAP_WEEKS_MAX,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="weeks",
            )
        )
        # The flow steps put the entity's own default here (change for a total).
        fields[
            vol.Optional(
                CONF_HEATMAP_AGGREGATE,
                default=d.get(CONF_HEATMAP_AGGREGATE, HEATMAP_AGGREGATE_MEAN),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=HEATMAP_AGGREGATES,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="heatmap_aggregate",
            )
        )
        fields[_opt_number_key(CONF_MIN_VALUE, d)] = _NUMBER_BOX_ANY
        fields[_opt_number_key(CONF_MAX_VALUE, d)] = _NUMBER_BOX_ANY

    if template in (WIDGET_TEMPLATE_COUNTDOWN, WIDGET_TEMPLATE_PROGRESS):
        fields[_attr_suggest_key(CONF_END_DATE_ATTRIBUTE, d)] = attr_selector
        fields[_attr_suggest_key(CONF_START_DATE_ATTRIBUTE, d)] = attr_selector

    if template == WIDGET_TEMPLATE_COUNTDOWN:
        fields[_attr_suggest_key(CONF_EXPIRED_TEXT, d)] = vol.All(str, vol.Length(max=WIDGET_EXPIRED_TEXT_MAX))

    if template == WIDGET_TEMPLATE_BATTERY:
        fields[_object_rows_key(CONF_BATTERY_DEVICES, d, required=True)] = _BATTERY_DEVICES_SELECTOR
        # Small widgets show 2 of the rows above and medium 4, so ordering by
        # level is what decides whether the ones running low are visible.
        fields[
            vol.Optional(
                CONF_WIDGET_BATTERY_SORT,
                default=d.get(CONF_WIDGET_BATTERY_SORT, ""),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=WIDGET_BATTERY_SORTS,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="battery_sort",
            )
        )

    if template == WIDGET_TEMPLATE_SCHEDULE:
        stored_attrs = d.get(CONF_SCHEDULE_ATTRIBUTES)
        joined = ", ".join(stored_attrs) if isinstance(stored_attrs, list) else (stored_attrs or "")
        fields[vol.Optional(CONF_SCHEDULE_ATTRIBUTES, description={"suggested_value": joined})] = vol.All(
            str, vol.Length(max=MAX_TEXT_LEN)
        )
        fields[
            vol.Optional(
                CONF_SCHEDULE_START_KEY,
                default=d.get(CONF_SCHEDULE_START_KEY, DEFAULT_SCHEDULE_START_KEY),
            )
        ] = vol.All(str, vol.Length(max=MAX_TEXT_LEN))
        fields[
            vol.Optional(
                CONF_SCHEDULE_VALUE_KEY,
                default=d.get(CONF_SCHEDULE_VALUE_KEY, DEFAULT_SCHEDULE_VALUE_KEY),
            )
        ] = vol.All(str, vol.Length(max=MAX_TEXT_LEN))
        fields[_opt_number_key(CONF_SCHEDULE_LOW_MAX, d)] = _NUMBER_BOX_ANY
        fields[_opt_number_key(CONF_SCHEDULE_HIGH_MIN, d)] = _NUMBER_BOX_ANY

    if template == WIDGET_TEMPLATE_FLOW:
        fields[_object_rows_key(CONF_FLOW_NODES, d, required=True)] = _FLOW_NODES_SELECTOR

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
        ] = _NUMBER_BOX_ANY
        fields[
            vol.Required(
                CONF_MAX_VALUE,
                default=d.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE),
            )
        ] = _NUMBER_BOX_ANY

    if template == WIDGET_TEMPLATE_STATUS:
        # HA leaves the top-level "" (none) out of the submit, so a default would bring the old severity back.
        fields[_attr_suggest_key(CONF_SEVERITY, d)] = SelectSelector(
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

    # Style for every size and per size, plus Row Density on stat_list only. A
    # stored value the template no longer takes falls back to "not set".
    for conf, allowed in widget_looks(template).values():
        look_selector = SelectSelector(
            SelectSelectorConfig(
                options=[WIDGET_LOOK_UNSET, *allowed],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key=conf,
            )
        )
        for key in widget_look_keys(conf):
            stored = d.get(key)
            fields[vol.Optional(key, default=stored if stored in allowed else WIDGET_LOOK_UNSET)] = look_selector

    # Cosmetic fields (all templates)
    fields[_attr_suggest_key(CONF_LABEL, d)] = vol.All(str, vol.Length(max=WIDGET_LABEL_MAX))
    fields[_attr_suggest_key(CONF_LABEL_ATTRIBUTE, d)] = attr_selector
    fields[_attr_suggest_key(CONF_SUBTITLE_ATTRIBUTE, d)] = attr_selector
    if template not in WIDGET_GROUP_TEMPLATES:
        # A subtitle timer needs a date to anchor on, which the group templates
        # (no single state) have nowhere to read from. Top-level fields may use
        # real selectors - the ObjectSelector sub-field restriction is not in play.
        # No "" suggestion: inside a section HA submits it as is and the EntitySelector refuses it.
        fields[_entity_source_key(CONF_SUBTITLE_TIMER_ENTITY, d)] = EntitySelector(EntitySelectorConfig())
        fields[_attr_suggest_key(CONF_SUBTITLE_TIMER_ATTRIBUTE, d)] = attr_selector
        fields[
            vol.Optional(
                CONF_SUBTITLE_TIMER_STYLE,
                default=d.get(CONF_SUBTITLE_TIMER_STYLE, DEFAULT_SUBTITLE_TIMER_STYLE),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=TIMER_STYLES,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="timer_style",
            )
        )
    fields[_attr_suggest_key(CONF_ICON, d)] = IconSelector(IconSelectorConfig())
    fields[_attr_suggest_key(CONF_ICON_ATTRIBUTE, d)] = attr_selector
    fields[accent_key] = ColorRGBSelector()
    fields[_attr_suggest_key(CONF_ACCENT_COLOR_ATTRIBUTE, d)] = attr_selector
    fields[bg_color_key] = ColorRGBSelector()
    fields[text_color_key] = ColorRGBSelector()

    # Widget-wide tap action (universal across all templates)
    fields[_attr_suggest_key(CONF_TAP_ACTION_URL, d)] = vol.All(str, vol.Length(max=MAX_URL_LEN))
    fields[
        vol.Optional(
            CONF_TAP_ACTION_FOREGROUND,
            default=d.get(CONF_TAP_ACTION_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND),
        )
    ] = BooleanSelector()

    # Button slots. Unlike the activity side there is no template gate: the server
    # takes url_action / secondary_url_action on every widget template. iOS draws
    # them on the Home Screen families only, which the field help says.
    _add_button_action_fields(fields, d)

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
    # Optional with no default: an empty field means "never demote to stale",
    # which is the pre-1.6 behaviour every existing widget already has.
    fields[_opt_number_key(CONF_WIDGET_STALE_AFTER, d)] = NumberSelector(
        NumberSelectorConfig(
            min=WIDGET_STALE_AFTER_MIN,
            max=WIDGET_STALE_AFTER_MAX,
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
    timer_style = str(item.get("timer_style", "") or "").strip().lower()
    if len(label) > WIDGET_STAT_LABEL_MAX or len(unit) > WIDGET_STAT_UNIT_MAX:
        raise vol.Invalid("invalid_stat_row", path=[CONF_STAT_ROWS])
    if timer_style and timer_style not in TIMER_STYLES:
        raise vol.Invalid("invalid_stat_row", path=[CONF_STAT_ROWS])
    row: dict = {CONF_LABEL: label, CONF_ENTITY_ID: entity_id}
    if attr:
        row[CONF_VALUE_ATTRIBUTE] = attr
    if unit:
        row[CONF_UNIT] = unit
    if timer_style:
        row["timer_style"] = timer_style
    return row


def _strict_node_chrome(item: dict, error: str, path_key: str) -> tuple[str, str, str]:
    """Validate the name/icon/color a battery device and a flow node share.

    The server bounds all three in one validateNodeChrome; the mapper mirrors
    that in _node_chrome, and this is the strict form-side twin. Returns the
    stripped trio so the caller only decides which keys to store.
    """
    name = str(item.get(CONF_NODE_NAME, "") or "").strip()
    icon = str(item.get(CONF_ICON, "") or "").strip()
    color = str(item.get("color", "") or "").strip()
    if len(name) > WIDGET_NODE_NAME_MAX or len(icon) > WIDGET_NODE_ICON_MAX:
        raise vol.Invalid(error, path=[path_key])
    if color and not is_valid_color(color):
        raise vol.Invalid(error, path=[path_key])
    return name, icon, color


def _strict_battery_row(item: object) -> dict:
    """Validate + normalize one battery-device row, raising on any problem.

    Only the entity is required: the name falls back to the entity's friendly
    name at render time, which is what most people want anyway.
    """
    if not isinstance(item, dict):
        raise vol.Invalid("invalid_battery_device", path=[CONF_BATTERY_DEVICES])
    entity_id = str(item.get(CONF_ENTITY_ID, "") or "").strip()
    if not entity_id:
        raise vol.Invalid("invalid_battery_device", path=[CONF_BATTERY_DEVICES])
    attr = str(item.get(CONF_VALUE_ATTRIBUTE, "") or "").strip()
    charging = str(item.get(CONF_CHARGING_ENTITY, "") or "").strip()
    name, icon, color = _strict_node_chrome(item, "invalid_battery_device", CONF_BATTERY_DEVICES)
    row: dict = {CONF_ENTITY_ID: entity_id}
    for key, value in (
        (CONF_NODE_NAME, name),
        (CONF_VALUE_ATTRIBUTE, attr),
        (CONF_CHARGING_ENTITY, charging),
        (CONF_ICON, icon),
        ("color", color),
    ):
        if value:
            row[key] = value
    return row


def _parse_battery_devices(raw: object) -> list[dict]:
    """Parse battery devices from the row editor, raising the translated errors.

    Unlike stat_rows there is no legacy comma-string form to keep working - the
    template is new.
    """
    if not isinstance(raw, list):
        return []
    rows = [_strict_battery_row(item) for item in raw]
    if len(rows) > WIDGET_MAX_BATTERY_DEVICES:
        raise vol.Invalid("too_many_battery_devices", path=[CONF_BATTERY_DEVICES])
    return rows


def _strict_flow_node(item: object) -> dict:
    """Validate + normalize one flow-node row, raising on any problem."""
    if not isinstance(item, dict):
        raise vol.Invalid("invalid_flow_node", path=[CONF_FLOW_NODES])
    slot = str(item.get(CONF_FLOW_SLOT, "") or "").strip().lower()
    entity_id = str(item.get(CONF_ENTITY_ID, "") or "").strip()
    if slot not in FLOW_SLOTS or not entity_id:
        raise vol.Invalid("invalid_flow_node", path=[CONF_FLOW_NODES])
    attr = str(item.get(CONF_VALUE_ATTRIBUTE, "") or "").strip()
    total = str(item.get(CONF_TOTAL_ENTITY, "") or "").strip()
    level = str(item.get(CONF_LEVEL_ENTITY, "") or "").strip()
    name, icon, color = _strict_node_chrome(item, "invalid_flow_node", CONF_FLOW_NODES)
    row: dict = {CONF_FLOW_SLOT: slot, CONF_ENTITY_ID: entity_id}
    for key, value in (
        (CONF_NODE_NAME, name),
        (CONF_VALUE_ATTRIBUTE, attr),
        (CONF_TOTAL_ENTITY, total),
        (CONF_LEVEL_ENTITY, level),
        (CONF_ICON, icon),
        ("color", color),
    ):
        if value:
            row[key] = value
    return row


def _parse_flow_nodes(raw: object) -> list[dict]:
    """Parse flow nodes from the row editor, enforcing the per-slot arity.

    Three inputs is the server cap; output, storage and exchange are single
    slots, so a second row claiming one is a config mistake worth naming rather
    than silently dropping.
    """
    if not isinstance(raw, list):
        return []
    rows = [_strict_flow_node(item) for item in raw]
    inputs = sum(1 for r in rows if r[CONF_FLOW_SLOT] == FLOW_SLOT_INPUT)
    if inputs > WIDGET_MAX_FLOW_INPUTS:
        raise vol.Invalid("too_many_flow_inputs", path=[CONF_FLOW_NODES])
    singles = [r[CONF_FLOW_SLOT] for r in rows if r[CONF_FLOW_SLOT] != FLOW_SLOT_INPUT]
    if len(set(singles)) != len(singles):
        raise vol.Invalid("duplicate_flow_slot", path=[CONF_FLOW_NODES])
    return rows


def _parse_widget_stat_rows(raw: object) -> list[dict]:
    """Parse stat_rows from a row-editor list (or the legacy comma string).

    From the form each row is a dict with the storage keys: ``{label, entity_id,
    value_attribute?, unit?}``. The legacy string form
    ('label=entity_id[:attr[:unit]], ...') stays accepted. Raises invalid_stat_row /
    too_many_stat_rows (past WIDGET_MAX_STAT_ROWS), and applies the per-field length
    caps to the rows.
    """
    if isinstance(raw, list):
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
            raise vol.Invalid("invalid_stat_row", path=[CONF_STAT_ROWS])
        label, rest = entry.split("=", 1)
        label = label.strip()
        parts = [p.strip() for p in rest.split(":")]
        if not label or not parts or not parts[0]:
            raise vol.Invalid("invalid_stat_row", path=[CONF_STAT_ROWS])
        row: dict = {CONF_LABEL: label, CONF_ENTITY_ID: parts[0]}
        if len(parts) > 1 and parts[1]:
            row[CONF_VALUE_ATTRIBUTE] = parts[1]
        if len(parts) > 2 and parts[2]:
            row[CONF_UNIT] = parts[2]
        rows.append(row)
    if len(rows) > WIDGET_MAX_STAT_ROWS:
        raise vol.Invalid("too_many_stat_rows", path=[CONF_STAT_ROWS])
    return rows


def _optional_number(user_input: dict, key: str) -> float | None:
    """Read a genuinely optional numeric field: None when left blank."""
    raw = user_input.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as err:
        raise vol.Invalid("invalid_number", path=[key]) from err
    if not math.isfinite(value):
        raise vol.Invalid("invalid_number", path=[key])
    return value


def _coerce_trend_bounds(user_input: dict) -> tuple[float | None, float | None]:
    """Trend chart bounds: both optional, ordered only when both are set.

    Unlike the gauge, an unset bound means "let the client auto-scale" rather
    than a default of 0-100, so the gauge's coercion cannot be reused.
    """
    min_v = _optional_number(user_input, CONF_MIN_VALUE)
    max_v = _optional_number(user_input, CONF_MAX_VALUE)
    if min_v is not None and max_v is not None and min_v >= max_v:
        raise vol.Invalid("invalid_gauge_range", path=[CONF_MIN_VALUE])
    return min_v, max_v


def _coerce_schedule_thresholds(user_input: dict) -> tuple[float | None, float | None]:
    """Schedule band thresholds: low_max must sit below high_min when both are set."""
    low_max = _optional_number(user_input, CONF_SCHEDULE_LOW_MAX)
    high_min = _optional_number(user_input, CONF_SCHEDULE_HIGH_MIN)
    if low_max is not None and high_min is not None and low_max >= high_min:
        raise vol.Invalid("invalid_schedule_thresholds", path=[CONF_SCHEDULE_LOW_MAX])
    return low_max, high_min


def _coerce_stale_after(raw: object) -> int | None:
    """Clamp stale_after into the server's bounds; None when the field is empty.

    Clamped rather than rejected: the NumberSelector already bounds the form, so
    an out-of-range value only reaches here from hand-written YAML, where dropping
    the whole config over it would be worse than nudging the number.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        seconds = int(float(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(WIDGET_STALE_AFTER_MIN, min(WIDGET_STALE_AFTER_MAX, seconds))


def _parse_widget_input(user_input: dict, step1: dict) -> dict:
    """Build the persisted subentry data from step-1 + step-2 inputs."""
    user_input = _flatten_section_input(user_input, WIDGET_SECTIONS)
    entity_id = step1[CONF_ENTITY_ID]
    template = step1[CONF_WIDGET_TEMPLATE]
    raw_slug = (step1.get(CONF_SLUG) or "").strip()
    slug = (normalize_slug(raw_slug) if raw_slug else "") or sanitize_slug(entity_id)

    if template in (WIDGET_TEMPLATE_TREND, WIDGET_TEMPLATE_HEATMAP):
        min_v, max_v = _coerce_trend_bounds(user_input)
    else:
        min_v, max_v = _coerce_gauge_range(user_input, is_gauge=template == WIDGET_TEMPLATE_GAUGE)

    stat_rows = _parse_widget_stat_rows(user_input.get(CONF_STAT_ROWS, ""))
    if template == WIDGET_TEMPLATE_STAT_LIST and not stat_rows:
        raise vol.Invalid("stat_rows_required", path=[CONF_STAT_ROWS])

    battery_devices = _parse_battery_devices(user_input.get(CONF_BATTERY_DEVICES))
    if template == WIDGET_TEMPLATE_BATTERY and not battery_devices:
        raise vol.Invalid("battery_devices_required", path=[CONF_BATTERY_DEVICES])

    flow_nodes = _parse_flow_nodes(user_input.get(CONF_FLOW_NODES))
    if template == WIDGET_TEMPLATE_FLOW and not flow_nodes:
        raise vol.Invalid("flow_nodes_required", path=[CONF_FLOW_NODES])

    low_max, high_min = _coerce_schedule_thresholds(user_input)

    timer_style = str(user_input.get(CONF_SUBTITLE_TIMER_STYLE, "") or "").strip().lower()
    if timer_style not in TIMER_STYLES:
        timer_style = DEFAULT_SUBTITLE_TIMER_STYLE

    history_period = int(user_input.get(CONF_HISTORY_PERIOD, 0) or 0)
    history_period = max(0, min(HISTORY_PERIOD_MAX, history_period))

    poll_interval = int(user_input.get(CONF_WIDGET_POLL_INTERVAL, DEFAULT_WIDGET_POLL_INTERVAL))
    poll_interval = max(WIDGET_POLL_INTERVAL_MIN, min(WIDGET_POLL_INTERVAL_MAX, poll_interval))

    trigger = user_input.get(CONF_WIDGET_TRIGGER_MODE) or WIDGET_TRIGGER_EVENT
    if trigger not in WIDGET_TRIGGER_MODES:
        trigger = WIDGET_TRIGGER_EVENT

    value_scale = user_input.get(CONF_VALUE_SCALE) or DEFAULT_VALUE_SCALE
    if value_scale not in VALUE_SCALES:
        value_scale = DEFAULT_VALUE_SCALE

    # No `or ""` coalesce here, unlike the two blocks above: their default is
    # non-empty, while "" is itself a member of WIDGET_BATTERY_SORTS, so a None
    # falls through the allowlist check to the same place.
    battery_sort = user_input.get(CONF_WIDGET_BATTERY_SORT)
    if battery_sort not in WIDGET_BATTERY_SORTS:
        battery_sort = ""

    action_slots = _parse_action_slots(user_input)

    # "" for not set and for anything the template does not take (density off
    # stat_list), so a template move cannot store a value the server would refuse.
    offered = widget_looks(template)
    looks: dict[str, str] = {}
    for field, (conf, allowed) in WIDGET_LOOKS.items():
        for key in widget_look_keys(conf):
            value = user_input.get(key)
            looks[key] = value if field in offered and value in allowed else ""

    return {
        CONF_ENTITY_ID: entity_id,
        CONF_SLUG: slug,
        CONF_WIDGET_NAME: user_input.get(CONF_WIDGET_NAME, "") or "",
        CONF_WIDGET_TEMPLATE: template,
        **looks,
        CONF_WIDGET_TRIGGER_MODE: trigger,
        CONF_WIDGET_POLL_INTERVAL: poll_interval,
        CONF_VALUE_ATTRIBUTE: user_input.get(CONF_VALUE_ATTRIBUTE, "") or "",
        CONF_UNIT: user_input.get(CONF_UNIT, "") or "",
        CONF_MIN_VALUE: min_v,
        CONF_MAX_VALUE: max_v,
        CONF_VALUE_SCALE: value_scale,
        CONF_SEVERITY: user_input.get(CONF_SEVERITY, "") or "",
        CONF_STAT_ROWS: stat_rows,
        CONF_HISTORY_PERIOD: history_period,
        CONF_START_DATE_ATTRIBUTE: user_input.get(CONF_START_DATE_ATTRIBUTE, "") or "",
        CONF_END_DATE_ATTRIBUTE: user_input.get(CONF_END_DATE_ATTRIBUTE, "") or "",
        CONF_EXPIRED_TEXT: user_input.get(CONF_EXPIRED_TEXT, "") or "",
        CONF_BATTERY_DEVICES: battery_devices,
        CONF_WIDGET_BATTERY_SORT: battery_sort,
        CONF_SCHEDULE_ATTRIBUTES: _parse_csv(user_input.get(CONF_SCHEDULE_ATTRIBUTES, "") or ""),
        CONF_SCHEDULE_START_KEY: user_input.get(CONF_SCHEDULE_START_KEY, "") or DEFAULT_SCHEDULE_START_KEY,
        CONF_SCHEDULE_VALUE_KEY: user_input.get(CONF_SCHEDULE_VALUE_KEY, "") or DEFAULT_SCHEDULE_VALUE_KEY,
        CONF_SCHEDULE_LOW_MAX: low_max,
        CONF_SCHEDULE_HIGH_MIN: high_min,
        CONF_FLOW_NODES: flow_nodes,
        CONF_WIDGET_STALE_AFTER: _coerce_stale_after(user_input.get(CONF_WIDGET_STALE_AFTER)),
        CONF_LABEL: user_input.get(CONF_LABEL, "") or "",
        CONF_LABEL_ATTRIBUTE: user_input.get(CONF_LABEL_ATTRIBUTE, "") or "",
        CONF_SUBTITLE_ATTRIBUTE: user_input.get(CONF_SUBTITLE_ATTRIBUTE, "") or "",
        CONF_SUBTITLE_TIMER_ENTITY: user_input.get(CONF_SUBTITLE_TIMER_ENTITY, "") or "",
        CONF_SUBTITLE_TIMER_ATTRIBUTE: user_input.get(CONF_SUBTITLE_TIMER_ATTRIBUTE, "") or "",
        CONF_SUBTITLE_TIMER_STYLE: timer_style,
        CONF_ICON: user_input.get(CONF_ICON, "") or "",
        CONF_ICON_ATTRIBUTE: user_input.get(CONF_ICON_ATTRIBUTE, "") or "",
        CONF_ACCENT_COLOR: _rgb_to_hex(user_input.get(CONF_ACCENT_COLOR)),
        CONF_ACCENT_COLOR_ATTRIBUTE: user_input.get(CONF_ACCENT_COLOR_ATTRIBUTE, "") or "",
        CONF_BACKGROUND_COLOR: _rgb_to_hex(user_input.get(CONF_BACKGROUND_COLOR)),
        CONF_TEXT_COLOR: _rgb_to_hex(user_input.get(CONF_TEXT_COLOR)),
        **action_slots,
        # Heatmap keys only on a heatmap, so the other templates store what they always have.
        **(_parse_heatmap_fields(user_input) if template == WIDGET_TEMPLATE_HEATMAP else {}),
    }


def _parse_heatmap_fields(user_input: dict) -> dict:
    """Weeks clamped to 1-22, and an aggregate from the allowlist (mean when unknown)."""
    raw = user_input.get(CONF_HEATMAP_WEEKS)
    try:
        weeks = DEFAULT_HEATMAP_WEEKS if raw in (None, "") else int(float(raw))
    except (TypeError, ValueError):
        weeks = DEFAULT_HEATMAP_WEEKS
    aggregate = user_input.get(CONF_HEATMAP_AGGREGATE)
    return {
        CONF_HEATMAP_WEEKS: max(1, min(WIDGET_HEATMAP_WEEKS_MAX, weeks)),
        CONF_HEATMAP_AGGREGATE: aggregate if aggregate in HEATMAP_AGGREGATES else HEATMAP_AGGREGATE_MEAN,
    }


def _default_heatmap_aggregate(info: dict) -> str:
    """change for an entity with a sum statistic (energy, steps), mean for the rest."""
    return HEATMAP_AGGREGATE_CHANGE if info.get("has_sum") else HEATMAP_AGGREGATE_MEAN


def _heatmap_aggregate_supported(aggregate: str | None, info: dict) -> bool:
    """Whether the entity's recorder statistics carry this daily statistic."""
    if aggregate == HEATMAP_AGGREGATE_CHANGE:
        return bool(info.get("has_sum"))
    if aggregate == HEATMAP_AGGREGATE_MEAN:
        return statistics_has_mean(info)
    return aggregate in HEATMAP_AGGREGATES and statistics_has_min_max(info)


def _check_heatmap_aggregate(aggregate: str, info: dict) -> None:
    """Refuse a daily statistic the entity's recorder statistics do not carry."""
    if not _heatmap_aggregate_supported(aggregate, info):
        raise vol.Invalid("heatmap_aggregate_unsupported", path=[CONF_HEATMAP_AGGREGATE])


async def _heatmap_statistics(hass: HomeAssistant, step1: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """A heatmap fills its cells from recorder statistics, so its entity must have some.

    Returns the statistics metadata and the step-1 errors.
    """
    if step1.get(CONF_WIDGET_TEMPLATE) != WIDGET_TEMPLATE_HEATMAP:
        return {}, {}
    info = await async_statistics_info(hass, step1[CONF_ENTITY_ID]) or {}
    return info, {} if info else {CONF_ENTITY_ID: "heatmap_no_statistics"}


def _widget_details_to_config(
    step1: dict[str, Any], user_input: dict[str, Any], statistics_info: dict[str, Any]
) -> tuple[dict | None, dict[str, str]]:
    """The details step's parse: the config to store, or the {field: error} the form shows.

    Shared with the panel preview, so a draft that previews cleanly also saves.
    """
    try:
        widget_cfg = _parse_widget_input(user_input, step1)
        if widget_cfg[CONF_WIDGET_TEMPLATE] == WIDGET_TEMPLATE_HEATMAP:
            _check_heatmap_aggregate(widget_cfg[CONF_HEATMAP_AGGREGATE], statistics_info)
    except vol.Invalid as exc:
        return None, dict.fromkeys(exc.path, str(exc.msg))
    return widget_cfg, {}


def _widget_reconfigure_defaults(data: Mapping[str, Any]) -> dict[str, Any]:
    """The details form's defaults on reconfigure, prepared from the stored config.

    Shared with the panel preview, so a reconfigure draft fills the defaults its save does.
    """
    current = dict(data)
    # stat_rows rehydrate as a list, but a pre-0.37 config may hold fields the
    # strict parser now rejects, so truncate over-cap fields for the form
    # default (storage stays untouched until the user submits).
    if isinstance(current.get(CONF_STAT_ROWS), list):
        current[CONF_STAT_ROWS] = _truncate_row_fields(
            current[CONF_STAT_ROWS],
            {CONF_LABEL: WIDGET_STAT_LABEL_MAX, CONF_UNIT: WIDGET_STAT_UNIT_MAX},
        )
    node_caps = {CONF_NODE_NAME: WIDGET_NODE_NAME_MAX, CONF_ICON: WIDGET_NODE_ICON_MAX}
    for rows_key in (CONF_BATTERY_DEVICES, CONF_FLOW_NODES):
        if isinstance(current.get(rows_key), list):
            current[rows_key] = _truncate_row_fields(current[rows_key], node_caps)
    return current


def _widget_details_defaults(
    template: str, saved: Mapping[str, Any], statistics_info: dict[str, Any]
) -> dict[str, Any]:
    """The details form's defaults: the prepared stored config on reconfigure, {} on add."""
    defaults = dict(saved)
    # A stored choice the (possibly new) entity cannot serve falls back to its default.
    if template == WIDGET_TEMPLATE_HEATMAP and not _heatmap_aggregate_supported(
        defaults.get(CONF_HEATMAP_AGGREGATE), statistics_info
    ):
        defaults[CONF_HEATMAP_AGGREGATE] = _default_heatmap_aggregate(statistics_info)
    return defaults


class PushWardWidgetSubentryFlow(config_entries.ConfigSubentryFlow):
    """Two-step flow for adding / reconfiguring a tracked widget."""

    def __init__(self) -> None:
        super().__init__()
        self._step1_input: dict[str, Any] = {}
        self._is_reconfigure: bool = False
        self._details_defaults: dict[str, Any] = {}
        self._suggestion_offered: bool = False
        # Recorder statistics metadata of a heatmap's entity, read in step 1.
        self._statistics_info: dict[str, Any] = {}

    async def _heatmap_statistics_errors(self, user_input: dict[str, Any]) -> dict[str, str]:
        self._statistics_info, errors = await _heatmap_statistics(self.hass, user_input)
        return errors

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

            if errors := await self._heatmap_statistics_errors(user_input):
                return self.async_show_form(
                    step_id="user",
                    data_schema=_widget_step1_schema(defaults=user_input),
                    errors=errors,
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
            if errors := await self._heatmap_statistics_errors(user_input):
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=_widget_step1_schema(defaults=user_input),
                    errors=errors,
                )
            self._step1_input = user_input
            self._is_reconfigure = True
            self._details_defaults = _widget_reconfigure_defaults(subentry.data)
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
            widget_cfg, errors = _widget_details_to_config(self._step1_input, user_input, self._statistics_info)
            if widget_cfg is not None:
                title = widget_cfg.get(CONF_WIDGET_NAME) or widget_cfg[CONF_SLUG]
                entry = self._get_entry()
                subentry = self._get_reconfigure_subentry() if self._is_reconfigure else None
                if _tracked_elsewhere(
                    entry,
                    SUBENTRY_TYPE_WIDGET,
                    CONF_SLUG,
                    widget_cfg[CONF_SLUG],
                    subentry.subentry_id if subentry else None,
                ):
                    return self.async_abort(reason="already_configured")
                if subentry is not None:
                    return self.async_update_and_abort(
                        entry,
                        subentry,
                        data=widget_cfg,
                        title=title,
                        unique_id=widget_cfg[CONF_SLUG],
                    )
                return self.async_create_entry(
                    title=title,
                    data=widget_cfg,
                    unique_id=widget_cfg[CONF_SLUG],
                )

        defaults = _widget_details_defaults(
            template, self._details_defaults if self._is_reconfigure else {}, self._statistics_info
        )
        errors = _section_errors(WIDGET_SECTIONS, _WIDGET_TOPLEVEL, errors)
        expand = set(errors) & WIDGET_SECTIONS.keys()
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


def _opt_number_key(conf_key: str, current: dict) -> vol.Optional:
    """Build a vol.Optional key for a genuinely optional number.

    Prefills through ``suggested_value`` rather than ``default``: the gauge's
    Required min/max pattern would make an untouched trend or schedule bound
    submit a number, and "unset" is a meaningful state for both (the client
    auto-scales, and iOS derives the schedule bands itself).
    """
    value = current.get(conf_key)
    if value is None or value == "":
        return vol.Optional(conf_key)
    return vol.Optional(conf_key, description={"suggested_value": value})


def _entity_source_key(conf_key: str, current: dict) -> vol.Optional:
    """Build a vol.Optional key for a companion-entity EntitySelector.

    Pre-fills the saved entity_id when present; otherwise leaves the field empty
    so an unset companion submits as absent rather than an invalid empty entity.
    """
    saved = current.get(conf_key)
    if saved:
        return vol.Optional(conf_key, description={"suggested_value": saved})
    return vol.Optional(conf_key)


def _attr_suggest_key(conf_key: str, current: dict) -> vol.Optional:
    """Build a vol.Optional key that pre-fills an optional text, attribute or icon field via suggested_value.

    HA leaves a cleared optional field out of the submit, and a default would put the
    stored value straight back; a suggested value lets the clear reach the parse.
    """
    return vol.Optional(conf_key, description={"suggested_value": current.get(conf_key, "")})


def _parse_csv(value: str) -> list[str]:
    """Parse a comma-separated string into a list of stripped, non-empty items."""
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


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


def _truncate_row_fields(rows: object, caps: dict[str, int]) -> object:
    """Truncate over-cap string fields in a stored row list for reconfigure prefill.

    Storage->form rehydration boundary for the board-tile / stat-row / log-column row
    editors. 0.36.0 stored these with no per-field caps (only a whole-blob length
    limit); the strict submit-time parser now rejects an over-cap field, which would
    dead-end reconfigure of a legacy config. Truncate each named field to its cap so
    the form opens clean and submits; storage stays untouched until the user submits.
    Non-list input passes through so an add flow / legacy string is unaffected.
    """
    if not isinstance(rows, list):
        return rows
    out: list = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        clean = dict(row)
        for field, cap in caps.items():
            value = clean.get(field)
            if isinstance(value, str) and len(value) > cap:
                clean[field] = value[:cap]
        out.append(clean)
    return out


def _blank(value: object) -> bool:
    """True for a missing/empty editor cell (None or an all-whitespace string)."""
    return value is None or (isinstance(value, str) and not value.strip())


def _decompose_steps_rows(raw: object, total_steps: int) -> dict:
    """Split the unified steps-editor rows into the four stored step keys.

    Each row is one step in order: {label?, row?, weight?, color?}. Returns
    {step_labels: {str(i): label}, step_rows: [...], step_weights: [...],
    step_colors: [...]}. The row count must be 0 or exactly total_steps
    (step_length_mismatch otherwise). row/weight are all-or-none across
    rows - a partial column is dropped, since the server needs exactly total_steps
    entries; color keeps a positional blank for each uncolored row.
    """
    empty = {CONF_STEP_LABELS: {}, CONF_STEP_ROWS: [], CONF_STEP_WEIGHTS: [], CONF_STEP_COLORS: []}
    if not isinstance(raw, list):
        return empty
    rows = [r for r in raw if isinstance(r, dict)]
    if not rows:
        return empty
    if len(rows) != total_steps:
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
            if len(label) > STEP_LABEL_MAX:
                raise vol.Invalid("invalid_step_labels", path=[CONF_STEPS_EDITOR])
            labels[str(index)] = label

        row_val = row.get("row")
        if _blank(row_val):
            have_rows = False
        else:
            try:
                height = int(float(row_val))
            except (TypeError, ValueError):
                raise vol.Invalid("invalid_step_rows", path=[CONF_STEPS_EDITOR]) from None
            if not (STEP_ROW_MIN <= height <= STEP_ROW_MAX):
                raise vol.Invalid("invalid_step_rows", path=[CONF_STEPS_EDITOR])
            row_heights.append(height)

        weight_val = row.get("weight")
        if _blank(weight_val):
            have_weights = False
        else:
            try:
                weight = float(weight_val)
            except (TypeError, ValueError):
                raise vol.Invalid("invalid_step_weights", path=[CONF_STEPS_EDITOR]) from None
            if not _is_valid_step_weight(weight):
                raise vol.Invalid("invalid_step_weights", path=[CONF_STEPS_EDITOR])
            weights.append(weight)

        color = str(row.get("color", "") or "").strip()
        if color:
            if not is_valid_color(color):
                raise vol.Invalid("invalid_step_colors", path=[CONF_STEPS_EDITOR])
            have_color = True
        colors.append(color)

    return {
        CONF_STEP_LABELS: labels,
        CONF_STEP_ROWS: row_heights if have_rows else [],
        CONF_STEP_WEIGHTS: weights if have_weights else [],
        CONF_STEP_COLORS: colors if have_color else [],
    }


def _sanitize_step_row_height(value: object) -> int | None:
    """Clamp a stored step-row height into [STEP_ROW_MIN, STEP_ROW_MAX], or None if unparseable."""
    try:
        return max(STEP_ROW_MIN, min(STEP_ROW_MAX, int(float(value))))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _compose_steps_rows(config: dict) -> list[dict]:
    """Compose unified steps-editor rows from the four stored step keys.

    One row per step (exactly total_steps), ordered: label from step_labels[str(i)],
    row/weight/color positional. Returns [] when none of the four keys hold data.

    This is a storage->form rehydration boundary, so it sanitizes legacy values a
    pre-0.37 free-text config could hold but the strict submit-time parser now rejects,
    so the reconfigure form opens clean and submits unchanged:
      - a label over STEP_LABEL_MAX is truncated to the cap;
      - a row height outside [STEP_ROW_MIN, STEP_ROW_MAX] is clamped (matching the
        render-time clamp);
      - a step_rows/step_weights/step_colors list whose length does not match
        total_steps is dropped rather than pre-filling a length-mismatched form (the
        server ignores a wrong-length list too);
      - a non-positive/non-finite weight anywhere drops the whole weight column (the
        render drops the array wholesale on any bad weight);
      - an off-allowlist color is dropped to blank (the render maps it to accent).
    Storage stays untouched until the user submits.
    """
    labels = config.get(CONF_STEP_LABELS) or {}
    rows = config.get(CONF_STEP_ROWS) or []
    weights = config.get(CONF_STEP_WEIGHTS) or []
    colors = config.get(CONF_STEP_COLORS) or []
    if not (labels or rows or weights or colors):
        return []
    total = int(config.get(CONF_TOTAL_STEPS) or DEFAULT_TOTAL_STEPS)
    if total < 1:
        return []
    # A per-step list only survives when it lines up with total_steps; a legacy list of
    # a different length (e.g. left over after total_steps was reduced) would fail the
    # strict length check, so drop it rather than pre-fill an unsubmittable form.
    if len(rows) != total:
        rows = []
    weights_ok = all(_is_valid_step_weight(w) for w in weights)
    if len(weights) != total or not weights_ok:
        weights = []
    if len(colors) != total:
        colors = []
    out: list[dict] = []
    for i in range(1, total + 1):
        row: dict = {}
        label = labels.get(str(i)) if isinstance(labels, dict) else None
        if label:
            row[CONF_LABEL] = str(label)[:STEP_LABEL_MAX]
        if i - 1 < len(rows):
            height = _sanitize_step_row_height(rows[i - 1])
            if height is not None:
                row["row"] = height
        if i - 1 < len(weights):
            row["weight"] = weights[i - 1]
        if i - 1 < len(colors) and colors[i - 1] and is_valid_color(str(colors[i - 1])):
            row["color"] = colors[i - 1]
        out.append(row)
    return out


def _is_number(value: object) -> bool:
    """True for a real int/float (a JSON bool is not a number here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_valid_step_weight(value: object) -> bool:
    """True for a positive, finite step weight (a JSON bool is not a number here)."""
    return _is_number(value) and math.isfinite(value) and value > 0


def _strict_threshold(item: object) -> dict:
    """Validate + normalize one timeline threshold row dict, raising on any problem.

    The value is required and must parse to a finite number; the optional color must
    be a named colour or hex, and the optional label is capped at THRESHOLD_LABEL_MAX.
    Returns a threshold carrying only the non-empty storage keys.
    """
    if not isinstance(item, dict):
        raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS])
    raw_value = item.get("value")
    if _blank(raw_value):
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


def _parse_thresholds(raw: object) -> list[dict]:
    """Parse timeline thresholds from a row-editor list (or the legacy comma string).

    From the form each row is a dict with the storage keys: ``{value, color?, label?}``.
    The legacy string form ('value[:color[:label]], ...', e.g. '25:red:Hot, 20') stays
    accepted. Raises invalid_threshold / too_many_thresholds (past THRESHOLDS_MAX), and
    applies the colour rule and label length cap to the rows.
    """
    if isinstance(raw, list):
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
            if entry.strip():
                raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS])
            continue
        try:
            threshold: dict = {"value": float(parts[0])}
        except ValueError:
            raise vol.Invalid("invalid_threshold", path=[CONF_THRESHOLDS]) from None
        if len(parts) > 1 and parts[1]:
            threshold["color"] = parts[1]
        if len(parts) > 2 and parts[2]:
            threshold["label"] = parts[2]
        result.append(threshold)
    if len(result) > THRESHOLDS_MAX:
        raise vol.Invalid("too_many_thresholds", path=[CONF_THRESHOLDS])
    return result


def _todo_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """The tracked to-do list form: the list plus when and how its reminders go out."""
    d = defaults or {}
    entity_default = {"default": d[CONF_ENTITY_ID]} if d.get(CONF_ENTITY_ID) else {}
    return vol.Schema(
        {
            vol.Required(CONF_ENTITY_ID, **entity_default): EntitySelector(EntitySelectorConfig(domain="todo")),
            vol.Required(
                CONF_TODO_OFFSET_MINUTES, default=d.get(CONF_TODO_OFFSET_MINUTES, DEFAULT_TODO_OFFSET_MINUTES)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=TODO_OFFSET_MAX_MINUTES,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Required(
                CONF_TODO_ALL_DAY_TIME, default=d.get(CONF_TODO_ALL_DAY_TIME, DEFAULT_TODO_ALL_DAY_TIME)
            ): TimeSelector(),
            vol.Required(CONF_TODO_LEVEL, default=d.get(CONF_TODO_LEVEL, DEFAULT_TODO_LEVEL)): SelectSelector(
                SelectSelectorConfig(
                    options=TODO_LEVELS,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="todo_notification_level",
                )
            ),
            vol.Required(
                CONF_TODO_DONE_BUTTON, default=d.get(CONF_TODO_DONE_BUTTON, DEFAULT_TODO_DONE_BUTTON)
            ): BooleanSelector(),
            vol.Required(CONF_TODO_RETRACT, default=d.get(CONF_TODO_RETRACT, DEFAULT_TODO_RETRACT)): BooleanSelector(),
            vol.Required(
                CONF_TODO_MAX_SCHEDULED, default=d.get(CONF_TODO_MAX_SCHEDULED, DEFAULT_TODO_MAX_SCHEDULED)
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=TODO_MAX_SCHEDULED_LIMIT, step=1, mode=NumberSelectorMode.BOX)
            ),
        }
    )


def _todo_data(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Stored form of the to-do list form (NumberSelector hands back floats)."""
    return {
        CONF_ENTITY_ID: user_input[CONF_ENTITY_ID],
        CONF_TODO_OFFSET_MINUTES: int(user_input.get(CONF_TODO_OFFSET_MINUTES, DEFAULT_TODO_OFFSET_MINUTES)),
        CONF_TODO_ALL_DAY_TIME: str(user_input.get(CONF_TODO_ALL_DAY_TIME, DEFAULT_TODO_ALL_DAY_TIME)),
        CONF_TODO_LEVEL: user_input.get(CONF_TODO_LEVEL, DEFAULT_TODO_LEVEL),
        CONF_TODO_DONE_BUTTON: bool(user_input.get(CONF_TODO_DONE_BUTTON, DEFAULT_TODO_DONE_BUTTON)),
        CONF_TODO_RETRACT: bool(user_input.get(CONF_TODO_RETRACT, DEFAULT_TODO_RETRACT)),
        CONF_TODO_MAX_SCHEDULED: int(user_input.get(CONF_TODO_MAX_SCHEDULED, DEFAULT_TODO_MAX_SCHEDULED)),
    }


def _todo_unique_id(entity_id: str) -> str:
    """Subentry unique ids are checked across subentry types; a tracked entity uses the bare id."""
    return f"todo:{entity_id}"


def _todo_title(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    return str((state.attributes.get("friendly_name") if state else None) or entity_id)


class PushWardTodoSubentryFlow(config_entries.ConfigSubentryFlow):
    """Add or reconfigure a tracked to-do list (one form)."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.SubentryFlowResult:
        if user_input is not None:
            data = _todo_data(user_input)
            entity_id = data[CONF_ENTITY_ID]
            if _tracked_elsewhere(self._get_entry(), SUBENTRY_TYPE_TODO, CONF_ENTITY_ID, entity_id):
                return self.async_abort(reason="already_configured")
            return self.async_create_entry(
                title=_todo_title(self.hass, entity_id), data=data, unique_id=_todo_unique_id(entity_id)
            )
        return self.async_show_form(step_id="user", data_schema=_todo_schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            data = _todo_data(user_input)
            entity_id = data[CONF_ENTITY_ID]
            if _tracked_elsewhere(
                self._get_entry(), SUBENTRY_TYPE_TODO, CONF_ENTITY_ID, entity_id, subentry.subentry_id
            ):
                return self.async_abort(reason="already_configured")
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data=data,
                title=_todo_title(self.hass, entity_id),
                unique_id=_todo_unique_id(entity_id),
            )
        return self.async_show_form(step_id="reconfigure", data_schema=_todo_schema(subentry.data))
