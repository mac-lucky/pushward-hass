# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

PushWard for Home Assistant is a custom HACS integration that tracks HA entity state changes and surfaces them on iPhone two ways:

1. **Live Activities** (Dynamic Island + Lock Screen) — when an entity enters a configured "start" state a Live Activity is created; on an "end" state it is dismissed with a two-phase completion animation. Driven by `activity_manager` + `content_mapper`.
2. **Home Screen / Lock Screen widgets** — an entity (or several, for `stat_list`) is bound to a server-side widget that re-renders on state change or on a poll interval. Driven by `widget_manager` + `widget_mapper`.

The two surfaces are independent: each is a separate `ConfigSubentry` type (`tracked_entity` vs `tracked_widget`), has its own manager, mapper, `.storage` cache, and config-flow class. They share the API client and the icon/color helpers in `content_mapper`.

Requires Python 3.13.2+ and Home Assistant 2025.7.0+.

This is a **public repository** — no server internals, private URLs, API keys, or DB schemas should appear in code or commit history.

## Cross-Repository Dependencies

- **pushward-server**: This integration calls server's REST API for activity CRUD (create/update/end) and widget CRUD (create/PATCH) → server sends APNs → pushward-ios shows Live Activities / renders widgets
- API contract (endpoints, auth with integration keys `hlk_` prefix) is defined by pushward-server. Widget CRUD requires the `widgets` permission on the key; widget content field caps in `const.py` mirror `pushward-server/internal/model/widget.go`

## Commands

```bash
uv sync                                        # Install dependencies
uv run pytest tests/ -v                         # Run all tests
uv run pytest tests/test_api.py -v -k "test_x"  # Run single test
uv run ruff check . && uv run ruff format .     # Lint + format
```

## Architecture

```
config_flow.py    → ConfigEntry (integration key) + two ConfigSubentry flows:
                      PushWardEntitySubentryFlow (activities), PushWardWidgetSubentryFlow (widgets)
__init__.py       → Creates API client, starts ActivityManager + WidgetManager, registers 7 services
activity_manager  → Listens to HA state changes, decides activity start/update/end
content_mapper    → Translates HA State + entity config → activity content dict
                      (also exports shared helpers: resolve_icon, resolve_color, color_to_str,
                       add_tap_action, lookup_registry_icon — reused by widget_mapper)
widget_manager    → Listens to HA state changes / poll timer, diffs content, PATCHes widget
widget_mapper     → Translates HA State + widget config → widget content dict
api.py            → HTTP client with retry/backoff to PushWard server (activities + widgets)
```

## Key Patterns

- **Subentry two-step flow**: `config_flow.py` uses a two-step `ConfigSubentryFlow` — step 1 picks entity + template, step 2 dynamically builds schema via `_details_schema()` based on the selected template. Templates: `generic`, `countdown`, `alert`, `steps`, `gauge`, `timeline`. Each adds template-specific fields (e.g. `gauge` → min/max/unit, `timeline` → series/scale/decimals/thresholds/history_period).
- **Two-phase end**: On end state, manager sends ONGOING with completion content (green checkmark), sleeps `END_DELAY_SECONDS` (5s), then sends ENDED. The `generation` counter prevents stale ends if the activity restarts during the sleep.
- **Throttled updates with dedup**: Rate-limited per `update_interval` with content dict equality check. `flush_unsub` timer fires after cooldown.
- **Reauth**: 401/403 triggers `entry.async_start_reauth()` once via `_reauth_triggered` flag.
- **Timeline history buffer**: `TrackedEntity.history_buffer` is an in-memory ring buffer (≤300 samples) populated from live state changes and persisted to `.storage/pushward.history.<entry_id>`. Required because HA 2024.8+ strips most attributes from the recorder DB — for attribute-based entities (light brightness, climate temps), the recorder cannot be used to backfill the sparkline. For numeric-state sensors, the recorder is still used as a fallback.
- **Services**: 7 services registered in `__init__.py`: `create_activity`, `update_activity`, `end_activity`, `delete_activity`, `send_notification`, `send_email`, `widget_refresh`. Schemas in `services.yaml`. `widget_refresh` targets by `slug` xor `entity_id` (mutually exclusive) and fans out to every entry's `WidgetManager`. `send_email` POSTs `/emails` (the service field `body` maps to the API `text_body`); it requires the key's `emails` capability and a verified recipient (registered/confirmed in the iOS app — the integration can't verify recipients itself), surfacing `PushWardEmailPermissionError` on 403.

### Widget-specific patterns

- **5 widget templates** (`const.py`, mirror `pushward-server/internal/model/widget.go`): `value`, `progress`, `gauge`, `status`, `stat_list`. `value`/`progress`/`gauge` need a coercible numeric value (return `None` → request skipped); `status` can render with static label/icon only; `stat_list` binds 1–`WIDGET_MAX_STAT_ROWS` (6) rows, each to a *separate* entity.
- **Two trigger modes** (`widget_trigger_mode`): `event` (default) subscribes to `async_track_state_change_event` for all bound entities; `poll` runs `async_track_time_interval` (`widget_poll_interval`, clamped ≥10s). In poll mode the server `push_throttle` is coupled to the poll interval (`_compute_push_throttle`).
- **Diff cache + deferred create**: `WidgetManager` keeps `TrackedWidget.last_content` and skips a PATCH when the freshly rendered content equals it (unless forced via `widget_refresh`). On setup it POSTs each widget once (idempotent upsert) so the server matches HA after every restart; if the entity isn't yet renderable, the create is *deferred* until the first valid state arrives. Cache persists to `.storage/pushward.widgets.<entry_id>`.
- **Burst coalescing**: `_schedule_update` runs at most one in-flight update task per widget; rapid state changes during a send are dropped, not queued.
- **Trend auto-derivation**: `value`/`gauge` templates compute `trend` (up/down/flat) from the delta vs the previously sent numeric value — no config needed.
- **Widget permission gating**: the integration key needs the server-side `widgets` permission. A 403 surfaces as `PushWardWidgetPermissionError` → single de-duped persistent notification (cleared on next success); generic 403s get a per-slug notification. 401/403-auth triggers reauth once.

## Icon Resolution

Resolved in `content_mapper.map_content()` with 6-level fallback (most complex cross-file logic):

1. `icon_attribute` — dynamic from HA entity attribute
2. `CONF_ICON` — static icon from user config (MDI picker)
3. `state.attributes["icon"]` — legacy HA integrations
4. Entity registry icon — looked up in `activity_manager._get_registry_icon()`
5. `DEVICE_CLASS_ICONS` in `const.py` — mirrors HA frontend tables (modern integrations have empty backend icons)
6. `DOMAIN_DEFAULTS` in `const.py` — fallback per HA domain

`widget_mapper` reuses the same `resolve_icon`/`resolve_color` helpers, so widget icon/color resolution follows the same fallback chain — except `stat_list` widgets have no anchoring entity, so registry-icon lookup is skipped and only the static config icon applies.

## Testing

Tests use `pytest-homeassistant-custom-component` (real `HomeAssistant` fixture). When writing tests, reuse the helpers in `tests/conftest.py`:

- `make_entity_config(**overrides)` — builds a tracked-**entity** (activity) config dict with all `CONF_*` fields defaulted. Override only what the test cares about.
- `make_widget_config(**overrides)` — same idea for tracked-**widget** config dicts.
- `make_mock_state(state, attributes, entity_id)` — builds a mock HA `State`.
- `make_mock_response` / `make_mock_session` / `make_api_client` — wire a fake aiohttp session into a `PushWardApiClient` for API-layer tests.

Adding a new `CONF_*` constant means updating `make_entity_config` (and `make_widget_config` if it's a widget field) in `conftest.py` so existing tests don't break. Test files are split per module: `test_activity_manager`, `test_content_mapper`, `test_widget_manager`, `test_widget_mapper`, `test_widget_api`, `test_api`, `test_config_flow`, `test_services`, `test_icon_resolution`.

## Gotchas

- UI text lives in `translations/<lang>.json` only. HA custom integrations do **not** use `strings.json` (that's a HA Core build artifact) — English goes directly in `translations/en.json`. Adding a new locale is zero-code: drop a new `translations/<tag>.json` file.
- **Release**: tag must match `version` in `manifest.json`. HACS only sees GitHub releases (`hide_default_branch: true`).
- ConfigEntry `VERSION = 2` — migration from v1 (options-based) to v2 (subentries) exists in `__init__.py`.
