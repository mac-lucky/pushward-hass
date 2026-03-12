"""Constants for the PushWard integration."""

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

# Defaults
DEFAULT_SERVER_URL = "https://api.pushward.app"
DEFAULT_PRIORITY = 1
DEFAULT_TEMPLATE = "generic"
DEFAULT_UPDATE_INTERVAL = 5
DEFAULT_TOTAL_STEPS = 1
DEFAULT_SEVERITY = "info"

# Alert severities
SEVERITIES = ["critical", "warning", "info"]

# Templates
TEMPLATES = ["generic", "countdown", "alert", "pipeline"]

# API retry
MAX_RETRIES = 5
RETRY_BASE_DELAY = 1  # seconds
RETRY_MAX_DELAY = 30  # seconds

# End activity delay (two-phase end)
END_DELAY_SECONDS = 5

# Domain-based defaults for entity configuration
DOMAIN_DEFAULTS: dict[str, dict] = {
    "binary_sensor": {
        "icon": "circle.fill",
        "start_states": ["on"],
        "end_states": ["off"],
    },
    "switch": {
        "icon": "power",
        "start_states": ["on"],
        "end_states": ["off"],
    },
    "climate": {
        "icon": "thermometer",
        "start_states": ["heating", "cooling"],
        "end_states": ["off", "idle"],
    },
    "vacuum": {
        "icon": "fan",
        "start_states": ["cleaning"],
        "end_states": ["docked", "idle"],
    },
    "media_player": {
        "icon": "play.circle.fill",
        "start_states": ["playing"],
        "end_states": ["off", "idle", "paused"],
    },
    "lock": {
        "icon": "lock.fill",
        "start_states": ["unlocked"],
        "end_states": ["locked"],
    },
    "cover": {
        "icon": "rectangle.portrait.arrowtriangle.2.outward",
        "start_states": ["opening", "closing"],
        "end_states": ["open", "closed"],
    },
    "timer": {
        "icon": "timer",
        "start_states": ["active"],
        "end_states": ["idle", "paused"],
    },
    "sensor": {
        "icon": "gauge",
        "start_states": [],
        "end_states": [],
    },
}
