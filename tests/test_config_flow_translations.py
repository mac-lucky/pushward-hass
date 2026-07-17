"""Structural tests for the SelectSelector translation_key registry.

These run without a HomeAssistant fixture. They tie three things together:

1. Every dropdown that carries a ``translation_key`` is listed in
   ``SELECT_TRANSLATION_KEYS`` (so a new translated dropdown can't drift out of
   the registry silently), and
2. every registry entry maps to a ``selector.<key>.options`` block that carries a
   label for every wire value, in every locale (so the HA UI never shows a raw
   value because a translation is missing).

Without this the frontend falls back to the bare option value ("stat_list",
"logarithmic", ...) and the user sees the wire form instead of a word.
"""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.helpers.selector import SelectSelector

from custom_components.pushward.config_flow import (
    SELECT_TRANSLATION_KEYS,
    _details_schema,
    _entity_template_schema,
    _widget_details_schema,
    _widget_step1_schema,
)
from custom_components.pushward.const import TEMPLATES, WIDGET_TEMPLATES

_TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "pushward" / "translations"


def _translation_keys_in_schemas() -> dict[str, SelectSelector]:
    """Every SelectSelector across both subentry flows that declares a translation_key.

    Returns translation_key -> the selector, so a later assert can diff the option
    set too. Templates are enumerated so template-specific dropdowns are covered.
    """
    found: dict[str, SelectSelector] = {}

    def scan(schema) -> None:
        for sel in schema.schema.values():
            if isinstance(sel, SelectSelector):
                key = sel.config.get("translation_key")
                if key:
                    found[key] = sel

    scan(_entity_template_schema())
    for template in TEMPLATES:
        scan(_details_schema("sensor.foo", template, {}))
    scan(_widget_step1_schema())
    for template in WIDGET_TEMPLATES:
        scan(_widget_details_schema("sensor.foo", template, {}))
    return found


def test_every_schema_translation_key_is_registered() -> None:
    """A dropdown declaring translation_key must be in SELECT_TRANSLATION_KEYS.

    Miss this and the option labels have nowhere to live -> the UI shows raw values.
    """
    found = _translation_keys_in_schemas()
    unregistered = sorted(set(found) - set(SELECT_TRANSLATION_KEYS))
    assert not unregistered, f"translation_key(s) not in SELECT_TRANSLATION_KEYS: {unregistered}"


def test_registry_keys_are_all_used() -> None:
    """No stale registry entries: every registered key is on a real dropdown."""
    found = _translation_keys_in_schemas()
    unused = sorted(set(SELECT_TRANSLATION_KEYS) - set(found))
    assert not unused, f"SELECT_TRANSLATION_KEYS entries with no dropdown: {unused}"


def test_registry_option_values_match_the_selector() -> None:
    """The wire values in the registry match the options the selector actually renders."""
    found = _translation_keys_in_schemas()
    for key, values in SELECT_TRANSLATION_KEYS.items():
        sel = found[key]
        rendered = tuple(sel.config.get("options", []))
        assert rendered == values, f"{key}: registry {values} != selector options {rendered}"


def test_every_locale_has_complete_selector_options() -> None:
    """Every locale carries a label for every wire value of every registered dropdown."""
    for locale_file in sorted(_TRANSLATIONS.glob("*.json")):
        data = json.loads(locale_file.read_text())
        selector = data.get("selector", {})
        for key, values in SELECT_TRANSLATION_KEYS.items():
            options = selector.get(key, {}).get("options", {})
            missing = [v for v in values if v not in options]
            assert not missing, f"{locale_file.name}: selector.{key}.options missing {missing}"
            # Guard against blank labels: a "" label renders as the raw value.
            blank = [v for v in values if not str(options.get(v, "")).strip()]
            assert not blank, f"{locale_file.name}: selector.{key}.options blank for {blank}"
