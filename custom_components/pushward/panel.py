"""The PushWard sidebar panel: its static files and the admin-only panel entry."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_integration

from .const import DOMAIN

PANEL_URL_PATH = "pushward"
STATIC_URL_PATH = "/pushward_static"
_FRONTEND_DIR = Path(__file__).parent / "frontend"


async def async_register_static_path(hass: HomeAssistant) -> None:
    """Serve frontend/. Called once, at component setup."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL_PATH, str(_FRONTEND_DIR), cache_headers=False)]
    )


async def async_register_panel(hass: HomeAssistant) -> None:
    """Add the sidebar panel unless it is there; the module URL carries the version against stale caches."""
    integration = await async_get_integration(hass, DOMAIN)
    # Checked after the await: registering a panel that exists raises, and
    # panel_custom adds it without yielding, so nothing can slip in between.
    if PANEL_URL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="pushward-panel",
        module_url=f"{STATIC_URL_PATH}/pushward-panel.js?v={integration.version}",
        sidebar_title="PushWard",
        sidebar_icon="mdi:bell-ring",
        require_admin=True,
    )


@callback
def async_remove_panel(hass: HomeAssistant) -> None:
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
