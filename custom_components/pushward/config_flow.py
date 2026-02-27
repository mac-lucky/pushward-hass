"""Config flow and subentry flow for PushWard integration."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    ColorRGBSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import PushWardApiClient, PushWardAuthError
from .const import (
    CONF_ACCENT_COLOR,
    CONF_ACTIVITY_NAME,
    CONF_END_STATES,
    CONF_ENTITY_ID,
    CONF_ICON,
    CONF_INTEGRATION_KEY,
    CONF_PRIORITY,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_REMAINING_TIME_ATTR,
    CONF_SERVER_URL,
    CONF_SLUG,
    CONF_START_STATES,
    CONF_TEMPLATE,
    CONF_UPDATE_INTERVAL,
    DEFAULT_PRIORITY,
    DEFAULT_SERVER_URL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    SUBENTRY_TYPE_ENTITY,
    TEMPLATES,
)
from .content_mapper import get_domain_defaults, sanitize_slug

_LOGGER = logging.getLogger(__name__)


def _validate_url(value: str) -> str:
    """Validate URL uses http or https scheme."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise vol.Invalid("URL must use http:// or https:// scheme")
    if not parsed.netloc:
        raise vol.Invalid("URL must include a host")
    return value


def _entity_schema(defaults: dict | None = None) -> vol.Schema:
    """Build the entity config schema with optional defaults."""
    d = defaults or {}

    # ColorRGBSelector requires a valid [r,g,b] default — omit if no color saved
    accent_rgb = _hex_to_rgb(d.get(CONF_ACCENT_COLOR, ""))
    accent_key = (
        vol.Optional(CONF_ACCENT_COLOR, default=accent_rgb)
        if accent_rgb is not None
        else vol.Optional(CONF_ACCENT_COLOR)
    )

    return vol.Schema(
        {
            vol.Required(
                CONF_ENTITY_ID,
                default=d.get(CONF_ENTITY_ID, ""),
            ): EntitySelector(EntitySelectorConfig()),
            vol.Optional(CONF_SLUG, default=d.get(CONF_SLUG, "")): str,
            vol.Optional(
                CONF_ACTIVITY_NAME,
                default=d.get(CONF_ACTIVITY_NAME, ""),
            ): str,
            vol.Optional(CONF_ICON, default=d.get(CONF_ICON, "")): str,
            vol.Optional(
                CONF_PRIORITY,
                default=d.get(CONF_PRIORITY, DEFAULT_PRIORITY),
            ): vol.All(int, vol.Range(min=0, max=10)),
            vol.Optional(
                CONF_TEMPLATE,
                default=d.get(CONF_TEMPLATE, "generic"),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=TEMPLATES,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_START_STATES,
                default=d.get(CONF_START_STATES, ""),
            ): str,
            vol.Optional(
                CONF_END_STATES,
                default=d.get(CONF_END_STATES, ""),
            ): str,
            vol.Optional(
                CONF_UPDATE_INTERVAL,
                default=d.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
            ): vol.All(int, vol.Range(min=1)),
            vol.Optional(
                CONF_PROGRESS_ATTRIBUTE,
                default=d.get(CONF_PROGRESS_ATTRIBUTE, ""),
            ): str,
            vol.Optional(
                CONF_REMAINING_TIME_ATTR,
                default=d.get(CONF_REMAINING_TIME_ATTR, ""),
            ): str,
            accent_key: ColorRGBSelector(),
        }
    )


def _parse_entity_input(user_input: dict) -> dict:
    """Normalize user input into an entity config dict."""
    entity_id = user_input[CONF_ENTITY_ID]
    raw_slug = user_input.get(CONF_SLUG, "").strip()
    if raw_slug:
        slug = re.sub(r"[^a-z0-9-]", "", raw_slug.lower())
        slug = re.sub(r"-+", "-", slug).strip("-")
    if not raw_slug or not slug:
        slug = sanitize_slug(entity_id)

    domain = entity_id.split(".")[0] if "." in entity_id else ""
    defaults = get_domain_defaults(domain)

    start_raw = user_input.get(CONF_START_STATES, "")
    end_raw = user_input.get(CONF_END_STATES, "")

    return {
        CONF_ENTITY_ID: entity_id,
        CONF_SLUG: slug,
        CONF_ACTIVITY_NAME: user_input.get(CONF_ACTIVITY_NAME, "") or entity_id,
        CONF_ICON: user_input.get(CONF_ICON, "") or defaults.get("icon", "questionmark.circle"),
        CONF_PRIORITY: user_input.get(CONF_PRIORITY, DEFAULT_PRIORITY),
        CONF_TEMPLATE: user_input.get(CONF_TEMPLATE, "generic"),
        CONF_START_STATES: _parse_csv(start_raw) or defaults.get("start_states", []),
        CONF_END_STATES: _parse_csv(end_raw) or defaults.get("end_states", []),
        CONF_UPDATE_INTERVAL: user_input.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        CONF_PROGRESS_ATTRIBUTE: user_input.get(CONF_PROGRESS_ATTRIBUTE, ""),
        CONF_REMAINING_TIME_ATTR: user_input.get(CONF_REMAINING_TIME_ATTR, ""),
        CONF_ACCENT_COLOR: _rgb_to_hex(user_input.get(CONF_ACCENT_COLOR)),
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
            try:
                _validate_url(user_input[CONF_SERVER_URL])
            except vol.Invalid:
                errors[CONF_SERVER_URL] = "invalid_url"
            else:
                session = async_get_clientsession(self.hass)
                client = PushWardApiClient(
                    session,
                    user_input[CONF_SERVER_URL],
                    user_input[CONF_INTEGRATION_KEY],
                )
                try:
                    await client.validate_connection()
                except PushWardAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Unexpected error during PushWard setup")
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title="PushWard",
                        data={
                            CONF_SERVER_URL: user_input[CONF_SERVER_URL],
                            CONF_INTEGRATION_KEY: user_input[CONF_INTEGRATION_KEY],
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERVER_URL, default=DEFAULT_SERVER_URL): str,
                    vol.Required(CONF_INTEGRATION_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle reconfiguration of server URL and integration key."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                _validate_url(user_input[CONF_SERVER_URL])
            except vol.Invalid:
                errors[CONF_SERVER_URL] = "invalid_url"
            else:
                session = async_get_clientsession(self.hass)
                client = PushWardApiClient(
                    session,
                    user_input[CONF_SERVER_URL],
                    user_input[CONF_INTEGRATION_KEY],
                )
                try:
                    await client.validate_connection()
                except PushWardAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Unexpected error during PushWard reconfigure")
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data={
                            CONF_SERVER_URL: user_input[CONF_SERVER_URL],
                            CONF_INTEGRATION_KEY: user_input[CONF_INTEGRATION_KEY],
                        },
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERVER_URL, default=entry.data.get(CONF_SERVER_URL, DEFAULT_SERVER_URL)): str,
                    vol.Required(CONF_INTEGRATION_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
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
    """Handle adding and reconfiguring tracked entities."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.SubentryFlowResult:
        """Add a new tracked entity."""
        if user_input is not None:
            entity_cfg = _parse_entity_input(user_input)
            return self.async_create_entry(
                title=entity_cfg[CONF_ACTIVITY_NAME],
                data=entity_cfg,
                unique_id=entity_cfg[CONF_ENTITY_ID],
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_entity_schema(),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Reconfigure an existing tracked entity."""
        subentry = self._get_reconfigure_subentry()
        entry = self._get_entry()

        if user_input is not None:
            entity_cfg = _parse_entity_input(user_input)
            return self.async_update_and_abort(
                entry,
                subentry,
                data=entity_cfg,
                title=entity_cfg[CONF_ACTIVITY_NAME],
            )

        # Pre-fill form with current values, convert lists back to CSV for editing
        current = dict(subentry.data)
        if isinstance(current.get(CONF_START_STATES), list):
            current[CONF_START_STATES] = ", ".join(current[CONF_START_STATES])
        if isinstance(current.get(CONF_END_STATES), list):
            current[CONF_END_STATES] = ", ".join(current[CONF_END_STATES])

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_entity_schema(defaults=current),
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
