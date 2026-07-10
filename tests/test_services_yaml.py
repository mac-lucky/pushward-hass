"""Structural tests for services.yaml and its translation section keys.

These run without a HomeAssistant fixture — they only parse the shipped
``services.yaml`` and ``translations/*.json`` files. They guard the
``update_activity`` collapsible-section regroup against two regressions:

1. A field silently dropped or duplicated while being moved between sections.
2. A section key in services.yaml drifting out of sync with the 24 translation
   files (each must carry a matching ``sections`` entry, or the HA UI shows a raw
   key as the section header).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from custom_components.pushward.const import TEMPLATES

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "pushward"
_SERVICES_YAML = _COMPONENT / "services.yaml"
_TRANSLATIONS = _COMPONENT / "translations"


def _services() -> dict:
    return yaml.safe_load(_SERVICES_YAML.read_text())


def _sections(service: dict) -> set[str]:
    """Section keys (a field node carrying its own nested ``fields``)."""
    return {key for key, val in (service.get("fields") or {}).items() if isinstance(val, dict) and "fields" in val}


def _leaf_fields(service: dict) -> list[str]:
    """All field keys, flattening collapsible sections into top-level keys."""
    out: list[str] = []
    for key, val in (service.get("fields") or {}).items():
        if isinstance(val, dict) and "fields" in val:
            out.extend((val.get("fields") or {}).keys())
        else:
            out.append(key)
    return out


def test_services_yaml_parses() -> None:
    """services.yaml is valid YAML with no duplicate keys (HA's loader rejects dupes)."""
    assert isinstance(_services(), dict)


def test_no_duplicate_field_keys() -> None:
    """Moving fields into sections must never duplicate a field key within a service.

    Submitted service data stays flat (sections are UI-only), so two fields with the
    same key — one at the root, one in a section — would collide silently.
    """
    for name, body in _services().items():
        leaves = _leaf_fields(body)
        assert len(leaves) == len(set(leaves)), f"{name}: duplicate field key(s) {sorted(leaves)}"


def test_sections_are_well_formed() -> None:
    """Each collapsible section carries ``collapsed`` + a non-empty ``fields`` map."""
    for name, body in _services().items():
        for sec in _sections(body):
            node = body["fields"][sec]
            assert node.get("collapsed") is True, f"{name}.{sec}: missing 'collapsed: true'"
            assert node.get("fields"), f"{name}.{sec}: empty 'fields'"


def test_per_template_update_services_exist_without_template_field() -> None:
    """Each update_activity_<template> exists, requires slug+state, and never exposes a template field.

    The template is implied by the service name and injected by the handler — exposing a
    template field would let the user contradict the service they called.
    """
    services = _services()
    for template in TEMPLATES:
        name = f"update_activity_{template}"
        assert name in services, f"{name} missing from services.yaml"
        leaves = _leaf_fields(services[name])
        assert "template" not in leaves, f"{name}: must not expose a 'template' field"
        assert {"slug", "state"} <= set(leaves), f"{name}: missing slug/state"


def test_deprecated_update_activity_still_registered() -> None:
    """The legacy update_activity stays as a backward-compatible alias."""
    assert "update_activity" in _services()


# Matches the stale dict-keyed wording ("1=Label", "1 = Label") the bug fix removed.
_DICT_STEP_LABEL_RE = re.compile(r"\b1\s*=")


def test_step_labels_documented_as_ordered_list_everywhere() -> None:
    """step_labels is an ordered list — guard against the old dict-keyed ("1=Label") wording.

    The server is ``StepLabels []string``; the schema validates a list. Stale locales used to
    describe a dict keyed by step number, which both misled users and failed validation. The
    English source is asserted positively; every locale is guarded against the dict marker.
    """
    svc_yaml = _SERVICES_YAML.read_text()
    assert "keyed by step number" not in svc_yaml, "services.yaml still documents step_labels as a dict"

    en = json.loads((_TRANSLATIONS / "en.json").read_text())
    en_desc = en["services"]["update_activity"]["fields"]["step_labels"]["description"].lower()
    assert "list" in en_desc, f"en.json step_labels no longer describes an ordered list: {en_desc!r}"

    for locale_file in sorted(_TRANSLATIONS.glob("*.json")):
        data = json.loads(locale_file.read_text())
        for svc_name, svc in data.get("services", {}).items():
            desc = svc.get("fields", {}).get("step_labels", {}).get("description", "")
            assert not _DICT_STEP_LABEL_RE.search(desc), (
                f"{locale_file.name}:{svc_name} step_labels uses stale dict wording: {desc!r}"
            )


def test_sections_have_matching_translations_in_every_locale() -> None:
    """Every services.yaml section key must have a matching entry in all locale files.

    The update_activity regroup added a ``sections`` block to 24 translation files;
    a drift here means the HA UI renders the raw key as a section header.
    """
    services = _services()
    expected = {name: _sections(body) for name, body in services.items() if _sections(body)}
    assert expected, "expected at least one service with sections"

    for locale_file in sorted(_TRANSLATIONS.glob("*.json")):
        data = json.loads(locale_file.read_text())
        svc_block = data.get("services", {})
        for name, section_keys in expected.items():
            locale_sections = set(svc_block.get(name, {}).get("sections", {}).keys())
            assert locale_sections == section_keys, (
                f"{locale_file.name}: {name} sections {sorted(locale_sections)} != services.yaml {sorted(section_keys)}"
            )


def _flatten_keys(node: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, val in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(val, dict):
            keys |= _flatten_keys(val, path)
        else:
            keys.add(path)
    return keys


def test_every_locale_carries_every_en_key() -> None:
    """Every locale must carry exactly the en.json key set.

    HA silently falls back to English for missing custom-integration keys, so a
    locale drifting behind en.json ships untranslated UI without any CI signal.
    Extra keys are stale leftovers from removed features and are equally wrong.
    """
    en_keys = _flatten_keys(json.loads((_TRANSLATIONS / "en.json").read_text()))
    for locale_file in sorted(_TRANSLATIONS.glob("*.json")):
        if locale_file.name == "en.json":
            continue
        keys = _flatten_keys(json.loads(locale_file.read_text()))
        missing = sorted(en_keys - keys)
        extra = sorted(keys - en_keys)
        assert not missing, f"{locale_file.name}: missing {missing[:8]} (+{max(0, len(missing) - 8)} more)"
        assert not extra, f"{locale_file.name}: stale keys {extra[:8]} (+{max(0, len(extra) - 8)} more)"
