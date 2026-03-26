# PushWard for Home Assistant

[![CI](https://github.com/mac-lucky/pushward-hass/actions/workflows/ci.yml/badge.svg)](https://github.com/mac-lucky/pushward-hass/actions/workflows/ci.yml)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Custom [HACS](https://hacs.xyz) integration that tracks Home Assistant entities as [PushWard](https://pushward.app) Live Activities on iPhone (Dynamic Island + Lock Screen).

When an entity enters a configured "start" state (e.g., washer turns on), a Live Activity appears on your iPhone. When it enters an "end" state, the activity dismisses with a two-phase completion animation.

## Features

- **Track any HA entity** as a PushWard Live Activity
- **4 templates** — generic, countdown, alert, steps
- **14 domain defaults** — binary_sensor, switch, climate, vacuum, media_player, lock, cover, timer, sensor, light, fan, weather, update, water_heater
- **Two-phase end** — shows completion state (green checkmark) before dismissing
- **Throttled updates** with content deduplication
- **6-level icon fallback** — attribute → config → entity → registry → device class → domain default
- **Color support** — RGB, HSV, XY, Kelvin, named colors
- **Deep links** — primary and secondary tap-to-open URLs
- **TTL controls** — auto-delete after end, auto-end on stale activity
- **Priority** — 0–10 range for activity ordering
- **Rapid on/off handling** — cancels pending end if activity restarts
- **Automatic resume** on HA restart
- **4 services** for manual activity management from automations

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots in the top right corner and select **Custom repositories**
3. Add `https://github.com/mac-lucky/pushward-hass` with category **Integration**
4. Search for "PushWard" and install
5. Restart Home Assistant

### Manual

Copy the `custom_components/pushward` directory into your Home Assistant `custom_components/` folder and restart.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **PushWard**
3. Enter your PushWard integration key
4. Add entities to track via the integration's **Configure** button

### Adding an Entity

Each tracked entity uses a two-step flow:

**Step 1** — Pick an entity and a template:

| Template | Use case |
|----------|----------|
| `generic` | Flexible — progress bar, subtitle, icon |
| `countdown` | Timer with remaining time and end date |
| `alert` | Severity-based notification (critical/warning/info) |
| `steps` | Multi-step process (e.g., build stages) |

**Step 2** — Configure activity details (fields vary by template):

| Field | Description |
|-------|-------------|
| Slug | Unique ID (auto-generated from entity if blank) |
| Activity Name | Display name on iPhone |
| Icon | Static MDI or SF Symbol icon |
| Icon Attribute | Entity attribute for dynamic icon |
| Priority | 0–10 (default: 1) |
| Start / End States | States that trigger start or end |
| Update Interval | Min seconds between updates (default: 5) |
| Progress Attribute | Entity attribute holding 0–100 value |
| Remaining Time Attribute | Seconds remaining (countdown template) |
| Total Steps / Current Step Attribute | Steps tracking |
| Severity | critical, warning, or info (alert template) |
| Subtitle Attribute | Entity attribute for subtitle text |
| State Labels | Custom state→label mapping (e.g., `on=Running, off=Stopped`) |
| Completion Message | Text shown at end (default: "Complete") |
| Accent Color | Static hex color (#RRGGBB) |
| Accent Color Attribute | Entity attribute for dynamic color |
| URL / Secondary URL | Deep-link URLs (http/https) |
| Ended TTL | Seconds to keep activity after end |
| Stale TTL | Seconds of inactivity before auto-end |

## Services

All services are available under the `pushward` domain for use in automations and scripts.

### `pushward.create_activity`

Create a new activity (without starting it from entity tracking).

| Field | Required | Description |
|-------|----------|-------------|
| `slug` | Yes | Unique activity identifier |
| `name` | Yes | Display name on iPhone |
| `priority` | No | 0–10 (default: 1) |
| `ended_ttl` | No | Seconds after end before auto-delete |
| `stale_ttl` | No | Seconds of inactivity before auto-end |

### `pushward.update_activity`

Push a content update to an existing activity.

| Field | Required | Description |
|-------|----------|-------------|
| `slug` | Yes | Activity identifier |
| `state` | Yes | `ONGOING` or `ENDED` |
| `template` | No | generic, countdown, steps, or alert |
| `progress` | No | 0.0–1.0 |
| `state_text` | No | Display text |
| `icon` | No | SF Symbol or MDI icon |
| `subtitle` | No | Subtitle text |
| `accent_color` | No | Color name or hex |
| `remaining_time` | No | Seconds remaining |
| `url` / `secondary_url` | No | Tap-to-open URLs |
| `end_date` | No | Unix timestamp for countdown |
| `total_steps` / `current_step` | No | Steps progress |
| `severity` | No | critical, warning, or info |
| `completion_message` | No | End display message |

### `pushward.end_activity`

End an activity with an optional completion message.

| Field | Required | Description |
|-------|----------|-------------|
| `slug` | Yes | Activity identifier |
| `completion_message` | No | End display message |

### `pushward.delete_activity`

Delete an activity immediately (no completion animation).

| Field | Required | Description |
|-------|----------|-------------|
| `slug` | Yes | Activity identifier |

## Domain Defaults

When adding an entity, start and end states are pre-filled based on the entity's domain:

| Domain | Start States | End States | Default Icon |
|--------|-------------|------------|--------------|
| binary_sensor | on | off | mdi:toggle-switch |
| switch | on | off | mdi:toggle-switch |
| light | on | off | mdi:lightbulb |
| fan | on | off | mdi:fan |
| climate | heating, cooling | off, idle | mdi:thermostat |
| vacuum | cleaning | docked, idle | mdi:robot-vacuum |
| media_player | playing | off, idle, paused | mdi:cast |
| lock | unlocked | locked | mdi:lock |
| cover | opening, closing | open, closed | mdi:window-open |
| timer | active | idle, paused | mdi:timer-outline |
| update | on | off | mdi:package-up |
| water_heater | heating | off, idle | mdi:water-boiler |
| sensor | *(user-defined)* | *(user-defined)* | mdi:eye |
| weather | *(user-defined)* | *(user-defined)* | mdi:weather-cloudy |

## Requirements

- Home Assistant **2025.7.0** or newer
- A [PushWard](https://pushward.app) account with an integration key
- The [PushWard iOS app](https://pushward.app) installed on your iPhone

## Development

```bash
uv sync                                        # Install dependencies
uv run pytest tests/ -v                        # Run tests
uv run ruff check . && uv run ruff format .    # Lint + format
```

## License

[MIT](LICENSE)
