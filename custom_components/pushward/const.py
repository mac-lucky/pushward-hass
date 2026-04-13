"""Constants for the PushWard integration."""

import re
from urllib.parse import urlparse

import voluptuous as vol

DOMAIN = "pushward"
SUBENTRY_TYPE_ENTITY = "tracked_entity"

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
CONF_ACCENT_COLOR = "accent_color"
CONF_TOTAL_STEPS = "total_steps"
CONF_CURRENT_STEP_ATTR = "current_step_attribute"
CONF_SEVERITY = "severity"
CONF_SUBTITLE_ATTRIBUTE = "subtitle_attribute"
CONF_STATE_LABELS = "state_labels"
CONF_ENDED_TTL = "ended_ttl"
CONF_STALE_TTL = "stale_ttl"
CONF_COMPLETION_MESSAGE = "completion_message"
CONF_URL = "url"
CONF_SECONDARY_URL = "secondary_url"
CONF_ICON_ATTRIBUTE = "icon_attribute"
CONF_ACCENT_COLOR_ATTRIBUTE = "accent_color_attribute"
CONF_VALUE_ATTRIBUTE = "value_attribute"
CONF_MIN_VALUE = "min_value"
CONF_MAX_VALUE = "max_value"
CONF_UNIT = "unit"
CONF_SERIES = "series"
CONF_SCALE = "scale"
CONF_DECIMALS = "decimals"
CONF_SMOOTHING = "smoothing"
CONF_THRESHOLDS = "thresholds"
CONF_HISTORY_PERIOD = "history_period"

# Defaults
DEFAULT_SERVER_URL = "https://api.pushward.app"
DEFAULT_PRIORITY = 1
DEFAULT_UPDATE_INTERVAL = 5
DEFAULT_TOTAL_STEPS = 1
DEFAULT_SEVERITY = "info"
DEFAULT_MIN_VALUE = 0.0
DEFAULT_MAX_VALUE = 100.0
DEFAULT_SCALE = "linear"
DEFAULT_DECIMALS = 1
DEFAULT_HISTORY_PERIOD = 0

# Validation ranges
PRIORITY_MIN = 0
PRIORITY_MAX = 10
TOTAL_STEPS_MAX = 20
UPDATE_INTERVAL_MIN = 1

# Free-text input length caps.
MAX_TEXT_LEN = 255
MAX_LONG_TEXT_LEN = 1024
MAX_URL_LEN = 2048

# Alert severities
SEVERITIES = ["critical", "warning", "info"]

# Notification interruption levels
NOTIFICATION_LEVELS = ["passive", "active", "time-sensitive", "critical"]

# Templates
TEMPLATES = ["generic", "countdown", "alert", "steps", "gauge", "timeline"]

# Timeline scales
SCALES = ["linear", "logarithmic"]

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


_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


def validate_slug(value: str) -> str:
    """Validate slug matches server pattern: alphanumeric, hyphens, underscores, max 128 chars."""
    if not isinstance(value, str) or not _SLUG_RE.match(value):
        raise vol.Invalid(
            "Slug must start with a letter or digit, contain only letters, digits, hyphens, "
            "or underscores, and be at most 128 characters"
        )
    return value


def normalize_slug(raw: str) -> str:
    """Normalize a raw string into a valid slug (lowercase, alphanumeric + hyphens/underscores)."""
    slug = raw.lower().replace(".", "-").replace(" ", "-")
    slug = re.sub(r"[^a-z0-9_-]", "", slug)
    return re.sub(r"-+", "-", slug).strip("-")
