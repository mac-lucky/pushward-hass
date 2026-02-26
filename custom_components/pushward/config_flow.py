"""Config flow and options flow for PushWard integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import PushWardApiClient, PushWardAuthError
from .const import (
    CONF_ACCENT_COLOR,
    CONF_ACTIVITY_NAME,
    CONF_END_STATES,
    CONF_ENTITIES,
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
    TEMPLATES,
)
from .content_mapper import get_domain_defaults, sanitize_slug

_LOGGER = logging.getLogger(__name__)


class PushWardConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial PushWard configuration."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the initial setup step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}

        if user_input is not None:
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
                    options={CONF_ENTITIES: []},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERVER_URL, default=DEFAULT_SERVER_URL): str,
                    vol.Required(CONF_INTEGRATION_KEY): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> PushWardOptionsFlow:
        """Get the options flow handler."""
        return PushWardOptionsFlow(config_entry)


class PushWardOptionsFlow(config_entries.OptionsFlow):
    """Handle PushWard entity management options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._entities: list[dict] = list(config_entry.options.get(CONF_ENTITIES, []))
        self._edit_index: int | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_entity", "edit_entity", "remove_entity"],
        )

    async def async_step_add_entity(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Add an entity to track."""
        if user_input is not None:
            entity_cfg = self._parse_entity_input(user_input)
            self._entities.append(entity_cfg)
            return self.async_create_entry(title="", data={CONF_ENTITIES: self._entities})

        return self.async_show_form(
            step_id="add_entity",
            data_schema=self._entity_schema(),
        )

    async def async_step_edit_entity(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Select an entity to edit."""
        if not self._entities:
            return self.async_abort(reason="no_entities")

        if user_input is not None:
            selected = user_input[CONF_ENTITY_ID]
            for i, ent in enumerate(self._entities):
                if ent[CONF_ENTITY_ID] == selected:
                    self._edit_index = i
                    return await self.async_step_edit_entity_form()
            return self.async_abort(reason="entity_not_found")

        entity_ids = [e[CONF_ENTITY_ID] for e in self._entities]
        return self.async_show_form(
            step_id="edit_entity",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ENTITY_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=entity_ids,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_edit_entity_form(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit a tracked entity's configuration."""
        if user_input is not None and self._edit_index is not None:
            entity_cfg = self._parse_entity_input(user_input)
            entity_cfg[CONF_ENTITY_ID] = self._entities[self._edit_index][CONF_ENTITY_ID]
            self._entities[self._edit_index] = entity_cfg
            return self.async_create_entry(title="", data={CONF_ENTITIES: self._entities})

        current = self._entities[self._edit_index]  # type: ignore[index]
        return self.async_show_form(
            step_id="edit_entity_form",
            data_schema=self._entity_schema(defaults=current),
        )

    async def async_step_remove_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Remove tracked entities."""
        if not self._entities:
            return self.async_abort(reason="no_entities")

        if user_input is not None:
            to_remove = set(user_input.get(CONF_ENTITY_ID, []))
            self._entities = [e for e in self._entities if e[CONF_ENTITY_ID] not in to_remove]
            return self.async_create_entry(title="", data={CONF_ENTITIES: self._entities})

        entity_ids = [e[CONF_ENTITY_ID] for e in self._entities]
        return self.async_show_form(
            step_id="remove_entity",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ENTITY_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=entity_ids,
                            mode=SelectSelectorMode.LIST,
                            multiple=True,
                        )
                    ),
                }
            ),
        )

    def _entity_schema(self, defaults: dict | None = None) -> vol.Schema:
        """Build the entity config schema with optional defaults."""
        d = defaults or {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_ENTITY_ID,
                    default=d.get(CONF_ENTITY_ID, ""),
                ): EntitySelector(EntitySelectorConfig()),
                vol.Required(CONF_SLUG, default=d.get(CONF_SLUG, "")): str,
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
                vol.Optional(
                    CONF_ACCENT_COLOR,
                    default=d.get(CONF_ACCENT_COLOR, ""),
                ): str,
            }
        )

    @staticmethod
    def _parse_entity_input(user_input: dict) -> dict:
        """Normalize user input into an entity config dict."""
        entity_id = user_input[CONF_ENTITY_ID]
        slug = user_input.get(CONF_SLUG, "").strip()
        if not slug:
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
            CONF_ACCENT_COLOR: user_input.get(CONF_ACCENT_COLOR, ""),
        }


def _parse_csv(value: str) -> list[str]:
    """Parse a comma-separated string into a list of stripped, non-empty items."""
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]
