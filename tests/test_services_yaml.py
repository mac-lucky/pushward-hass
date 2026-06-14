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
from pathlib import Path

import yaml

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
