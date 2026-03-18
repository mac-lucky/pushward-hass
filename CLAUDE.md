# CLAUDE.md

## Overview

PushWard for Home Assistant is a custom HACS integration that tracks HA entity state changes and surfaces them as PushWard Live Activities on iPhone (Dynamic Island + Lock Screen). When an entity enters a configured "start" state, a Live Activity is created; when it enters an "end" state, it is dismissed with a two-phase completion animation.

This is a **public repository** — no server internals, private URLs, API keys, or DB schemas should appear in code or commit history.

## File Structure

```
custom_components/pushward/
├── __init__.py          # Entry setup/teardown, service registration, migration
├── config_flow.py       # Config flow (setup/reconfigure/reauth) + subentry flow (entity add/edit)
├── activity_manager.py  # Core state machine: listens to HA events, manages activity lifecycle
├── content_mapper.py    # Maps HA state/attributes → PushWard API content dict
├── api.py               # HTTP client for PushWard REST API (create/update/delete/validate)
├── const.py             # Constants, config keys, domain defaults
├── manifest.json        # HA integration manifest (version, domain, requirements)
├── strings.json         # UI strings for config flow steps and errors
├── services.yaml        # Service definitions (update/create/end/delete activity)
├── translations/
│   └── en.json          # Localized UI strings (mirrors strings.json)
├── brand/
│   ├── icon.png         # Brand icon for HA integrations list
│   └── icon@2x.png      # Retina brand icon
├── icon.png             # Integration icon
└── icon@2x.png          # Retina integration icon
```

## Internal Data Flow

```
config_flow.py          → Creates ConfigEntry (integration key) + ConfigSubentries (tracked entities)
        ↓
__init__.py             → Reads entry data, creates API client, starts ActivityManager
        ↓
activity_manager.py     → Listens to HA state changes, decides start/update/end
        ↓
content_mapper.py       → Translates HA State + entity config → content dict
        ↓
api.py                  → POST/PUT/DELETE to PushWard server
```

## Build & Test

```bash
# Run tests
uv run pytest tests/ -v

# Lint
uv run ruff check .

# Format check
uv run ruff format --check .
```

No build step — Python source is used directly by Home Assistant.

## Key Patterns

- **Subentries**: Each tracked entity is a ConfigSubentry (type `tracked_entity`), not options-flow data. This enables per-entity add/edit/delete in the HA UI.
- **Two-phase end**: When an entity enters an end state, the manager first sends an ONGOING update with completion content (e.g., "Complete" + green), waits `END_DELAY_SECONDS`, then sends ENDED. This gives the user a visible completion state before dismissal.
- **Throttled updates**: Updates are rate-limited per `update_interval` with content deduplication — identical content is not re-sent.
- **Generation counter**: Each `TrackedEntity` has a monotonically increasing `generation`. The two-phase end captures the generation before sleeping; if it changed (activity restarted), the ENDED phase is skipped.
- **Reauth**: If the API returns 401/403 (`PushWardAuthError`), the manager triggers `entry.async_start_reauth()` exactly once, halting further API calls until re-authenticated.
- **Domain defaults**: `DOMAIN_DEFAULTS` in `const.py` provides sensible start/end states and icons per HA domain (binary_sensor, switch, climate, etc.).

## Changelog

| Version | Changes |
|---------|---------|
| 0.13.0  | Replace plain text icon field with HA built-in MDI icon picker (`IconSelector`) for better UX |
| 0.12.0  | Fix attribute selectors not clearable during reconfigure; add icon resolution integration tests |
| 0.11.0  | Add device class → MDI icon mapping table; all default icons now use MDI for reliable iOS rendering |
| 0.10.0  | Fix icon resolution: add entity registry lookup and domain default fallback when no icon is configured |
| 0.9.0   | Code quality cleanup: extract duplicated validation/schema/URL helpers, replace wrapper closures with `functools.partial`, remove unused constant, add shared test helpers, fix stale README and reauth string |
| 0.8.0   | Hardcode API domain, remove server URL from config flow |
| 0.7.0   | Auto-detect entity native icon with MDI support |
| 0.6.0   | Guard `_async_entry_updated` against missing entry data |
| 0.5.0   | Sanitize public repo for open-source release |
| 0.4.2   | Accent color attribute support (RGB/XY/HS/Kelvin conversion) |
| 0.4.1   | Icon attribute support for dynamic icons |
| 0.4.0   | URL deep links, completion message, subtitle attribute, state labels |
| 0.3.0   | Pipeline and alert templates, TTL support |
| 0.2.0   | Subentry-based entity config (migration from v1 options) |
| 0.1.0   | Initial release: binary start/end, generic template, two-phase end |
