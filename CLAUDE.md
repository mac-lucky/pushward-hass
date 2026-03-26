# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

PushWard for Home Assistant is a custom HACS integration that tracks HA entity state changes and surfaces them as PushWard Live Activities on iPhone (Dynamic Island + Lock Screen). When an entity enters a configured "start" state, a Live Activity is created; when it enters an "end" state, it is dismissed with a two-phase completion animation.

This is a **public repository** — no server internals, private URLs, API keys, or DB schemas should appear in code or commit history.

## Commands

```bash
uv sync                                        # Install dependencies
uv run pytest tests/ -v                         # Run all tests
uv run pytest tests/test_api.py -v -k "test_x"  # Run single test
uv run ruff check . && uv run ruff format .     # Lint + format
```

## Architecture

```
config_flow.py    → ConfigEntry (integration key) + ConfigSubentries (tracked entities)
__init__.py       → Creates API client, starts ActivityManager, registers services
activity_manager  → Listens to HA state changes, decides start/update/end
content_mapper    → Translates HA State + entity config → API content dict
api.py            → HTTP client with retry/backoff to PushWard server
```

## Key Patterns

- **Subentry two-step flow**: `config_flow.py` uses a two-step `ConfigSubentryFlow` — step 1 picks entity + template, step 2 dynamically builds schema via `_details_schema()` based on the selected template (generic/countdown/alert/steps).
- **Two-phase end**: On end state, manager sends ONGOING with completion content (green checkmark), sleeps `END_DELAY_SECONDS` (5s), then sends ENDED. The `generation` counter prevents stale ends if the activity restarts during the sleep.
- **Throttled updates with dedup**: Rate-limited per `update_interval` with content dict equality check. `flush_unsub` timer fires after cooldown.
- **Reauth**: 401/403 triggers `entry.async_start_reauth()` once via `_reauth_triggered` flag.

## Icon Resolution

Resolved in `content_mapper.map_content()` with 6-level fallback (most complex cross-file logic):

1. `icon_attribute` — dynamic from HA entity attribute
2. `CONF_ICON` — static icon from user config (MDI picker)
3. `state.attributes["icon"]` — legacy HA integrations
4. Entity registry icon — looked up in `activity_manager._get_registry_icon()`
5. `DEVICE_CLASS_ICONS` in `const.py` — mirrors HA frontend tables (modern integrations have empty backend icons)
6. `DOMAIN_DEFAULTS` in `const.py` — fallback per HA domain

## Gotchas

- `strings.json` and `translations/en.json` must stay in sync — update both when changing UI text.
- **Release**: tag must match `version` in `manifest.json`. HACS only sees GitHub releases (`hide_default_branch: true`).
- ConfigEntry `VERSION = 2` — migration from v1 (options-based) to v2 (subentries) exists in `__init__.py`.
