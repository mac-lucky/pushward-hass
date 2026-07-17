"""Constants for the PushWard integration."""

import re
from typing import NamedTuple
from urllib.parse import urlparse

import voluptuous as vol

DOMAIN = "pushward"
SUBENTRY_TYPE_ENTITY = "tracked_entity"
SUBENTRY_TYPE_WIDGET = "tracked_widget"

CONF_SERVER_URL = "server_url"
CONF_INTEGRATION_KEY = "integration_key"

# Per-entity config keys
CONF_ENTITY_ID = "entity_id"
CONF_SLUG = "slug"
CONF_ACTIVITY_NAME = "activity_name"
CONF_ICON = "icon"
CONF_PRIORITY = "priority"
CONF_TEMPLATE = "template"
CONF_START_STATES = "start_states"
CONF_END_STATES = "end_states"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_PROGRESS_ATTRIBUTE = "progress_attribute"
CONF_REMAINING_TIME_ATTR = "remaining_time_attribute"
# Templates the server accepts live_progress on (pushward-server Content.Validate).
# Both derive the window from a remaining-time source, so the config flow must offer
# one for every template listed here.
LIVE_PROGRESS_TEMPLATES = ("generic", "steps")
# Opt-in for the templates above: with a remaining-time source, interpolate the
# progress bar toward 1.0 by end_date and show a counting-down ETA (server field
# live_progress). On steps it fills the current step, not the whole run.
CONF_LIVE_PROGRESS = "live_progress"
CONF_ACCENT_COLOR = "accent_color"
CONF_TOTAL_STEPS = "total_steps"
CONF_CURRENT_STEP_ATTR = "current_step_attribute"
CONF_SEVERITY = "severity"
CONF_SEVERITY_LABEL = "severity_label"
CONF_SUBTITLE_ATTRIBUTE = "subtitle_attribute"
CONF_STATE_LABELS = "state_labels"
CONF_ENDED_TTL = "ended_ttl"
CONF_STALE_TTL = "stale_ttl"
CONF_DISMISSAL_TTL = "dismissal_ttl"
CONF_COMPLETION_MESSAGE = "completion_message"
CONF_URL = "url"
CONF_SECONDARY_URL = "secondary_url"
CONF_URL_FOREGROUND = "url_foreground"
CONF_URL_TITLE = "url_title"
CONF_SECONDARY_URL_FOREGROUND = "secondary_url_foreground"
CONF_SECONDARY_URL_TITLE = "secondary_url_title"
CONF_TAP_ACTION_URL = "tap_action_url"
CONF_TAP_ACTION_FOREGROUND = "tap_action_foreground"
CONF_ICON_ATTRIBUTE = "icon_attribute"
CONF_ACCENT_COLOR_ATTRIBUTE = "accent_color_attribute"
CONF_VALUE_ATTRIBUTE = "value_attribute"
CONF_MIN_VALUE = "min_value"
CONF_MAX_VALUE = "max_value"
CONF_UNIT = "unit"
CONF_SERIES = "series"
# Timeline template: bind SEPARATE entities as named series (each its own line),
# alongside or instead of the CONF_SERIES attribute map. Stored as a list of
# series dicts ({label, entity_id, attribute?}) with the label frozen at config
# time (the server merges series by label); the config flow edits them as a
# comma-separated `[Label=]entity_id[:attribute]` string (mirrors board tiles).
CONF_SERIES_ENTITIES = "series_entities"
# Timeline template: label of the series whose value drives the headline number
# and the compact high/low range on iOS. Empty = auto (the tracked entity's own
# series when one exists, else the first configured series entity).
CONF_PRIMARY_SERIES = "primary_series"
CONF_SCALE = "scale"
CONF_DECIMALS = "decimals"
CONF_SMOOTHING = "smoothing"
CONF_THRESHOLDS = "thresholds"
CONF_HISTORY_PERIOD = "history_period"
CONF_SOUND = "sound"
CONF_WARNING_THRESHOLD = "warning_threshold"
CONF_ALARM = "alarm"
CONF_SNOOZE_SECONDS = "snooze_seconds"
CONF_STEP_LABELS = "step_labels"
CONF_STEP_ROWS = "step_rows"
# Steps template: per-step relative width (positive numbers) and per-step color.
# Both are positional and must carry exactly total_steps entries or the server
# 400s; an empty step_colors entry falls back to accent_color.
CONF_STEP_WEIGHTS = "step_weights"
CONF_STEP_COLORS = "step_colors"
CONF_FIRED_AT_ATTRIBUTE = "fired_at_attribute"
CONF_UNITS = "units"
# Board template: 1-4 tiles, each bound to a separate entity. Stored as a list of
# tile dicts ({label, entity_id, value_attribute?, unit?, icon?}); the config flow
# edits them as a comma-separated string (mirrors widget stat_rows).
CONF_TILES = "tiles"
# Log template: optional attribute supplying the per-line level (info/warn/error).
CONF_LOG_LEVEL_ATTRIBUTE = "log_level_attribute"
# Log template: optional extra columns composed into each line's text. A freeform
# comma-separated string mirroring the board-tile format — each column is
# `[Label=]source[|unit]`, where source is a bare attribute of the tracked entity
# (`brightness`), another entity's state (`binary_sensor.door`), or another
# entity's attribute (`sensor.temp:temperature`). Stored as a list of column dicts
# ({label?, entity_id?, attribute?, unit?}); the config flow edits them as a string.
CONF_LOG_COLUMNS = "log_columns"

# Companion source entities — read a value from a SEPARATE entity instead of an
# attribute of the tracked entity. Empty => use the tracked entity. When set,
# the paired *_attr* key (if any) is read as an attribute of the companion;
# leave the attribute empty to use the companion's own state.
CONF_REMAINING_TIME_ENTITY = "remaining_time_entity"
CONF_PROGRESS_ENTITY = "progress_entity"
CONF_VALUE_ENTITY = "value_entity"
CONF_CURRENT_STEP_ENTITY = "current_step_entity"
CONF_FIRED_AT_ENTITY = "fired_at_entity"
CONF_SUBTITLE_ENTITY = "subtitle_entity"
CONF_BACKGROUND_COLOR = "background_color"
CONF_BACKGROUND_COLOR_ATTRIBUTE = "background_color_attribute"
CONF_TEXT_COLOR = "text_color"
CONF_TEXT_COLOR_ATTRIBUTE = "text_color_attribute"

# Widget-specific config keys
CONF_WIDGET_TEMPLATE = "widget_template"
CONF_WIDGET_NAME = "widget_name"
CONF_WIDGET_TRIGGER_MODE = "widget_trigger_mode"
CONF_WIDGET_POLL_INTERVAL = "widget_poll_interval"
CONF_LABEL = "label"
CONF_LABEL_ATTRIBUTE = "label_attribute"
CONF_STAT_ROWS = "stat_rows"
# Widget progress template: how to read the bound value. The server wants a
# 0.0-1.0 fraction, but plenty of HA sensors report 0-100. Not CONF_SCALE, which
# is the timeline chart's linear/log axis.
CONF_VALUE_SCALE = "value_scale"

# Defaults
DEFAULT_SERVER_URL = "https://api.pushward.app"
APP_STORE_URL = "https://apps.apple.com/app/id6759689999"
DEFAULT_PRIORITY = 1
DEFAULT_UPDATE_INTERVAL = 5
DEFAULT_TOTAL_STEPS = 1
DEFAULT_SEVERITY = "info"
DEFAULT_MIN_VALUE = 0.0
DEFAULT_MAX_VALUE = 100.0
DEFAULT_SCALE = "linear"
DEFAULT_VALUE_SCALE = "auto"
DEFAULT_DECIMALS = 1
DEFAULT_HISTORY_PERIOD = 0
DEFAULT_TAP_ACTION_FOREGROUND = True

# Validation ranges
PRIORITY_MIN = 0
PRIORITY_MAX = 10
TOTAL_STEPS_MAX = 64  # server MaxTotalSteps
UPDATE_INTERVAL_MIN = 1
WARNING_THRESHOLD_MAX = 86400  # 24 h
SNOOZE_SECONDS_MIN = 60
SNOOZE_SECONDS_MAX = 3600
# Mirrors the server's dismissal_ttl bound (0 = remove on end, 14400 = the iOS 4h ceiling).
DISMISSAL_TTL_MIN = 0
DISMISSAL_TTL_MAX = 14400
# Mirrors the server's ended_ttl / stale_ttl bounds (services.yaml declares the same).
ACTIVITY_TTL_MIN = 1
ACTIVITY_TTL_MAX = 2592000  # 30 d

# Free-text input length caps.
MAX_TEXT_LEN = 255
MAX_LONG_TEXT_LEN = 1024
MAX_URL_LEN = 2048
MAX_SLUG_LEN = 128
# Optional alert badge-text override; mirrors the server's 32-rune cap.
MAX_SEVERITY_LABEL_LEN = 32
MAX_TAP_ACTION_TITLE_LEN = 64
MAX_TAP_ACTION_ICON_LEN = 64
MAX_TAP_ACTION_BODY_LEN = 1024  # server maxTapActionBodyRunes
MAX_TAP_ACTION_HEADERS_LEN = 1024  # server maxTapActionHeadersBytes (sum of name+value byte lengths)
# Reply-with-text placeholder / send-button label caps; mirror the server's 64.
MAX_TEXT_INPUT_LABEL_LEN = 64

# Tap-action / action-button HTTP routing — mirrors pushward-server action_validation.go.
# method/headers/body are only valid on http(s) URLs; custom-scheme URLs (homeassistant://,
# youtube://, …) are tap targets only.
TAP_ACTION_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
# Schemes the server rejects outright for any URL (XSS / local-file vectors).
DANGEROUS_URL_SCHEMES = frozenset({"javascript", "data", "file", "vbscript"})

# Activity wire-format states (must match pushward-server enum).
ACTIVITY_STATE_ONGOING = "ongoing"
ACTIVITY_STATE_ENDED = "ended"
ACTIVITY_STATES = [ACTIVITY_STATE_ONGOING, ACTIVITY_STATE_ENDED]

# Alert severities
SEVERITIES = ["critical", "warning", "info"]

# Notification interruption levels
# "critical" is kept for backward compat — UI dropdown hides it until Apple approves entitlement
NOTIFICATION_LEVELS = ["passive", "active", "time-sensitive", "critical"]

# Templates
TEMPLATES = ["generic", "countdown", "alert", "steps", "gauge", "timeline", "board", "log"]

# Widget templates (server: pushward-server/internal/model/widget.go)
WIDGET_TEMPLATE_VALUE = "value"
WIDGET_TEMPLATE_PROGRESS = "progress"
WIDGET_TEMPLATE_GAUGE = "gauge"
WIDGET_TEMPLATE_STATUS = "status"
WIDGET_TEMPLATE_STAT_LIST = "stat_list"
WIDGET_TEMPLATES = [
    WIDGET_TEMPLATE_VALUE,
    WIDGET_TEMPLATE_PROGRESS,
    WIDGET_TEMPLATE_GAUGE,
    WIDGET_TEMPLATE_STATUS,
    WIDGET_TEMPLATE_STAT_LIST,
]

# Widget trigger modes
WIDGET_TRIGGER_EVENT = "event"
WIDGET_TRIGGER_POLL = "poll"
WIDGET_TRIGGER_MODES = [WIDGET_TRIGGER_EVENT, WIDGET_TRIGGER_POLL]

# Widget poll interval bounds (seconds)
WIDGET_POLL_INTERVAL_MIN = 10
WIDGET_POLL_INTERVAL_MAX = 3600
DEFAULT_WIDGET_POLL_INTERVAL = 60

# Server caps for widget content fields (mirrors widget.go validation).
WIDGET_MAX_STAT_ROWS = 6
WIDGET_STAT_LABEL_MAX = 32
WIDGET_STAT_VALUE_MAX = 32
WIDGET_STAT_UNIT_MAX = 16
WIDGET_UNIT_MAX = 32
WIDGET_LABEL_MAX = 256
WIDGET_SUBTITLE_MAX = 256
WIDGET_NAME_MAX = 256

# Widget severities (mirrors server validWidgetSeverities)
WIDGET_SEVERITIES = ["", "info", "warning", "critical", "success"]

# How the progress widget reads its bound value. Client-side only -- the server
# always receives the 0.0-1.0 fraction it expects.
VALUE_SCALE_AUTO = "auto"
VALUE_SCALE_FRACTION = "fraction"
VALUE_SCALE_PERCENT = "percent"
VALUE_SCALES = [VALUE_SCALE_AUTO, VALUE_SCALE_FRACTION, VALUE_SCALE_PERCENT]

# How far past 1.0 a value can land and still read as a fraction. A ratio like
# elapsed/total overshoots by rounding noise right as it completes, and reading
# that as a percent would drop a finished bar to nearly empty.
FRACTION_OVERSHOOT_TOLERANCE = 0.05

# Widget trend annotations (mirrors server validWidgetTrends)
WIDGET_TREND_UP = "up"
WIDGET_TREND_DOWN = "down"
WIDGET_TREND_FLAT = "flat"

# Named colors the server accepts (mirrors server-side ValidateColor's
# validNamedColors; must stay in sync with
# pushward-server/internal/model/activity.go). Used both for the is_valid_color
# check in content_mapper and as the option set for the named-color dropdowns in
# the config flow (tile color, and the timeline thresholds color in a later phase).
NAMED_COLORS = (
    "red",
    "orange",
    "yellow",
    "green",
    "blue",
    "purple",
    "pink",
    "indigo",
    "teal",
    "cyan",
    "mint",
    "brown",
)

# Board template caps (mirror pushward-server/internal/model/activity.go).
# A board carries 1-BOARD_MAX_TILES tiles (RFC-7396 atomic replace). Per tile:
# label (required, ≤32), value (string, required, ≤16 — a string so "Open"/"On"/
# numbers all render), unit (≤8), icon (≤128, SF Symbol or mdi:), color (named or
# hex, ValidateColor), trend (up/down/flat), url_action (per-tile TapAction).
BOARD_MAX_TILES = 4
BOARD_TILE_LABEL_MAX = 32
BOARD_TILE_VALUE_MAX = 16
BOARD_TILE_UNIT_MAX = 8
BOARD_TILE_ICON_MAX = 128
# Per-tile trend arrows reuse the widget trend vocabulary (same wire values).
BOARD_TRENDS = (WIDGET_TREND_UP, WIDGET_TREND_DOWN, WIDGET_TREND_FLAT)

# Log template caps (mirror pushward-server/internal/model/activity.go).
# A log carries 1-LOG_MAX_LINES lines (newest-first, atomic replace). Per line:
# text (required, ≤512), at (optional unix timestamp int), level (optional —
# info/warn/error; NB: a different set from the alert template's `severity`).
# `log_backlog` is SERVER-OWNED and must never be sent by this integration.
LOG_MAX_LINES = 20
LOG_LINE_TEXT_MAX = 512
LOG_LEVELS = ("info", "warn", "error")
# Log column composition (client-side only — composed into the single `text`
# string; no server contract change). At most LOG_MAX_COLUMNS columns per line,
# each rendered value capped at LOG_COLUMN_VALUE_MAX and label at LOG_COLUMN_LABEL_MAX.
LOG_MAX_COLUMNS = 6
LOG_COLUMN_VALUE_MAX = 64
LOG_COLUMN_LABEL_MAX = 32

# Timeline template caps (mirror pushward-server/internal/model/activity.go).
# A timeline carries 1-TIMELINE_MAX_SERIES named series, merged by label per
# RFC 7396; each label (a value-map key) is capped at TIMELINE_SERIES_LABEL_MAX runes.
TIMELINE_MAX_SERIES = 10
TIMELINE_SERIES_LABEL_MAX = 32
# Timeline sparkline thresholds: at most THRESHOLDS_MAX reference lines, each with a
# numeric value, an optional color (ValidateColor), and an optional label capped at
# THRESHOLD_LABEL_MAX runes.
THRESHOLDS_MAX = 5
THRESHOLD_LABEL_MAX = 12

# Timeline scales
SCALES = ["linear", "logarithmic"]

# Must stay in sync with server UpdateActivityRequest.sound.
SOUNDS = ("default", "chime", "alert", "success", "warning", "bell", "ding", "buzz", "notification")

# Usage/quota coordinator poll interval (seconds). Usage moves slowly and
# /auth/me is per-IP rate-limited, so poll conservatively.
USAGE_UPDATE_INTERVAL = 900  # 15 min

# /auth/me period-reset timestamp keys. Notifications reset daily on premium
# (the daily key) and monthly on free (the monthly key, used as the fallback).
QUOTA_RESET_KEY = "quota_resets_at"
QUOTA_DAILY_RESET_KEY = "quota_resets_day_at"


class MeteredResource(NamedTuple):
    """A metered account resource tracked for usage-limit repair issues."""

    used_key: str
    limit_key: str
    translation_key: str
    # Preferred /auth/me reset key; falls back to QUOTA_RESET_KEY when absent.
    reset_key: str


# Metered resources checked for usage-limit repair issues. Mirrors the metered
# keys in USAGE_SENSORS (sensor.py); the drift-guard test keeps the pair in sync.
# Each resource carries its own translation key so the issue text — including
# pluralization — is fully localizable, rather than injecting an English resource
# name as a placeholder (which would arrive untranslated in every locale).
USAGE_LIMIT_RESOURCES = (
    MeteredResource("notifications_used", "notifications_limit", "usage_limit_notifications", QUOTA_DAILY_RESET_KEY),
    MeteredResource(
        "live_activity_updates_used", "live_activity_updates_limit", "usage_limit_live_activity", QUOTA_RESET_KEY
    ),
    MeteredResource("widget_updates_used", "widget_updates_limit", "usage_limit_widgets", QUOTA_RESET_KEY),
    MeteredResource("emails_used", "emails_limit", "usage_limit_emails", QUOTA_RESET_KEY),
)


def usage_limit_issue_id(entry_id: str, used_key: str) -> str:
    """Stable repair-issue id for a per-entry, per-resource usage limit."""
    return f"usage_limit_{entry_id}_{used_key}"


# API retry
MAX_RETRIES = 5
RETRY_BASE_DELAY = 1  # seconds
RETRY_MAX_DELAY = 30  # seconds
MAX_CONCURRENT_REQUESTS = 5  # max simultaneous API request+retry loops

END_DELAY_SECONDS = 5

# Domain-based defaults for entity configuration
DOMAIN_DEFAULTS: dict[str, dict] = {
    "binary_sensor": {
        "icon": "mdi:toggle-switch-variant",
        "start_states": ["on"],
        "end_states": ["off"],
    },
    "switch": {
        "icon": "mdi:toggle-switch-variant",
        "start_states": ["on"],
        "end_states": ["off"],
    },
    "climate": {
        "icon": "mdi:thermostat",
        "start_states": ["heating", "cooling"],
        "end_states": ["off", "idle"],
    },
    "vacuum": {
        "icon": "mdi:robot-vacuum",
        "start_states": ["cleaning"],
        "end_states": ["docked", "idle"],
    },
    "media_player": {
        "icon": "mdi:cast",
        "start_states": ["playing"],
        "end_states": ["off", "idle", "paused"],
    },
    "lock": {
        "icon": "mdi:lock",
        "start_states": ["unlocked"],
        "end_states": ["locked"],
    },
    "cover": {
        "icon": "mdi:window-open",
        "start_states": ["opening", "closing"],
        "end_states": ["open", "closed"],
    },
    "timer": {
        "icon": "mdi:timer-outline",
        "start_states": ["active"],
        "end_states": ["idle", "paused"],
    },
    "sensor": {
        "icon": "mdi:eye",
        "start_states": [],
        "end_states": [],
    },
    "light": {
        "icon": "mdi:lightbulb",
        "start_states": ["on"],
        "end_states": ["off"],
    },
    "fan": {
        "icon": "mdi:fan",
        "start_states": ["on"],
        "end_states": ["off"],
    },
    "weather": {
        "icon": "mdi:weather-cloudy",
        "start_states": [],
        "end_states": [],
    },
    "update": {
        "icon": "mdi:package-up",
        "start_states": ["on"],
        "end_states": ["off"],
    },
    "water_heater": {
        "icon": "mdi:water-boiler",
        "start_states": ["heating"],
        "end_states": ["off", "idle"],
    },
}

# Device class → MDI icon mapping (mirrors HA frontend icon tables).
# Modern HA integrations use frontend-only icon translations, so
# state.attributes["icon"] and entity_registry.original_icon are empty.
# This table lets us resolve a sensible icon from the backend.
DEVICE_CLASS_ICONS: dict[str, str] = {
    # binary_sensor device classes (using "on" state icon)
    "binary_sensor.battery": "mdi:battery-alert",
    "binary_sensor.battery_charging": "mdi:battery-charging",
    "binary_sensor.carbon_monoxide": "mdi:smoke-detector-alert",
    "binary_sensor.cold": "mdi:snowflake",
    "binary_sensor.connectivity": "mdi:check-network",
    "binary_sensor.door": "mdi:door-open",
    "binary_sensor.garage_door": "mdi:garage-open",
    "binary_sensor.gas": "mdi:fire-alert",
    "binary_sensor.heat": "mdi:fire",
    "binary_sensor.light": "mdi:brightness-7",
    "binary_sensor.lock": "mdi:lock-open",
    "binary_sensor.moisture": "mdi:water",
    "binary_sensor.motion": "mdi:motion-sensor",
    "binary_sensor.moving": "mdi:motion-sensor",
    "binary_sensor.occupancy": "mdi:home",
    "binary_sensor.opening": "mdi:square-rounded-outline",
    "binary_sensor.plug": "mdi:power-plug",
    "binary_sensor.power": "mdi:power",
    "binary_sensor.presence": "mdi:home",
    "binary_sensor.problem": "mdi:alert-circle",
    "binary_sensor.running": "mdi:play",
    "binary_sensor.safety": "mdi:alert-circle",
    "binary_sensor.smoke": "mdi:smoke-detector-variant-alert",
    "binary_sensor.sound": "mdi:ear-hearing",
    "binary_sensor.tamper": "mdi:alert-circle",
    "binary_sensor.update": "mdi:package-up",
    "binary_sensor.vibration": "mdi:vibrate",
    "binary_sensor.window": "mdi:window-open",
    # sensor device classes
    "sensor.apparent_power": "mdi:flash",
    "sensor.aqi": "mdi:air-filter",
    "sensor.atmospheric_pressure": "mdi:thermometer-lines",
    "sensor.battery": "mdi:battery",
    "sensor.carbon_dioxide": "mdi:molecule-co2",
    "sensor.carbon_monoxide": "mdi:molecule-co",
    "sensor.current": "mdi:current-ac",
    "sensor.data_rate": "mdi:transmission-tower",
    "sensor.data_size": "mdi:database",
    "sensor.date": "mdi:calendar",
    "sensor.distance": "mdi:arrow-left-right",
    "sensor.duration": "mdi:progress-clock",
    "sensor.energy": "mdi:lightning-bolt",
    "sensor.energy_storage": "mdi:battery-plus",
    "sensor.frequency": "mdi:sine-wave",
    "sensor.gas": "mdi:meter-gas",
    "sensor.humidity": "mdi:water-percent",
    "sensor.illuminance": "mdi:brightness-5",
    "sensor.irradiance": "mdi:sun-wireless",
    "sensor.moisture": "mdi:water-percent",
    "sensor.monetary": "mdi:cash",
    "sensor.ph": "mdi:ph",
    "sensor.pm1": "mdi:dots-hexagon",
    "sensor.pm10": "mdi:dots-hexagon",
    "sensor.pm25": "mdi:dots-hexagon",
    "sensor.power": "mdi:flash",
    "sensor.power_factor": "mdi:angle-acute",
    "sensor.precipitation": "mdi:weather-rainy",
    "sensor.precipitation_intensity": "mdi:weather-pouring",
    "sensor.pressure": "mdi:gauge",
    "sensor.reactive_power": "mdi:flash",
    "sensor.signal_strength": "mdi:wifi",
    "sensor.sound_pressure": "mdi:ear-hearing",
    "sensor.speed": "mdi:speedometer",
    "sensor.temperature": "mdi:thermometer",
    "sensor.timestamp": "mdi:clock",
    "sensor.volatile_organic_compounds": "mdi:molecule-co2",
    "sensor.voltage": "mdi:sine-wave",
    "sensor.volume": "mdi:car-coolant-level",
    "sensor.volume_flow_rate": "mdi:waves-arrow-right",
    "sensor.volume_storage": "mdi:storage-tank",
    "sensor.water": "mdi:water",
    "sensor.weight": "mdi:weight",
    "sensor.wind_direction": "mdi:compass-rose",
    "sensor.wind_speed": "mdi:weather-windy",
    # cover device classes
    "cover.awning": "mdi:awning-outline",
    "cover.blind": "mdi:blinds-open",
    "cover.curtain": "mdi:curtains",
    "cover.damper": "mdi:circle",
    "cover.door": "mdi:door-open",
    "cover.garage": "mdi:garage-open",
    "cover.gate": "mdi:gate-open",
    "cover.shade": "mdi:roller-shade",
    "cover.shutter": "mdi:window-shutter-open",
    "cover.window": "mdi:window-open",
    # switch device classes
    "switch.outlet": "mdi:power-plug",
    "switch.switch": "mdi:toggle-switch-variant",
    # button device classes
    "button.identify": "mdi:crosshairs-question",
    "button.restart": "mdi:restart",
    "button.update": "mdi:package-up",
    # update device classes
    "update.firmware": "mdi:chip",
}


def validate_url(value: str) -> str:
    """Validate URL uses http or https scheme."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise vol.Invalid("URL must use http:// or https:// scheme")
    if not parsed.netloc:
        raise vol.Invalid("URL must include a host")
    return value


def validate_tap_action_url(value: str) -> str:
    """Validate a tap-action / action-button URL the way the server does.

    Unlike ``validate_url`` (http/https only), the server accepts any scheme except
    javascript/data/file/vbscript: custom schemes like ``homeassistant://`` or
    ``youtube://`` open the matching app. http(s) URLs still require a host.
    Mirrors pushward-server ValidateActionURL.
    """
    if not isinstance(value, str) or not value:
        raise vol.Invalid("URL must be a non-empty string")
    if len(value) > MAX_URL_LEN:
        raise vol.Invalid(f"URL must be at most {MAX_URL_LEN} characters")
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if not scheme:
        raise vol.Invalid("URL must include a scheme (e.g. https:// or homeassistant://)")
    if scheme in DANGEROUS_URL_SCHEMES:
        raise vol.Invalid(f"URL scheme '{scheme}' is not allowed")
    if scheme in ("http", "https") and not parsed.netloc:
        raise vol.Invalid("http(s) URL must include a host")
    return value


# RFC 7230 token: the only characters allowed in an HTTP header field-name.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def validate_action_headers(value: dict) -> dict:
    """Validate action HTTP headers the way the server does (action_validation.go).

    Rejects non-token header names, CR/LF/NUL in values, and a combined name+value
    size over ``MAX_TAP_ACTION_HEADERS_LEN`` bytes — so the caller gets a clear HA
    error instead of a server 400. The byte cap mirrors the server's Go ``len()``,
    hence ``encode("utf-8")``.
    """
    total = 0
    for name, val in value.items():
        if not _HEADER_NAME_RE.match(name):
            raise vol.Invalid(f"header name {name!r} is not a valid HTTP token (RFC 7230)")
        if any(ch in val for ch in "\r\n\x00"):
            raise vol.Invalid(f"header {name!r} value contains a CR, LF, or NUL character")
        total += len(name.encode("utf-8")) + len(val.encode("utf-8"))
    if total > MAX_TAP_ACTION_HEADERS_LEN:
        raise vol.Invalid(f"headers total size must not exceed {MAX_TAP_ACTION_HEADERS_LEN} bytes")
    return value


# Server ParseDuration: plain integer seconds, or h/m/s unit groups in that order.
_DURATION_RE = re.compile(r"\d+|(?:\d+h)?(?:\d+m)?(?:\d+s)?")


def validate_duration(value: object) -> object:
    """Validate a countdown duration: int seconds (>=1) or a Go-style string ("60s", "1h30m").

    Mirrors pushward-server ParseDuration + ResolveDuration (which rejects a
    non-positive result), so "0", "-5", "abc", and "1x" fail here instead of as a
    server 400. Integer/float input is coerced to int; string input passes through
    for the server to expand.
    """
    if isinstance(value, bool):
        raise vol.Invalid("duration must be seconds or a duration string")
    if isinstance(value, (int, float)):
        seconds = int(value)
        if seconds < 1:
            raise vol.Invalid("duration must be at least 1 second")
        return seconds
    if not isinstance(value, str):
        raise vol.Invalid("duration must be an integer or a duration string")
    text = value.strip()
    if not _DURATION_RE.fullmatch(text) or not any(ch in "123456789" for ch in text):
        raise vol.Invalid('invalid duration; use seconds (90) or units like "1h30m"')
    return value


_SLUG_RE = re.compile(rf"^[a-zA-Z0-9][a-zA-Z0-9_-]{{0,{MAX_SLUG_LEN - 1}}}$")


def validate_slug(value: str) -> str:
    """Validate slug matches server pattern: must start with an alphanumeric, contain only
    alphanumerics, hyphens, or underscores, and be at most MAX_SLUG_LEN chars."""
    if not isinstance(value, str) or not _SLUG_RE.match(value):
        raise vol.Invalid(
            "Slug must start with a letter or digit, contain only letters, digits, hyphens, "
            f"or underscores, and be at most {MAX_SLUG_LEN} characters"
        )
    return value


def normalize_slug(raw: str) -> str:
    """Normalize a raw string into a slug that satisfies the server pattern.

    The server requires the slug to start with an alphanumeric, contain only
    alphanumerics / hyphens / underscores, and be at most MAX_SLUG_LEN chars.
    """
    slug = raw.lower().replace(".", "-").replace(" ", "-")
    slug = re.sub(r"[^a-z0-9_-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-_")
    return slug[:MAX_SLUG_LEN]
