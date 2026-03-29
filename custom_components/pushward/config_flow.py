"""Config flow and subentry flow for PushWard integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    AttributeSelector,
    AttributeSelectorConfig,
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
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ACTIVITY_NAME,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_END_STATES,
    CONF_ENDED_TTL,
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_INTEGRATION_KEY,
    CONF_PRIORITY,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SECONDARY_URL,
    CONF_SERVER_URL,
    CONF_SEVERITY,
    CONF_SLUG,
    CONF_STALE_TTL,
    CONF_START_STATES,
    CONF_STATE_LABELS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_TEMPLATE,
    CONF_TOTAL_STEPS,
    CONF_UPDATE_INTERVAL,
    CONF_URL,
    DEFAULT_PRIORITY,
    DEFAULT_SERVER_URL,
    DEFAULT_SEVERITY,
    DEFAULT_TOTAL_STEPS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PRIORITY_MAX,
    PRIORITY_MIN,
    SEVERITIES,
    SUBENTRY_TYPE_ENTITY,
    TEMPLATES,
    TOTAL_STEPS_MAX,
    UPDATE_INTERVAL_MIN,
    normalize_slug,
    validate_url,
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


def _details_schema(
    entity_id: str,
    template: str,
    defaults: dict | None = None,
    hass: HomeAssistant | None = None,
) -> vol.Schema:
    """Build step-2 schema with all config fields and dynamic selectors."""
    d = defaults or {}
    domain = entity_id.split(".")[0] if "." in entity_id else ""
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

    # ColorRGBSelector requires a valid [r,g,b] default — omit if no color saved
    accent_rgb = _hex_to_rgb(d.get(CONF_ACCENT_COLOR, ""))
    accent_key = (
        vol.Optional(CONF_ACCENT_COLOR, default=accent_rgb)
        if accent_rgb is not None
        else vol.Optional(CONF_ACCENT_COLOR)
    )

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
        fields[
            vol.Optional(
                CONF_PROGRESS_ATTRIBUTE,
                description={"suggested_value": d.get(CONF_PROGRESS_ATTRIBUTE, "")},
            )
        ] = attr_selector
    if template in ("generic", "countdown"):
        fields[
            vol.Optional(
                CONF_REMAINING_TIME_ATTR,
                description={"suggested_value": d.get(CONF_REMAINING_TIME_ATTR, "")},
            )
        ] = attr_selector
    if template == "steps":
        fields[
            vol.Optional(
                CONF_TOTAL_STEPS,
                default=d.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=1, max=TOTAL_STEPS_MAX))
        fields[
            vol.Optional(
                CONF_CURRENT_STEP_ATTR,
                description={"suggested_value": d.get(CONF_CURRENT_STEP_ATTR, "")},
            )
        ] = attr_selector
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

    # --- Identity fields ---
    fields[vol.Optional(CONF_SLUG, default=d.get(CONF_SLUG, ""))] = str
    fields[
        vol.Optional(
            CONF_ACTIVITY_NAME,
            default=d.get(CONF_ACTIVITY_NAME, ""),
        )
    ] = str
    fields[
        vol.Optional(
            CONF_ICON,
            description={"suggested_value": d.get(CONF_ICON, "")},
        )
    ] = IconSelector(IconSelectorConfig(placeholder="mdi:bell"))
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
            CONF_UPDATE_INTERVAL,
            default=d.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        )
    ] = vol.All(vol.Coerce(int), vol.Range(min=UPDATE_INTERVAL_MIN))

    # --- Optional fields ---
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
    ] = str
    fields[
        vol.Optional(
            CONF_COMPLETION_MESSAGE,
            default=d.get(CONF_COMPLETION_MESSAGE, ""),
        )
    ] = str
    fields[accent_key] = ColorRGBSelector()
    fields[
        vol.Optional(
            CONF_ACCENT_COLOR_ATTRIBUTE,
            description={"suggested_value": d.get(CONF_ACCENT_COLOR_ATTRIBUTE, "")},
        )
    ] = attr_selector
    fields[
        vol.Optional(
            CONF_URL,
            default=d.get(CONF_URL, ""),
        )
    ] = str
    fields[
        vol.Optional(
            CONF_SECONDARY_URL,
            default=d.get(CONF_SECONDARY_URL, ""),
        )
    ] = str
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


def _parse_entity_input(user_input: dict) -> dict:
    """Normalize user input into an entity config dict."""
    entity_id = user_input[CONF_ENTITY_ID]
    raw_slug = user_input.get(CONF_SLUG, "").strip()
    slug = (normalize_slug(raw_slug) if raw_slug else "") or sanitize_slug(entity_id)

    domain = entity_id.split(".")[0] if "." in entity_id else ""
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

    # Validate and convert URLs
    url = user_input.get(CONF_URL, "").strip()
    secondary_url = user_input.get(CONF_SECONDARY_URL, "").strip()
    url_errors: dict[str, str] = {}
    if url:
        try:
            validate_url(url)
        except vol.Invalid:
            url_errors[CONF_URL] = "invalid_url"
    if secondary_url:
        try:
            validate_url(secondary_url)
        except vol.Invalid:
            url_errors[CONF_SECONDARY_URL] = "invalid_url"
    if url_errors:
        raise vol.Invalid("invalid_url", path=list(url_errors.keys()))

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
        CONF_REMAINING_TIME_ATTR: user_input.get(CONF_REMAINING_TIME_ATTR, ""),
        CONF_SUBTITLE_ATTRIBUTE: user_input.get(CONF_SUBTITLE_ATTRIBUTE, ""),
        CONF_STATE_LABELS: _parse_state_labels(user_input.get(CONF_STATE_LABELS, "")),
        CONF_COMPLETION_MESSAGE: user_input.get(CONF_COMPLETION_MESSAGE, ""),
        CONF_TOTAL_STEPS: user_input.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS),
        CONF_CURRENT_STEP_ATTR: user_input.get(CONF_CURRENT_STEP_ATTR, ""),
        CONF_SEVERITY: user_input.get(CONF_SEVERITY, DEFAULT_SEVERITY),
        CONF_ACCENT_COLOR: _rgb_to_hex(user_input.get(CONF_ACCENT_COLOR)),
        CONF_ACCENT_COLOR_ATTRIBUTE: user_input.get(CONF_ACCENT_COLOR_ATTRIBUTE, ""),
        CONF_URL: url,
        CONF_SECONDARY_URL: secondary_url,
        CONF_ENDED_TTL: int(ended_ttl) if ended_ttl is not None else None,
        CONF_STALE_TTL: int(stale_ttl) if stale_ttl is not None else None,
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
        return {SUBENTRY_TYPE_ENTITY: PushWardEntitySubentryFlow}


class PushWardEntitySubentryFlow(config_entries.ConfigSubentryFlow):
    """Handle adding and reconfiguring tracked entities (two-step flow)."""

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()
        self._step1_input: dict[str, Any] = {}
        self._is_reconfigure: bool = False
        self._details_defaults: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.SubentryFlowResult:
        """Step 1: Entity + template."""
        if user_input is not None:
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
                current[CONF_STATE_LABELS] = ", ".join(f"{k}={v}" for k, v in labels.items())
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
                entity_cfg = _parse_entity_input(merged)
            except vol.Invalid as exc:
                for field in exc.path:
                    errors[field] = "invalid_url"
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


def _parse_csv(value: str) -> list[str]:
    """Parse a comma-separated string into a list of stripped, non-empty items."""
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def _parse_state_labels(value: str) -> dict[str, str]:
    """Parse 'state=Label, state2=Label 2' into a dict."""
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
