# PushWard for Home Assistant

[![CI](https://github.com/mac-lucky/pushward-hass/actions/workflows/ci.yml/badge.svg)](https://github.com/mac-lucky/pushward-hass/actions/workflows/ci.yml)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Custom [HACS](https://hacs.xyz) integration that tracks Home Assistant entities as [PushWard](https://pushward.app) Live Activities on iPhone (Dynamic Island + Lock Screen).

When an entity enters a configured "start" state (e.g., washer turns on), a Live Activity appears on your iPhone. When it enters an "end" state, the activity dismisses with a two-phase completion animation.

## Features

- **Track any HA entity** as a PushWard Live Activity
- **6 templates** — generic, countdown, alert, steps, gauge, timeline
- **Template auto-suggestion** — picks the best template based on entity domain and device class
- **14 domain defaults** — binary_sensor, switch, climate, vacuum, media_player, lock, cover, timer, sensor, light, fan, weather, update, water_heater
- **Two-phase end** — shows completion state (green checkmark) before dismissing
- **Throttled updates** with content deduplication
- **6-level icon fallback** — attribute → config → entity → registry → device class → domain default
- **Color support** — RGB, HSV, XY, Kelvin, named colors
- **Deep links** — primary and secondary tap-to-open URLs (steps/alert templates)
- **TTL controls** — auto-delete after end, auto-end on stale activity
- **Priority** — 0–10 range for activity ordering
- **Rapid on/off handling** — cancels pending end if activity restarts
- **Automatic resume** on HA restart
- **Push notifications** — send alerts from automations via the `send_notification` service
- **5 services** for activity management and notifications from automations

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
| `gauge` | Numeric value with range (e.g., temperature, battery) |
| `timeline` | Sparkline chart with labeled value series |

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
| Value Attribute | Entity attribute holding a numeric value (gauge/timeline) |
| Min Value / Max Value | Gauge range bounds (default: 0–100) |
| Unit | Display unit for the value (e.g., °C, %) |
| Series | Attribute→label mapping for multi-series timeline |
| Scale | Y-axis scale — linear or logarithmic (timeline) |
| Decimal Places | Value display precision, 0–10 (timeline) |
| Smooth Lines | Enable curve interpolation between points (timeline) |
| Thresholds | Horizontal reference lines on the sparkline (timeline) |
| Back-History Period | Minutes of history to seed the sparkline on activity start (timeline, 0–1440) |
| Subtitle Attribute | Entity attribute for subtitle text |
| State Labels | Custom state→label mapping (e.g., `on=Running, off=Stopped`) |
| Completion Message | Text shown at end (default: "Complete") |
| Accent Color | Static hex color (#RRGGBB) |
| Accent Color Attribute | Entity attribute for dynamic color |
| URL / Secondary URL | Deep-link URLs (http/https) |
| Ended TTL | Seconds to keep activity after end |
| Stale TTL | Seconds of inactivity before auto-end |

### How the timeline sparkline backfill works

Setting **Back-History Period** tells the integration how far back to seed the sparkline when the activity starts. Because Home Assistant 2024.8 [removed light/climate attributes from the recorder database](https://github.com/home-assistant/core/issues/123028), the recorder can no longer be queried to rebuild attribute-based history (e.g. a light's `brightness`). Instead, the integration keeps its own in-memory ring buffer (up to 300 samples per tracked entity), populated from live state changes and persisted to `.storage/pushward_history.<entry_id>` so it survives Home Assistant restarts.

This has a few practical implications:

- **First-time activation is empty.** Right after installing the integration, the buffer has no samples yet — the sparkline only starts filling in once the tracked attribute changes while Home Assistant is running.
- **Backfill resolution matches state-change frequency.** If a light's brightness changes every 30 seconds, the sparkline shows 30-second resolution. There is no polling.
- **Fresh-install HA restart is still empty.** The buffer is saved to disk and reloaded on restart, so no data is lost — but if the file doesn't exist yet, the buffer starts empty.
- **For numeric-state sensors (temperature, humidity, etc.)** the recorder is still used as a fallback, so backfill works immediately on first install.

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
| `template` | No | generic, countdown, steps, alert, gauge, or timeline |
| `progress` | No | 0.0–1.0 |
| `state_text` | No | Display text |
| `icon` | No | SF Symbol or MDI icon |
| `subtitle` | No | Subtitle text |
| `accent_color` | No | Color name or hex |
| `remaining_time` | No | Seconds remaining |
| `url` / `secondary_url` | No | Tap-to-open URLs (steps/alert only) |
| `end_date` | No | Unix timestamp for countdown |
| `total_steps` / `current_step` | No | Steps progress |
| `severity` | No | critical, warning, or info |
| `completion_message` | No | End display message |
| `value` | No | Numeric value (gauge) or labeled values object (timeline) |
| `min_value` / `max_value` | No | Gauge range bounds |
| `unit` | No | Display unit (e.g., °C, %) |
| `scale` | No | Y-axis scale: linear or logarithmic (timeline) |
| `decimals` | No | Decimal places 0–10 (timeline) |
| `smoothing` | No | Curve interpolation between points (timeline) |
| `thresholds` | No | Horizontal reference lines on sparkline (timeline) |

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

### `pushward.send_notification`

Send a push notification via PushWard.

| Field | Required | Description |
|-------|----------|-------------|
| `title` | Yes | Notification title |
| `body` | Yes | Notification body text |
| `subtitle` | No | Subtitle shown below the title |
| `level` | No | iOS interruption level: passive, active, time-sensitive, critical |
| `volume` | No | Sound volume 0.0–1.0 (critical level only) |
| `thread_id` | No | Groups notifications in Notification Center |
| `collapse_id` | No | APNs dedup key — replaces previous notification with same key (max 64 chars) |
| `category` | No | Notification category for custom actions |
| `source` | No | Source identifier for grouping in PushWard inbox |
| `source_display_name` | No | Human-readable source name in PushWard inbox |
| `activity_slug` | No | Link notification to an existing Live Activity |
| `push` | No | Send as APNs push alert (default: true). When false, inbox-only |

## Domain Defaults

When adding an entity, start and end states are pre-filled based on the entity's domain:

| Domain | Start States | End States | Default Icon |
|--------|-------------|------------|--------------|
| binary_sensor | on | off | mdi:toggle-switch-variant |
| switch | on | off | mdi:toggle-switch-variant |
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
