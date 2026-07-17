[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://hacs.xyz)
[![Website](https://img.shields.io/badge/pushward.app-5B4FE5?style=for-the-badge&logo=safari&logoColor=white)](https://pushward.app)
[![App Store](https://img.shields.io/badge/App_Store-Download-0D96F6?style=for-the-badge&logo=apple&logoColor=white)](https://apps.apple.com/app/id6759689999)

# PushWard for Home Assistant

[![CI](https://github.com/mac-lucky/pushward-hass/actions/workflows/ci.yml/badge.svg)](https://github.com/mac-lucky/pushward-hass/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Mirror Home Assistant entities onto iPhone via [PushWard](https://pushward.app) — as **Live Activities** (Dynamic Island + Lock Screen) and **Home/Lock Screen widgets** — plus account usage sensors and services to send push notifications, transactional email, and activity/widget updates from your automations.

> **New to PushWard?** Learn more at **[pushward.app](https://pushward.app)**. The iOS app is on the **[App Store](https://apps.apple.com/app/id6759689999)**; you control everything from this integration and your automations.

## Contents

[How it works](#how-it-works) · [Features](#features) · [Prerequisites](#prerequisites) · [Installation](#installation) · [Configuration](#configuration) · [Account sensors](#account-sensors) · [Services](#services) · [Domain Defaults](#domain-defaults) · [Translations](#translations) · [Development](#development) · [CI/CD & Releases](#cicd--releases) · [Server compatibility](#server-compatibility) · [Troubleshooting](#troubleshooting) · [Requirements & License](#requirements--license)

## How it works

The integration watches HA entity state changes and surfaces them on your iPhone two independent ways, while polling your account's own usage counters for sensors:

```
HA entity state change
        ├──► ActivityManager ──► PushWard API ──► APNs ──► iPhone Live Activity
        │                       (api.pushward.app)         (Dynamic Island + Lock Screen)
        └──► WidgetManager   ──► PushWard API ──► APNs ──► iPhone Home / Lock Screen widget

Automations ──► pushward.* services ──► PushWard API ──► APNs push / email
GET /auth/me (polled every 15 min) ──► account usage & quota sensors
```

- **Live Activities** — when an entity enters a configured *start* state (e.g. the washer turns on), a Live Activity appears; on an *end* state it dismisses with a two-phase completion animation. Each tracked entity is a `tracked_entity` subentry.
- **Widgets** — an entity (or several, for `stat_list`) is bound to a server-rendered Home/Lock Screen widget that re-renders on state change or on a poll interval. Each widget is a `tracked_widget` subentry.

The two surfaces are independent — separate config, managers, and caches — and share only the API client and icon/color resolution.

## Features

- **Track any HA entity** as a PushWard Live Activity (Dynamic Island + Lock Screen)
- **8 activity templates** — generic, countdown, alert, steps, gauge, timeline, board, log
- **5 widget templates** — value, progress, gauge, status, stat_list (up to 6 entity rows)
- **Two widget trigger modes** — `event` (state-change) or `poll` (10–3600 s interval)
- **Account usage sensors** — notifications, Live Activity updates, widget updates, and emails consumed vs. plan limits, plus subscription tier
- **Template auto-suggestion** — picks the best activity template from entity domain and device class
- **14 domain defaults** — pre-filled start/end states and a default icon per HA domain
- **Companion source entities** — read remaining time, progress, value, etc. from a *separate* entity
- **Two-phase end** — shows a completion state (green checkmark) before dismissing
- **Throttled updates** with content deduplication
- **6-level icon fallback** — attribute → config → entity → registry → device class → domain default
- **Color support** — RGB, HSV, XY, Kelvin, named colors
- **TTL controls** — auto-delete after end, auto-end on stale activity
- **8 services** — create/update/end/delete activities, send notifications, send email, refresh/delete widgets

## Prerequisites

- **Home Assistant 2025.7.0** or newer
- A **PushWard account** with an **integration key** (`hlk_` prefix), created in the PushWard iOS app under **Settings → Integration Keys** (recommended scope `ha-*`)
  - The key needs the **`widgets`** permission to publish widgets
  - The key needs the **`emails`** capability *and* a verified recipient to use `send_email`
- The **[PushWard iOS app](https://pushward.app)** installed on the iPhone that will display the Live Activities / widgets

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mac-lucky&repository=pushward-hass&category=integration)

One-click adds this custom repository to HACS; then install and restart. Or do it manually:

1. Open **HACS** in Home Assistant
2. Click the three-dot menu (top right) → **Custom repositories**
3. Add `https://github.com/mac-lucky/pushward-hass` with category **Integration**
4. Search for **PushWard** and install
5. **Restart Home Assistant**

### Manual

Copy the `custom_components/pushward` directory into your Home Assistant `config/custom_components/` folder and restart Home Assistant.

## Configuration

Setup is UI-driven (config flow). The **only** value you enter is your integration key — the server URL is fixed to `https://api.pushward.app` and is not user-editable.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=pushward)

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **PushWard**
3. Paste your **integration key** (validated against `GET /auth/me`)

Once the entry exists, add tracked entities and widgets through the integration's **Configure** / **Add tracked entity** / **Add tracked widget** subentry flows. The key can be replaced later via **Reconfigure**, and the integration auto-prompts for reauth if the key becomes invalid.

| Setting | Required | Default | Description |
|---------|:--------:|---------|-------------|
| Integration key | Yes | — | PushWard key (`hlk_` prefix). Stored on the config entry; validated on setup. |
| Server URL | No | `https://api.pushward.app` | Fixed by the integration; not shown in the UI. |

### Add a tracked entity (Live Activity)

A two-step flow. **Step 1** picks the entity and a template (a better template is auto-suggested from the entity's domain/device class):

| Template | Use case |
|----------|----------|
| `generic` | Flexible — progress bar, subtitle, icon |
| `countdown` | Timer with remaining time and end date |
| `alert` | Severity-based notification (critical/warning/info) |
| `steps` | Multi-step process (e.g. build stages) |
| `gauge` | Numeric value with a range (e.g. temperature, battery) |
| `timeline` | Sparkline chart, up to 10 named series from attributes or separate entities |
| `board` | 1–4 tiles, each showing a value from a **separate** entity |
| `log` | Newest-first list of log lines (up to 20), one per state change |

**Step 2** configures the details (fields vary by template):

| Field | Description |
|-------|-------------|
| Slug | Unique ID, max 128 chars (auto-generated from entity if blank) |
| Activity Name | Display name on iPhone |
| Icon / Icon Attribute | Static MDI/SF Symbol, or an entity attribute for a dynamic icon |
| Priority | 0–10 (default: 1) |
| Start / End States | States that trigger start or end |
| Update Interval | Min seconds between updates (default: 5) |
| Progress Entity / Attribute | 0–100 progress, optionally from a separate entity |
| Remaining Time Entity / Attribute | Seconds remaining (countdown), with smart time parsing |
| Total Steps / Current Step Entity / Attribute | Steps tracking, optionally from a separate entity |
| Step Details | One row per step: label, row height (1-10), relative width, and color (steps template) |
| Severity | critical, warning, or info (alert template) |
| Value Entity / Attribute | Numeric value (gauge/timeline), optionally from a separate entity |
| Min / Max Value | Gauge range bounds (default: 0–100) |
| Unit | Display unit (e.g. °C, %) |
| Series | Rows mapping a tracked-entity attribute to a series label (multi-series timeline) |
| Series Entities | Rows binding a separate entity as a timeline line (entity, optional attribute and label), max 10 total |
| Primary Series | Label of the series shown as the headline value and used for the compact high/low range; empty = the tracked entity's own series (or the first configured one) |
| Per-Series Units | Rows mapping a series label to its unit (timeline template) |
| Scale / Decimal Places / Smooth Lines / Thresholds | Timeline sparkline options (Thresholds is a row table: value, optional color, optional label) |
| Back-History Period | Minutes of history to seed the sparkline on start (0–1440) |
| Board Tiles | Rows binding a separate entity to a tile (label, entity, attribute, unit, icon, color, URL), max 4 (board template) |
| Log Columns | Rows adding extra values to each log line (label, entity, attribute, unit), max 6 (log template) |
| Log Level Attribute | Attribute supplying each line's `info`/`warn`/`error` level (log template) |
| Subtitle Entity / Attribute | Subtitle text, optionally from a separate entity |
| State Labels | Rows giving custom display text per state (a state and its label, e.g. `on` shows `Running`) |
| Completion Message | Text shown at end (default: "Complete") |
| Accent / Background / Text Color (+ Attribute) | Static hex / named color, or an entity attribute |
| URL / Secondary URL | Deep-link URLs, http/https (steps/alert templates) |
| Ended TTL / Stale TTL | Auto-delete-after-end / auto-end-after-idle, 1–2592000 s |

Board tiles, stat rows, series entities, thresholds, log columns, state labels, the timeline series and per-series units maps, and the per-step details are all edited as row tables in the UI (add a row per entry). Stored configs and non-form callers keep working: the older comma-separated string forms for these fields are still accepted on input.

#### Reading values from separate entities

By default every value (remaining time, progress, subtitle, gauge value, current step, fired-at) is read from the **tracked entity** — its state or one of its attributes. Many appliances expose these as **separate entities** (e.g. an LG washer has one sensor for the program state and another for remaining time). For each value you can set an optional **source entity**:

- **Source entity empty** → read from the tracked entity (default).
- **Source entity set, attribute empty** → read that entity's **state**.
- **Source entity set, attribute set** → read that **attribute** of the source entity.

**Smart time parsing** — the remaining-time source accepts a `timestamp`/finish-time sensor (anchors the end date directly, no drift), a `duration` sensor with a unit (`s`/`min`/`h`/`d`), an `H:MM:SS`/`MM:SS` string, or a plain number of seconds.

#### Multi-entity timeline series

A **timeline** can plot up to **10 named series** on one chart, each its own line, color, and unit. There are two ways to supply them, and they combine:

- **Series** is a row table, one row per attribute of the tracked entity, mapping the attribute to a series label.
- **Series Entities** is a row table that binds *separate* entities as lines, so values from unrelated sensors share one chart (a PM2.5 sensor per room, solar arrays, etc.). Each row takes an **entity** (required; its state, or an **attribute** you name) and an optional **label**. Left off, the label defaults to the entity's friendly name (with the attribute name appended for attribute sources so two attributes of one entity stay distinct). Labels are frozen when you save (the server merges series by label), truncated to 32 chars, and de-duplicated with a numeric suffix.

Each series entity is tracked as a companion, so a change to any one re-samples the chart while the anchor entity owns start/end. Units auto-default from each state-sourced entity's `unit_of_measurement`; the **Per-Series Units** table (a series label and its unit per row) overrides them. Numeric attributes in the 0-255 range (e.g. `brightness`) are rescaled to 0-100. The 10-line cap covers Series and Series Entities combined; the server and iOS app already render multi-series timelines, so this is a Home Assistant configuration option only.

#### Timeline sparkline backfill

**Back-History Period** seeds the sparkline when the activity starts. What can be seeded depends on where each series reads its value:

- **State-sourced series** (a plain numeric sensor, a value entity, or a series entity read as a state) backfill from Home Assistant's recorder in one batched query, so they fill in immediately on start.
- **Attribute-based series** (Series attribute maps, a value attribute, or a series entity read as an attribute) cannot use the recorder: Home Assistant 2024.8 [removed most attributes from the recorder](https://github.com/home-assistant/core/issues/123028). These fill only from samples the integration collects live while it runs.

For attribute-based history the integration keeps its own in-memory ring buffer (max 300 samples per entity), populated from live state changes and persisted to `.storage/pushward.history.<entry_id>` so it survives restarts. That buffer is empty right after install and fills as the tracked attribute changes, at state-change resolution (no polling). Recorder points and buffered points are merged by timestamp into the same series, so a numeric sensor gets both its recorded past and any live samples. If your value lives in an attribute and you want recorder backfill, expose it as a template sensor's state.

#### Board tiles (multi-entity)

A **board** shows a compact grid of **1–4 tiles**, each reading a *separate* entity. The **anchor entity** (step 1) still owns the activity lifecycle through its start/end states; the tiles supply the displayed values, and a change to any tile entity refreshes the board while it is active. Add one row per tile in the **Board Tiles** table:

- **Label** (required, ≤32 chars) and **Entity** (required) are the minimum.
- **Attribute** (optional) reads that attribute instead of the entity state.
- **Unit** (optional, ≤8 chars), **Icon** (optional — an SF Symbol like `cpu.fill` or an MDI icon like `mdi:thermometer`), **Color** (optional named or hex), and **URL** (optional per-tile tap target) follow.

Each tile **value** is rendered as text (so `Open`, `On`, and numbers all work) and capped at 16 chars. Tiles whose entity is unavailable are skipped.

#### Log lines

A **log** shows a newest-first list of up to **20 lines**. The integration appends one line on every state change of the tracked entity (the line **text** is the formatted state, honoring State Labels), accumulating a rolling buffer that is injected into each push and persisted across restarts in `.storage/pushward.history.<entry_id>`. Set the optional **Log Level Attribute** to an attribute holding `info`, `warn`, or `error` to tag each line's severity. (The server also keeps a longer scrollable backlog server-side; the integration never sends it.)

Consecutive lines with identical text are collapsed, so attribute-only churn (a light's brightness settling while its state stays `on`) would otherwise show only a bare `On`. Use the **Log Columns** table to append extra values to each line so it carries *what* changed — attributes of the tracked entity and/or values from other entities. Add one row per column (max 6), each with an optional **Label** and **Unit** plus a source:

- **Entity** empty, **Attribute** set reads that attribute of the tracked entity (`brightness`).
- **Entity** set, **Attribute** empty reads that entity's state (`binary_sensor.door`).
- **Entity** and **Attribute** both set reads that attribute of the other entity.

**Label** (optional) renders the column as `Label: value`; **Unit** (optional) is appended to the value as a literal suffix (no conversion).

Each line's text is the state label followed by ` · ` and each resolved column. Values are raw Home Assistant values (e.g. `brightness` is 0–255). Columns whose source is missing or unavailable are skipped; if every column resolves empty (e.g. the lamp is off so `brightness` is absent) the line falls back to just the state label. Other-entity columns are tracked as companions, so a change in any one appends a new composed line while the tracked entity still owns start/end.

Example for a lamp — a `K`-suffixed column reading `color_temp_kelvin` plus a bare `brightness` column render lines like `On · 4000K · 153`, and a brightness change now produces a distinct line instead of collapsing into the previous `On`.

### Add a tracked widget

A two-step flow mirroring entities. **Step 1** picks the entity, a widget template, and an optional slug override:

| Template | Use case |
|----------|----------|
| `value` | A single numeric value |
| `progress` | A value rendered as a progress bar |
| `gauge` | A value within a min/max range |
| `status` | A label/icon status (optionally severity-colored) |
| `stat_list` | Up to 6 rows, each bound to a **separate** entity |

**Step 2** configures the widget (publishing widgets requires the `widgets` key permission):

| Field | Description |
|-------|-------------|
| Widget Name | Display name |
| Value Attribute | Source attribute (value/progress/gauge); blank = entity state |
| Unit | Display unit (value/progress/gauge) |
| Min / Max Value | Gauge range bounds (default: 0–100) |
| Severity | "", info, warning, critical, success (status template) |
| Stat Rows | Rows binding a separate entity to a stat (label, entity, attribute, unit), max 6 (stat_list) |
| Label / Label Attribute | Static label or an entity attribute |
| Subtitle Attribute | Subtitle text from an attribute |
| Icon / Icon Attribute | Static MDI/SF Symbol or an entity attribute |
| Accent / Background / Text Color (+ Attribute) | Colors, static or from an attribute |
| Tap Action URL / Foreground | Deep link opened when the widget is tapped |
| Trigger Mode | `event` (state-change) or `poll` |
| Poll Interval | Seconds between re-evaluations in poll mode (10–3600, default 60) |

How many `stat_list` rows are visible depends on the widget size. By default a medium or large Home Screen widget shows all 6 rows; the small widget shows 4 and the Lock Screen rectangular shows 3, packing in up to 6 when every value is very short (for example a single status glyph). You can change Row Density per widget in the PushWard iOS app: Compact packs two columns to show up to 6 rows on any size (labels may truncate on the small placements), and Comfortable keeps a single column with larger rows. To see all 6 rows with full labels, use a medium or large widget, or set Compact.

## Account sensors

Each config entry registers **5 sensors** under one service device named **PushWard**, fed by a coordinator that polls `GET /auth/me` every **15 minutes**. They report your account's own consumption against its plan limits (these sensors stay *unavailable* on older servers that don't return usage to integration keys):

| Sensor | State | Attributes |
|--------|-------|------------|
| Notifications used | Count this period (`TOTAL_INCREASING`) | `limit`, `remaining`, `percent_used`, `period`, `resets_at` — plus `used_this_month`, `daily_resets_at` on premium |
| Live Activity updates used | Count this period | `limit`, `remaining`, `percent_used`, `period`, `resets_at` |
| Widget updates used | Count this period | `limit`, `remaining`, `percent_used`, `period`, `resets_at` |
| Emails used | Count this period | `limit`, `remaining`, `percent_used`, `period`, `resets_at` |
| Subscription tier | `free` or `premium` (ENUM) | — |

On premium, uncapped resources report `limit: unlimited`, and the notifications counter switches to a daily cap (hence `used_this_month` / `daily_resets_at`).

## Services

All services live in the `pushward` domain.

### `pushward.create_activity`

Create a new activity.

| Field | Required | Description |
|-------|:--------:|-------------|
| `slug` | Yes | Unique activity identifier |
| `name` | Yes | Display name on iPhone |
| `priority` | No | 0–10 (default: 1) |
| `ended_ttl` | No | Seconds after end before auto-delete (1–2592000) |
| `stale_ttl` | No | Seconds of inactivity before auto-end (1–2592000) |

### `pushward.update_activity_<template>`

Push a content update to an existing activity. There is **one action per template** —
`update_activity_generic`, `update_activity_countdown`, `update_activity_steps`,
`update_activity_alert`, `update_activity_gauge`, `update_activity_timeline`,
`update_activity_board`, `update_activity_log` — so the UI shows only the fields that template
supports (Home Assistant cannot hide service fields based on another field's value, so a single
action with collapsed sections would always surface every template's fields). The template is
implied by the action name; you no longer pass a `template` field.

**Common fields** — accepted by every `update_activity_*` action:

| Field | Required | Description |
|-------|:--------:|-------------|
| `slug` | Yes | Activity identifier |
| `state` | Yes | `ongoing` or `ended` |
| `state_text` | No | Display text |
| `subtitle` | No | Subtitle text |
| `icon` | No | SF Symbol or MDI icon |
| `progress` | No | 0.0–1.0 |
| `completion_message` | No | End display message |
| `accent_color` / `background_color` / `text_color` | No | Hex or named color |
| `remaining_time` | No | Seconds remaining |
| `sound` | No | default, chime, alert, success, warning, bell, ding, buzz, notification |
| `priority` | No | Per-update priority override (0–10) |
| `url` / `secondary_url` | No | Tap-target URLs (http(s) **or** a custom scheme like `homeassistant://`) |
| `tap_action` | No | Whole-activity tap target / silent webhook as an object — see [Action objects](#action-objects) |
| `url_action` / `secondary_url_action` | No | Primary / secondary button as an object (adds `title`, `icon`) — see [Action objects](#action-objects) |

> **Action objects** <a id="action-objects"></a> — `tap_action`, `url_action`, and
> `secondary_url_action` take `{ url, foreground, method, headers, body }` (the button forms
> also accept `title` and `icon`). `url` is required and may use any scheme except
> `javascript`/`data`/`file`/`vbscript`; `method`/`headers`/`body` turn the action into a
> silent HTTP webhook and are only valid on an `http(s)` URL. The legacy `url`/`secondary_url`
> strings remain as a shorthand for a plain tap target.

**Template-specific fields** — added by the matching action:

| Action | Extra fields |
|--------|--------------|
| `update_activity_countdown` | `end_date`, `duration`, `start_date`, `warning_threshold`, `alarm`, `snooze_seconds` |
| `update_activity_steps` | `total_steps`, `current_step`, `step_labels`, `step_rows` |
| `update_activity_alert` | `severity`, `fired_at` |
| `update_activity_gauge` | `value`, `min_value`, `max_value`, `unit` |
| `update_activity_timeline` | `value`, `unit`, `units`, `scale`, `decimals`, `smoothing`, `thresholds`, `history` |
| `update_activity_board` | `tiles` |
| `update_activity_log` | `lines` |
| `update_activity_generic` | _(common fields only)_ |

> **`board` / `log` use a lean schema.** They render no progress bar and no whole-activity
> button slots, so `update_activity_board` and `update_activity_log` accept only the labels
> (`state_text`, `subtitle`, `icon`), appearance (`completion_message`, the colors, `sound`,
> `priority`), the whole-activity `tap_action`, and their template field (`tiles` / `lines`) —
> **not** `progress`, `remaining_time`, `url`, `secondary_url`, `url_action`, or
> `secondary_url_action` (board tap targets are per-tile via each tile's `url_action`).
> `tiles` is a list of 1–4 objects `{ label, value, unit?, icon?, color?, trend?, url_action? }`
> (`value` is a string ≤16 chars). `lines` is a list of 1–20 newest-first objects
> `{ text, at?, level? }` where `level` is `info`/`warn`/`error`.

> `duration` (integer seconds or a string like `"30m"` / `"1h30m"`) is the set-and-forget
> alternative to `end_date`: the server re-anchors `start_date = now` and
> `end_date = now + duration`, which is what lets iOS animate the countdown's progress bar.
> Send `end_date` directly for mid-flight updates that must preserve the original timer —
> **if both are sent, `duration` wins** (it overwrites start/end). `timeline`'s `history`
> is a one-time seed (`{ series: [{ timestamp, value }] }`); the server owns the series
> after the first update.

`step_labels` and `step_rows` are **ordered lists** (one entry per step, length must equal
`total_steps`) — e.g. `step_labels: ["Build", "Test", "Deploy"]`, `step_rows: [1, 1, 2]`.

> **`pushward.update_activity` is deprecated.** The original single action (with a `template`
> field and collapsed sections) still works for backward compatibility but logs a deprecation
> warning and will be removed in a future release. Switch automations to the template-specific
> action above.

### `pushward.end_activity`

End an activity with an optional completion message.

| Field | Required | Description |
|-------|:--------:|-------------|
| `slug` | Yes | Activity identifier |
| `completion_message` | No | End display message |

### `pushward.delete_activity`

Delete an activity immediately (no completion animation).

| Field | Required | Description |
|-------|:--------:|-------------|
| `slug` | Yes | Activity identifier |

### `pushward.send_notification`

Send a push notification.

| Field | Required | Description |
|-------|:--------:|-------------|
| `title` | Yes | Notification title |
| `body` | Yes | Notification body text |
| `subtitle` | No | Subtitle below the title |
| `level` | No | iOS interruption level: passive, active, time-sensitive |
| `thread_id` | No | Groups notifications in Notification Center |
| `collapse_id` | No | APNs dedup key, replaces same-key notification (max 64 chars) |
| `source` / `source_display_name` | No | Grouping ID + label in the PushWard inbox |
| `activity_slug` | No | Link the notification to an existing Live Activity |
| `url` | No | Deep-link URL opened on tap |
| `media` | No | Object `{ url, type }` — type is image, video, or audio |
| `icon_url` | No | Custom icon URL |
| `metadata` | No | Arbitrary key-value pairs for custom app handling |
| `actions` | No | List of action buttons `{ id, title, url, foreground, destructive, authentication_required, icon }`; `url` may use a custom scheme, and `method`/`headers`/`body` make the button a silent HTTP webhook (http(s) only) |
| `push` | No | Send as APNs push (default: true); when false, inbox-only |

### `pushward.send_email`

Send a transactional email. Requires the key's **`emails`** capability; the recipient must already be added and confirmed in the PushWard iOS app (the integration cannot verify recipients itself).

| Field | Required | Description |
|-------|:--------:|-------------|
| `to` | Yes | Recipient email (a verified address on your account) |
| `subject` | Yes | Subject line |
| `body` | No | Plain-text body (provide `body`, `html_body`, or both) |
| `html_body` | No | HTML body (provide `body`, `html_body`, or both) |

### `pushward.widget_refresh`

Force-refresh a tracked widget, bypassing the diff cache so it re-renders even when the value is unchanged. Provide **exactly one** of `slug` or `entity_id`.

| Field | Required | Description |
|-------|:--------:|-------------|
| `slug` | No\* | Widget slug identifier |
| `entity_id` | No\* | HA entity bound to the widget |

\* Exactly one of `slug` or `entity_id` is required.

### `pushward.delete_widget`

Delete a widget on the server (`DELETE /widgets/{slug}`). Provide **exactly one** of `slug` or `entity_id`. Removing a tracked-widget subentry (or the whole integration) already deletes its server-side widget automatically — use this to clean up a widget published manually or one whose subentry is gone.

| Field | Required | Description |
|-------|:--------:|-------------|
| `slug` | No\* | Widget slug identifier |
| `entity_id` | No\* | HA entity bound to the widget |

\* Exactly one of `slug` or `entity_id` is required. If a tracked-widget subentry still drives the slug, it will be re-created on the next restart/sync — remove the subentry to delete it permanently.

### Example automation

```yaml
automation:
  - alias: Notify when the front door opens
    triggers:
      - trigger: state
        entity_id: binary_sensor.front_door
        to: "on"
    actions:
      - action: pushward.send_notification
        data:
          title: Front Door
          body: The front door was opened.
          level: time-sensitive
          thread_id: home-security
```

## Domain Defaults

When adding an entity, start/end states are pre-filled from the entity's domain:

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

The integration ships UI translations for **23 languages in addition to English** (24 locale files total). **All non-English translations are LLM-generated and have not been reviewed by native speakers** — they may contain awkward phrasing or errors. To report or fix one, [open an issue](https://github.com/mac-lucky/pushward-hass/issues) or edit the relevant `custom_components/pushward/translations/<lang>.json` (see [`custom_components/pushward/translations/README.md`](custom_components/pushward/translations/README.md)). To force English regardless of your HA language, switch your HA user profile language to English (**Settings → user profile → Language**).

## Development

The toolchain is [`uv`](https://docs.astral.sh/uv/) + [`ruff`](https://docs.astral.sh/ruff/), matching CI:

```bash
uv sync                                                            # Install deps (CI uses --frozen)
uv run pytest tests/ -v                                            # Run tests
uv run pytest tests/ -v --cov=custom_components/pushward --cov-report=term-missing  # With coverage
uv run pytest tests/test_api.py -v -k "test_name"                 # Single test
uv run ruff check . && uv run ruff format .                       # Lint + format
```

Requires Python **3.13.2+**. CI additionally runs **HACS validation** and **hassfest** on every push and PR.

## CI/CD & Releases

- **CI** (`.github/workflows/ci.yml`): HACS validation, hassfest, ruff lint+format, and pytest with coverage on every push/PR.
- **Releases**: the integration version lives in `custom_components/pushward/manifest.json` (currently **0.30.0**). Bump it and push a matching **`v*`** git tag — CI builds the changelog and creates the GitHub release automatically. **Do not create releases manually.** HACS only sees GitHub releases, and `hide_default_branch: true` is set in `hacs.json`.

## Server compatibility

This integration talks to the public PushWard REST API at **`https://api.pushward.app`**, authenticating with `Authorization: Bearer <integration_key>`. Endpoints used: `GET /auth/me`, `POST/PATCH/DELETE /activities`, `POST/PATCH/DELETE /widgets`, `POST /notifications`, `POST /emails`. The request/response contract — including the Live Activity `ContentState` shape and widget content caps — is owned by the PushWard server; this integration mirrors those caps in `const.py`. Widget endpoints require the key's `widgets` permission; `POST /emails` requires the `emails` capability plus a verified recipient. The client retries with exponential backoff (up to 5 attempts, max 5 concurrent) and honors `Retry-After` on 429.

## Troubleshooting

**View logs:** **Settings → System → Logs**, then search for `pushward` (the same lines land in `<config>/home-assistant.log`).

**Enable debug logging** (no restart) from **Developer Tools → Actions**:

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

**Common failures:**

- **Setup / reauth fails** — the integration key is invalid or expired (401). Create a fresh key in the iOS app and re-enter it.
- **Service call rejected with a server reason** — fixable problems surface as a validation error (e.g. a missing `widgets`/`emails` capability, or an unverified email recipient). Read the `custom_components.pushward.api` debug lines for the HTTP status and body.
- **`slug` doesn't match an existing activity** — create it first with `pushward.create_activity`.
- **Wrong field for the chosen template** — see [which fields apply to which template](#pushwardupdate_activity_template).
- **Widget never appears** — confirm the key has the `widgets` permission, and that the bound entity has a renderable value (value/progress/gauge widgets are skipped when the value isn't numeric).

## Requirements & License

- **Home Assistant 2025.7.0+** (set in `hacs.json`)
- **Python 3.13.2+**
- A [PushWard](https://pushward.app) account and integration key
- The PushWard iOS app on your iPhone ([App Store](https://apps.apple.com/app/id6759689999))

Licensed under [MIT](LICENSE).
