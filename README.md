[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://hacs.xyz)
[![Website](https://img.shields.io/badge/pushward.app-5B4FE5?style=for-the-badge&logo=safari&logoColor=white)](https://pushward.app)
[![TestFlight](https://img.shields.io/badge/TestFlight-Join_Beta-0D96F6?style=for-the-badge&logo=apple&logoColor=white)](https://testflight.apple.com/join/T4aT6s3W)

# PushWard for Home Assistant

[![CI](https://github.com/mac-lucky/pushward-hass/actions/workflows/ci.yml/badge.svg)](https://github.com/mac-lucky/pushward-hass/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Custom [HACS](https://hacs.xyz) integration that tracks Home Assistant entities as [PushWard](https://pushward.app) Live Activities on iPhone (Dynamic Island + Lock Screen).

> **New to PushWard?** Learn more at **[pushward.app](https://pushward.app)** and join the iOS beta on **[TestFlight](https://testflight.apple.com/join/T4aT6s3W)**.

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
- **Transactional email** — send emails to verified recipients via the `send_email` service
- **7 services** for activities, widgets, notifications, and email from automations

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
| Progress Entity / Attribute | 0–100 progress, optionally from a separate entity (see [Separate entities](#reading-values-from-separate-entities)) |
| Remaining Time Entity / Attribute | Seconds remaining (countdown), optionally from a separate entity with smart time parsing |
| Total Steps / Current Step Entity / Attribute | Steps tracking, optionally from a separate entity |
| Severity | critical, warning, or info (alert template) |
| Value Entity / Attribute | Numeric value (gauge/timeline), optionally from a separate entity |
| Min Value / Max Value | Gauge range bounds (default: 0–100) |
| Unit | Display unit for the value (e.g., °C, %) |
| Series | Attribute→label mapping for multi-series timeline |
| Scale | Y-axis scale — linear or logarithmic (timeline) |
| Decimal Places | Value display precision, 0–10 (timeline) |
| Smooth Lines | Enable curve interpolation between points (timeline) |
| Thresholds | Horizontal reference lines on the sparkline (timeline) |
| Back-History Period | Minutes of history to seed the sparkline on activity start (timeline, 0–1440) |
| Subtitle Entity / Attribute | Subtitle text, optionally from a separate entity |
| State Labels | Custom state→label mapping (e.g., `on=Running, off=Stopped`) |
| Completion Message | Text shown at end (default: "Complete") |
| Accent Color | Static hex color (#RRGGBB) |
| Accent Color Attribute | Entity attribute for dynamic color |
| URL / Secondary URL | Deep-link URLs (http/https) |
| Ended TTL | Seconds to keep activity after end |
| Stale TTL | Seconds of inactivity before auto-end |

### Reading values from separate entities

By default every value (remaining time, progress, subtitle, gauge value, current step, fired-at) is read from the **tracked entity** — either its state or one of its attributes. Many appliances instead expose these as **separate entities** (e.g. an LG washer has one sensor for the program state and another for the remaining time). For each value you can set an optional **source entity**:

- **Source entity empty** → read from the tracked entity (unchanged default behavior).
- **Source entity set, attribute empty** → read that entity's **state**.
- **Source entity set, attribute set** → read that **attribute** of the source entity.

So a washer Live Activity can use the program sensor as the tracked entity (driving start/end and the displayed state) and point **Remaining Time Entity** at the separate time sensor — no template helper required.

**Smart time parsing** — the remaining-time source accepts whatever format the sensor exposes:

- a **timestamp / finish-time** sensor (`device_class: timestamp`) → anchors the countdown's end date directly (most accurate, no drift),
- a **duration** sensor with a unit (`s` / `min` / `h` / `d`),
- an **`H:MM:SS`** or **`MM:SS`** string,
- a plain number of **seconds**.

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

> **Why so many fields?** Most are template-specific. In the Home Assistant UI (Developer
> Tools → Actions, or the automation editor) the template-specific fields are tucked into
> collapsed sections — **Countdown options**, **Steps options**, **Alert options**,
> **Gauge & Timeline options**, **Action buttons**, and **Display, color & sound**. Expand
> only the section that matches your template and ignore the rest. Every field stays optional
> and can still be set from YAML regardless of which section it lives in.

#### Which fields apply to which template

`slug`, `state`, and `template`, plus the universal display/override fields (`progress`,
`state_text`, `icon`, `subtitle`, `completion_message`, `accent_color`, `background_color`,
`text_color`, `remaining_time`, `sound`, `priority`), work with **every** template. The
remaining fields are template-specific:

| Field(s) | generic | countdown | steps | alert | gauge | timeline |
|----------|:-------:|:---------:|:-----:|:-----:|:-----:|:--------:|
| `end_date`, `warning_threshold`, `alarm`, `snooze_seconds` | | ✓ | | | | |
| `total_steps`, `current_step`, `step_labels`, `step_rows` | | | ✓ | | | |
| `severity`, `fired_at` | | | | ✓ | | |
| `min_value`, `max_value` | | | | | ✓ | |
| `value`, `unit` | | | | | ✓ | ✓ |
| `scale`, `decimals`, `smoothing`, `thresholds`, `units` | | | | | | ✓ |
| `url`, `secondary_url` | | | ✓ | ✓ | | |

Fields outside the active template's group have no visible effect for that template. If a call
fails, the error now carries the server's reason instead of a generic "Unknown error" — see
[Troubleshooting](#troubleshooting) below.

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
<!-- TODO(critical-alerts): re-add `critical` level and `volume` row once Apple approves entitlement -->
| `level` | No | iOS interruption level: passive, active, time-sensitive |
| `thread_id` | No | Groups notifications in Notification Center |
| `collapse_id` | No | APNs dedup key — replaces previous notification with same key (max 64 chars) |
| `source` | No | Source identifier for grouping in PushWard inbox |
| `source_display_name` | No | Human-readable source name in PushWard inbox |
| `activity_slug` | No | Link notification to an existing Live Activity |
| `push` | No | Send as APNs push alert (default: true). When false, inbox-only |

### `pushward.send_email`

Send a transactional email via PushWard. Requires an integration key with the `emails` capability. The recipient must already be added and confirmed in the PushWard iOS app — unverified addresses are rejected.

| Field | Required | Description |
|-------|----------|-------------|
| `to` | Yes | Recipient email address (must be a verified recipient on your account) |
| `subject` | Yes | Email subject line |
| `body` | No | Plain-text body (provide `body`, `html_body`, or both) |
| `html_body` | No | HTML body (provide `body`, `html_body`, or both) |

### `pushward.widget_refresh`

Force-refresh a tracked widget, bypassing the diff cache so it re-renders even when the value is unchanged. Provide exactly one of `slug` or `entity_id`.

| Field | Required | Description |
|-------|----------|-------------|
| `slug` | No\* | Widget slug identifier |
| `entity_id` | No\* | HA entity bound to the widget |

\* Exactly one of `slug` or `entity_id` is required.

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

## Translations

The integration ships with UI translations for 23 languages in addition to English. **All non-English translations were generated by an LLM and have not been reviewed by native speakers.** They may contain awkward phrasing, incorrect technical terms, or outright errors.

If you spot a bad translation, please [open an issue](https://github.com/mac-lucky/pushward-hass/issues) or submit a PR editing the relevant `custom_components/pushward/translations/<lang>.json` file. See [`custom_components/pushward/translations/README.md`](custom_components/pushward/translations/README.md) for details.

To use the English strings regardless of your Home Assistant language, switch your HA user profile language to English (**Settings → user profile → Language**).

## Troubleshooting

### Viewing logs

- **In the UI:** **Settings → System → Logs**, then search for `pushward`. Click an entry to expand the full traceback.
- **Log file:** the same lines are written to `<config>/home-assistant.log` (rotated to `home-assistant.log.1`).
- **Enable debug logging** (no restart needed) from **Developer Tools → Actions**, run `logger.set_level`:

  ```yaml
  action: logger.set_level
  data:
    custom_components.pushward: debug
  ```

  Or persist it in `configuration.yaml`:

  ```yaml
  logger:
    logs:
      custom_components.pushward: debug
  ```

  Narrow to one area with `custom_components.pushward.api` (HTTP calls) or `custom_components.pushward.activity_manager`.

### "Unknown error" when calling a service

Service calls surface the server's actual reason — a validation error for fixable problems (e.g. a missing key capability or an unverified email recipient), otherwise an error carrying the server's message. If a call still fails with a vague message, enable debug logging as above and read the `custom_components.pushward.api` lines for the HTTP status and response body. Common causes:

- The `slug` doesn't match an existing activity — create it first with `pushward.create_activity`.
- A value has the wrong type for the chosen template — see [Which fields apply to which template](#which-fields-apply-to-which-template).
- The integration key is missing a required capability (e.g. `widgets` or `emails`).

## Requirements

- Home Assistant **2025.7.0** or newer
- A [PushWard](https://pushward.app) account with an integration key
- The [PushWard iOS app](https://testflight.apple.com/join/T4aT6s3W) installed on your iPhone (available via TestFlight)

## Development

```bash
uv sync                                        # Install dependencies
uv run pytest tests/ -v                        # Run tests
uv run ruff check . && uv run ruff format .    # Lint + format
```

## License

[MIT](LICENSE)
