# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

HACS custom integration for Home Assistant that tracks HA entities as PushWard Live Activities on iPhone. When an entity enters a "start" state (e.g., washer turns on), a Live Activity appears on the iPhone; when it enters an "end" state, the activity dismisses.

## Architecture

```
custom_components/pushward/
├── __init__.py           # Entry point: setup/teardown, migration
├── manifest.json         # HA integration manifest (version must match release tag)
├── const.py              # Constants and domain defaults
├── config_flow.py        # Config flow (setup/reconfigure) + subentry flow (entity management)
├── api.py                # Async PushWard API client (aiohttp, retry)
├── activity_manager.py   # State listeners, activity lifecycle, throttled updates
├── content_mapper.py     # HA state/attrs → PushWard content translation
├── strings.json          # UI text for config/subentry flows
└── translations/en.json  # English translations (keep in sync with strings.json)
```

## Build Commands

Uses `uv` for dependency management.

```bash
# Setup (install dev dependencies into .venv)
uv sync --frozen

# Lint
ruff check custom_components/ tests/
ruff format --check custom_components/ tests/

# Test (all)
uv run pytest tests/ -v

# Test (single file or test)
uv run pytest tests/test_api.py -v
uv run pytest tests/test_content_mapper.py::test_map_content_basic -v

# Test with coverage (matches CI)
uv run pytest tests/ -v --cov=custom_components/pushward --cov-report=term-missing

# Install in HA (dev)
# Symlink or copy custom_components/pushward/ into HA's custom_components/
```

## Key Design Decisions

- **Config subentries (VERSION 2):** Tracked entities are stored as config subentries (not options). Each entity shows as an individual item on the integration card with add/reconfigure/remove. The `config_flow.py` uses `ConfigSubentryFlow` with `async_step_user` (add) and `async_step_reconfigure` (edit). Subentry unique_id is the entity_id.
- **Reconfigure flow:** Server URL and integration key can be changed via `async_step_reconfigure` on `PushWardConfigFlow` without removing and re-adding the integration.
- **Event-driven updates with throttling:** State changes trigger updates immediately. The `update_interval` setting acts as a rate limiter (not a polling interval) — rapid state changes within the cooldown are coalesced via `async_call_later`.
- **Two-phase end:** When an entity reaches an end state, first send ONGOING with completion content (progress=1.0, green, checkmark), wait 5s, then send ENDED. This matches other PushWard bridges.
- **Domain defaults:** When adding an entity, start/end states and icon are pre-filled based on the HA domain (e.g., `binary_sensor` → on/off, `climate` → heating,cooling/off,idle).
- **Slug format:** `ha-<sanitized-entity-id>` (e.g., `sensor.washer_status` → `ha-washer-status`). The slug field is optional — leave empty to auto-generate.
- **Integration key scope:** Recommended `ha-*` slug pattern with `activity:manage` scope.
- **ColorRGBSelector for accent color:** Stored as hex string (`#rrggbb`) internally, converted to/from `[r, g, b]` list for the HA color picker. Conditional default — only set when a valid color exists.
- **Progress attribute expects 0-100:** The `progress_attribute` reads a 0-100 value from the HA entity attribute and divides by 100 to produce a 0.0-1.0 float for the API.
- **strings.json ↔ translations/en.json:** These must stay in sync. `strings.json` is the source of truth; `translations/en.json` is a copy. When editing UI text, update both files.

## API Endpoints Used

- `GET /auth/me` — validate connection
- `POST /activities` — create activity (409 "already exists" = OK)
- `PATCH /activity/{slug}` — update activity state + content
- `DELETE /activities/{slug}` — delete activity (404 = OK)

Auth: `Authorization: Bearer <integration_key>`

## CI/CD

- HACS validation + hassfest + ruff + pytest in `ci.yml`
- **Auto-release:** On `v*` tag push, after all checks pass, CI creates a GitHub release with auto-generated notes.
- Dependabot auto-merge via shared workflow

## Releases

HACS detects updates by comparing `manifest.json` version against GitHub release tags.

**Creating a release:**

1. Bump version in `manifest.json` (semver: `MAJOR.MINOR.PATCH`)
2. Commit and push to `main`
3. Tag and push: `git tag v0.5.0 && git push --tags`
4. CI creates the GitHub release automatically after checks pass

**Version tag must match `manifest.json` version** (e.g., `"version": "0.3.1"` → tag `v0.3.1`).

## Changelog

| Version | Changes |
|---------|---------|
| 0.4.2 | Preserve last progress/subtitle on activity end instead of forcing 100% |
| 0.4.1 | Version bump |
| 0.4.0 | Fix HACS update detection (`hide_default_branch`), bump min HA to 2025.7.0, add integration icons |
| 0.3.2 | Add integration icons (`icon.png`, `icon@2x.png`) |
| 0.3.1 | Make slug field optional, add reconfigure flow for server URL/key |
| 0.3.0 | Replace options flow with config subentries for entity management |
| 0.2.0 | Event-driven updates with throttling, field descriptions with examples, color picker for accent color, SF Symbols link |
| 0.1.1 | Fix voluptuous_serialize crash (move URL validation out of schema) |
| 0.1.0 | Initial release — config flow, API client, activity manager, content mapper |
