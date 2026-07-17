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

import voluptuous as vol
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import SelectSelector

from custom_components.pushward.config_flow import (
    ENTITY_SECTIONS,
    SELECT_TRANSLATION_KEYS,
    WIDGET_SECTIONS,
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
            if isinstance(sel, section):
                scan(sel.schema)  # dropdowns like sound/scale/trigger_mode now live in sections
            elif isinstance(sel, SelectSelector):
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


# ---------------------------------------------------------------------------
# Collapsible-section structural guards
#
# The step-2 detail forms group fields into section() collapsibles. These guards
# tie the schema partition, the frozen pre-section field lists, and the section
# translations together so a field can't silently vanish from a form, land in two
# sections, or lose its (relocated) label.
# ---------------------------------------------------------------------------

# Frozen, hand-maintained per-template field sets (every field the pre-section
# flat form rendered). Recomputed by hand only when a template gains/loses a
# field; a drift here means the section refactor dropped or duplicated one.
ENTITY_FROZEN_FIELDS: dict[str, set[str]] = {
    "generic": {
        "start_states",
        "end_states",
        "progress_entity",
        "progress_attribute",
        "remaining_time_entity",
        "remaining_time_attribute",
        "live_progress",
        "subtitle_entity",
        "subtitle_attribute",
        "slug",
        "activity_name",
        "icon",
        "icon_attribute",
        "priority",
        "sound",
        "update_interval",
        "state_labels",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "background_color_attribute",
        "text_color",
        "text_color_attribute",
        "tap_action_url",
        "tap_action_foreground",
        "ended_ttl",
        "stale_ttl",
        "dismissal_ttl",
    },
    "countdown": {
        "start_states",
        "end_states",
        "remaining_time_entity",
        "remaining_time_attribute",
        "completion_message",
        "warning_threshold",
        "alarm",
        "snooze_seconds",
        "subtitle_entity",
        "subtitle_attribute",
        "slug",
        "activity_name",
        "icon",
        "icon_attribute",
        "priority",
        "sound",
        "update_interval",
        "state_labels",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "background_color_attribute",
        "text_color",
        "text_color_attribute",
        "tap_action_url",
        "tap_action_foreground",
        "ended_ttl",
        "stale_ttl",
        "dismissal_ttl",
    },
    "alert": {
        "start_states",
        "end_states",
        "severity",
        "severity_label",
        "fired_at_entity",
        "fired_at_attribute",
        "subtitle_entity",
        "subtitle_attribute",
        "slug",
        "activity_name",
        "icon",
        "icon_attribute",
        "priority",
        "sound",
        "update_interval",
        "state_labels",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "background_color_attribute",
        "text_color",
        "text_color_attribute",
        "tap_action_url",
        "tap_action_foreground",
        "url",
        "url_foreground",
        "url_title",
        "secondary_url",
        "secondary_url_foreground",
        "secondary_url_title",
        "ended_ttl",
        "stale_ttl",
        "dismissal_ttl",
    },
    "steps": {
        "start_states",
        "end_states",
        "progress_entity",
        "progress_attribute",
        "remaining_time_entity",
        "remaining_time_attribute",
        "live_progress",
        "total_steps",
        "current_step_entity",
        "current_step_attribute",
        "step_labels",
        "step_rows",
        "step_weights",
        "step_colors",
        "subtitle_entity",
        "subtitle_attribute",
        "slug",
        "activity_name",
        "icon",
        "icon_attribute",
        "priority",
        "sound",
        "update_interval",
        "state_labels",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "background_color_attribute",
        "text_color",
        "text_color_attribute",
        "tap_action_url",
        "tap_action_foreground",
        "url",
        "url_foreground",
        "url_title",
        "secondary_url",
        "secondary_url_foreground",
        "secondary_url_title",
        "ended_ttl",
        "stale_ttl",
        "dismissal_ttl",
    },
    "gauge": {
        "start_states",
        "end_states",
        "value_entity",
        "value_attribute",
        "min_value",
        "max_value",
        "unit",
        "subtitle_entity",
        "subtitle_attribute",
        "slug",
        "activity_name",
        "icon",
        "icon_attribute",
        "priority",
        "sound",
        "update_interval",
        "state_labels",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "background_color_attribute",
        "text_color",
        "text_color_attribute",
        "tap_action_url",
        "tap_action_foreground",
        "ended_ttl",
        "stale_ttl",
        "dismissal_ttl",
    },
    "timeline": {
        "start_states",
        "end_states",
        "series",
        "series_entities",
        "units",
        "primary_series",
        "value_entity",
        "value_attribute",
        "unit",
        "scale",
        "decimals",
        "smoothing",
        "thresholds",
        "history_period",
        "subtitle_entity",
        "subtitle_attribute",
        "slug",
        "activity_name",
        "icon",
        "icon_attribute",
        "priority",
        "sound",
        "update_interval",
        "state_labels",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "background_color_attribute",
        "text_color",
        "text_color_attribute",
        "tap_action_url",
        "tap_action_foreground",
        "ended_ttl",
        "stale_ttl",
        "dismissal_ttl",
    },
    "board": {
        "start_states",
        "end_states",
        "tiles",
        "subtitle_entity",
        "subtitle_attribute",
        "slug",
        "activity_name",
        "icon",
        "icon_attribute",
        "priority",
        "sound",
        "update_interval",
        "state_labels",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "background_color_attribute",
        "text_color",
        "text_color_attribute",
        "tap_action_url",
        "tap_action_foreground",
        "ended_ttl",
        "stale_ttl",
        "dismissal_ttl",
    },
    "log": {
        "start_states",
        "end_states",
        "log_columns",
        "log_level_attribute",
        "subtitle_entity",
        "subtitle_attribute",
        "slug",
        "activity_name",
        "icon",
        "icon_attribute",
        "priority",
        "sound",
        "update_interval",
        "state_labels",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "background_color_attribute",
        "text_color",
        "text_color_attribute",
        "tap_action_url",
        "tap_action_foreground",
        "ended_ttl",
        "stale_ttl",
        "dismissal_ttl",
    },
}

WIDGET_FROZEN_FIELDS: dict[str, set[str]] = {
    "value": {
        "widget_name",
        "value_attribute",
        "unit",
        "label",
        "label_attribute",
        "subtitle_attribute",
        "icon",
        "icon_attribute",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "text_color",
        "tap_action_url",
        "tap_action_foreground",
        "widget_trigger_mode",
        "widget_poll_interval",
    },
    "progress": {
        "widget_name",
        "value_attribute",
        "unit",
        "value_scale",
        "label",
        "label_attribute",
        "subtitle_attribute",
        "icon",
        "icon_attribute",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "text_color",
        "tap_action_url",
        "tap_action_foreground",
        "widget_trigger_mode",
        "widget_poll_interval",
    },
    "gauge": {
        "widget_name",
        "value_attribute",
        "unit",
        "min_value",
        "max_value",
        "label",
        "label_attribute",
        "subtitle_attribute",
        "icon",
        "icon_attribute",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "text_color",
        "tap_action_url",
        "tap_action_foreground",
        "widget_trigger_mode",
        "widget_poll_interval",
    },
    "status": {
        "widget_name",
        "severity",
        "label",
        "label_attribute",
        "subtitle_attribute",
        "icon",
        "icon_attribute",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "text_color",
        "tap_action_url",
        "tap_action_foreground",
        "widget_trigger_mode",
        "widget_poll_interval",
    },
    "stat_list": {
        "widget_name",
        "stat_rows",
        "label",
        "label_attribute",
        "subtitle_attribute",
        "icon",
        "icon_attribute",
        "accent_color",
        "accent_color_attribute",
        "background_color",
        "text_color",
        "tap_action_url",
        "tap_action_foreground",
        "widget_trigger_mode",
        "widget_poll_interval",
    },
}


def _partition(schema) -> tuple[set[str], dict[str, list[str]]]:
    """Return (top-level field names, {section_key: [field names]}) for a form schema."""
    top: set[str] = set()
    sections: dict[str, list[str]] = {}
    for key, value in schema.schema.items():
        name = key.schema if isinstance(key, vol.Marker) else key
        if isinstance(value, section):
            sections[name] = [ik.schema if isinstance(ik, vol.Marker) else ik for ik in value.schema.schema]
        else:
            top.add(name)
    return top, sections


def test_entity_sections_cover_frozen_field_set() -> None:
    """Per template, top-level + sectioned fields == the frozen set, with no overlap or dupes."""
    for template in TEMPLATES:
        top, sections = _partition(_details_schema("binary_sensor.washer", template, {}))
        sectioned = [f for fs in sections.values() for f in fs]
        assert len(sectioned) == len(set(sectioned)), f"{template}: a field is in two sections"
        assert not (top & set(sectioned)), f"{template}: field both top-level and sectioned {top & set(sectioned)}"
        assert top | set(sectioned) == ENTITY_FROZEN_FIELDS[template], f"{template}: field set drifted"
        unknown = set(sections) - set(ENTITY_SECTIONS)
        assert not unknown, f"{template}: unknown section {unknown}"


def test_widget_sections_cover_frozen_field_set() -> None:
    """Same coverage/no-overlap guard for the widget detail form."""
    for template in WIDGET_TEMPLATES:
        top, sections = _partition(_widget_details_schema("sensor.foo", template, {}))
        sectioned = [f for fs in sections.values() for f in fs]
        assert len(sectioned) == len(set(sectioned)), f"{template}: a field is in two sections"
        assert not (top & set(sectioned)), f"{template}: field both top-level and sectioned {top & set(sectioned)}"
        assert top | set(sectioned) == WIDGET_FROZEN_FIELDS[template], f"{template}: field set drifted"
        unknown = set(sections) - set(WIDGET_SECTIONS)
        assert not unknown, f"{template}: unknown section {unknown}"


def test_every_entity_section_has_a_name_in_all_locales() -> None:
    """Each entity section key carries a sections.<key>.name in every locale (raw key otherwise)."""
    for locale_file in sorted(_TRANSLATIONS.glob("*.json")):
        data = json.loads(locale_file.read_text())
        sections = data["config_subentries"]["tracked_entity"]["step"]["details"]["sections"]
        for key in ENTITY_SECTIONS:
            assert str(sections.get(key, {}).get("name", "")).strip(), (
                f"{locale_file.name}: entity section {key} has no name"
            )


def test_every_widget_section_has_a_name_in_all_locales() -> None:
    """Each widget section key carries a sections.<key>.name in every locale."""
    for locale_file in sorted(_TRANSLATIONS.glob("*.json")):
        data = json.loads(locale_file.read_text())
        sections = data["config_subentries"]["tracked_widget"]["step"]["details"]["sections"]
        for key in WIDGET_SECTIONS:
            assert str(sections.get(key, {}).get("name", "")).strip(), (
                f"{locale_file.name}: widget section {key} has no name"
            )


def test_data_sources_section_has_a_description_in_all_locales() -> None:
    """The data_sources section gets a one-line description (only section that does)."""
    for locale_file in sorted(_TRANSLATIONS.glob("*.json")):
        data = json.loads(locale_file.read_text())
        sections = data["config_subentries"]["tracked_entity"]["step"]["details"]["sections"]
        assert str(sections["data_sources"].get("description", "")).strip(), (
            f"{locale_file.name}: data_sources section has no description"
        )


def test_entity_sectioned_fields_labelled_under_their_section_in_en() -> None:
    """Every entity section field has its data + data_description entry under that section in en.json."""
    en = json.loads((_TRANSLATIONS / "en.json").read_text())
    sections = en["config_subentries"]["tracked_entity"]["step"]["details"]["sections"]
    for sec, fields in ENTITY_SECTIONS.items():
        data = sections[sec].get("data", {})
        desc = sections[sec].get("data_description", {})
        for field in fields:
            assert field in data, f"en: sections.{sec}.data missing {field}"
            assert field in desc, f"en: sections.{sec}.data_description missing {field}"


def test_widget_sectioned_fields_labelled_under_their_section_in_en() -> None:
    """Every widget section field has its data + data_description entry under that section in en.json."""
    en = json.loads((_TRANSLATIONS / "en.json").read_text())
    sections = en["config_subentries"]["tracked_widget"]["step"]["details"]["sections"]
    for sec, fields in WIDGET_SECTIONS.items():
        data = sections[sec].get("data", {})
        desc = sections[sec].get("data_description", {})
        for field in fields:
            assert field in data, f"en: widget sections.{sec}.data missing {field}"
            assert field in desc, f"en: widget sections.{sec}.data_description missing {field}"


def test_remaining_time_labelled_both_top_level_and_in_data_sources_en() -> None:
    """remaining_time is top-level on countdown and sectioned on generic/steps, so it needs a label in both."""
    en = json.loads((_TRANSLATIONS / "en.json").read_text())
    details = en["config_subentries"]["tracked_entity"]["step"]["details"]
    for field in ("remaining_time_entity", "remaining_time_attribute"):
        assert field in details["data"], f"en: top-level data missing {field}"
        assert field in details["sections"]["data_sources"]["data"], f"en: data_sources.data missing {field}"
