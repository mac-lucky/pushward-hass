---
name: pushward-home-assistant
description: Set up and run the PushWard integration for Home Assistant - Live Activities on the iPhone Lock Screen and Dynamic Island (or an Android live notification) driven by entity state, home screen widgets, to-do reminders and push notifications from automations. Use when the user says "show the washer on my Lock Screen", "countdown to my next calendar event", "show when someone is on the way home", "widget with room temperatures", "remind me of my to-do list", "notify my phone from an automation" or "set up PushWard in Home Assistant", or when PushWard subentries or pushward.* actions come up in a Home Assistant config.
---

# PushWard for Home Assistant

PushWard (domain `pushward`, installed from HACS) turns Home Assistant state into things on the
user's phone. Everything is configured in the UI; there is no YAML for it. One PushWard account
is one config entry, and each item under it is a subentry:

| The user wants | Add | Subentry type |
|---|---|---|
| something live while it lasts (a wash cycle, a trip home, a meeting) | a tracked entity, which is a Live Activity | `tracked_entity` |
| a value to glance at on the home screen (temperatures, battery, a price) | a tracked widget | `tracked_widget` |
| a push before each due item of a to-do list | a tracked to-do list | `tracked_todo` |
| a one-off push from an automation | the `pushward.send_notification` action | none |

## Setup

1. HACS: search **PushWard**, download, restart Home Assistant. It is in the default HACS
   store; never tell the user to add a custom repository.
2. The user creates an integration key (`hlk_...`) in the PushWard app under
   **Settings > Integration Keys** (a `ha-*` slug scope is a good default) and pastes it into
   **Settings > Devices & Services > Add Integration > PushWard** themselves. Never ask for the
   key in the chat and never put it in a tool call. Widgets need the key's `widgets`
   permission; `send_email` needs `emails` and a recipient verified in the app.
3. Admin users now have a **PushWard** item in the sidebar.

## The PushWard panel

Send people to the sidebar panel first. It lists every tracked entity, widget and to-do list
with a live preview of what the phone shows, and its add/edit forms are the integration's own,
so every option below is there. **Send to phone** pushes the previewed item as a test (fixed
slugs `ha-preview` and `ha-preview-widget`, so a new test replaces the last one) and **End on
phone** takes it down again. **Open in playground** loads it into the pushward.app playground.
The same forms are under **Settings > Devices & Services > PushWard** for non-panel users.

## Live Activities: when they run

Step 1 picks the entity and a template; Step 2 holds the details. The template is suggested
from the entity:

| Template | Use for |
|---|---|
| `generic` | most things: state text, progress bar, subtitle, icon |
| `countdown` | a timer to a finish time (washer, oven, calendar event) |
| `alert` | a severity card (critical, warning, info) |
| `steps` | a multi-stage process |
| `gauge` | one number within a range |
| `timeline` | a sparkline of up to 10 series |
| `board` | 1-4 tiles, each from another entity |
| `log` | newest-first lines, one per state change |
| `media` | a media player card with cover art and buttons |

Lifecycle fields:

- **Start States / End States.** Prefilled per domain (`binary_sensor` on/off, `vacuum`
  cleaning/docked, ...). `*` as a start state means any state that is not an end state, so a
  `person` or `device_tracker` (defaults `*` and `home`) stays up while away in any zone.
  `unavailable` and `unknown` never start one.
- **Run Only While.** Rows of an entity and comma-separated states; the activity runs only
  while every row matches (example: `person.alex` with `home`, so it only shows while Alex is
  home to see it).
- **Calendar entities** ignore start/end states and follow the next timed event: **Lead Time**
  minutes before it (default 30) the activity opens and counts down to the start, then to the
  end, and it ends with the event. All-day events are skipped. **Subtitle Before the Event** and
  **Subtitle During the Event** replace the calendar name for each phase (for example
  `In progress`).
- **Priority** (0-10, in Display options) orders activities when several run at once.

How it ends: by default the card shows a green completion frame (Completion Message, default
"Complete") for 5 seconds, ends, and stays on the Lock Screen for a while. **Remove Right Away
When It Ends** (Advanced) skips the frame and takes the card off the phone at once. Auto-Remove
From Lock Screen (also Advanced) sets how long an ended card stays otherwise.

## Live Activities: what they show

Every value reads from the tracked entity by default, its state or an attribute. Each one can
point at a separate entity instead (a washer's remaining-time sensor, a phone's distance
sensor); those companion entities refresh the card but never start or end it.

- **State text:** State Labels map states to text (`on` shows `Running`). State Text Entity /
  Attribute shows another value instead, with a format (`auto`, `distance_m` gives `850 m` /
  `3.2 km`, `duration_s`, `duration_min`) and a pattern such as `{value} from home`.
- **Progress:** Progress Entity / Attribute as 0-100 (`direct`), or `remaining` for a value
  falling toward 0 (a distance, items left): the bar fills from the largest value seen this run.
  **Accent From Progress** colors the accent red, yellow, then green as it fills.
- **Remaining time:** a timestamp sensor, a duration sensor in s/min/h/d, `H:MM:SS`, or plain
  seconds. Live Progress ETA fills the bar smoothly toward that finish time.
- **Icon:** a static `mdi:` or SF Symbol name, or **Icon Per State** rows (optionally keyed off
  another entity, such as a direction-of-travel sensor).
- **Subtitle:** an entity/attribute, or Subtitle Columns that join several labelled values on
  one line (`ETA: 15 min`).

The full field list is the "All Step 2 fields" table in the README:
https://github.com/mac-lucky/pushward-hass#add-a-tracked-entity-live-activity

## Recipes

Give the user the panel field values. When you configure it yourself through ha-mcp (next
section), the same values go into the payload shown.

**Washer.** Entity `sensor.washer_state` (or a running `binary_sensor`), template `countdown`,
start `running`, end `idle, off`, Remaining Time Entity `sensor.washer_remaining_time`,
Completion Message `Laundry done`.

**Next calendar event.** Entity `calendar.family`, template `countdown`, Lead Time 30, Subtitle
During the Event `In progress`, Remove Right Away on.

```json
{"entity_id": "calendar.family", "template": "countdown", "calendar_lead_minutes": 30,
 "calendar_subtitle_during": "In progress",
 "data_sources": {}, "display_options": {"slug": "ha-calendar", "priority": 2},
 "colors": {}, "tap_actions": {}, "advanced": {"remove_on_end": true}}
```

**Someone on the way home.** Entity `person.sam`, template `generic`, start `*`, end `home`,
Run Only While `person.alex` = `home`; state text from the distance sensor with format
`distance_m` and pattern `{value} from home`; progress from the same sensor in `remaining`
mode; Accent From Progress on; Remaining Time Entity the travel-time sensor; Remove Right Away
on.

```json
{"entity_id": "person.sam", "template": "generic", "start_states": ["*"], "end_states": ["home"],
 "run_while": [{"entity_id": "person.alex", "states": "home"}],
 "data_sources": {"state_text_entity": "sensor.sam_home_distance", "state_text_format": "distance_m",
   "state_text_pattern": "{value} from home", "progress_entity": "sensor.sam_home_distance",
   "progress_mode": "remaining", "remaining_time_entity": "sensor.sam_travel_time"},
 "display_options": {"slug": "ha-sam-home", "icon": "mdi:map-marker", "priority": 3},
 "colors": {"accent_from_progress": true}, "tap_actions": {}, "advanced": {"remove_on_end": true}}
```

**Room temperatures widget.** Add a tracked widget on any one of the sensors, template
`stat_list`, one Stat Rows row per room (label, entity, unit). Widgets update on state change
by default (Trigger Mode `event`).

**To-do reminders.** Add a tracked to-do list, pick the `todo` entity, set Remind before due.
Every open item with a due date gets a scheduled push; the Done button completes the item in
Home Assistant.

## From automations

- `pushward.send_notification`: `title`, `body`, optional `level` (`passive`, `active`,
  `time-sensitive`, `critical`), `url`, `collapse_id`, `send_at`, and `actions`. An action
  without a `url` is recorded: the tap fires a `pushward_notification_action` event (`action`,
  `reply_text`, `notification_id`), shaped like the Companion app's, or wait for it with
  `pushward.get_notification_answer`. `pushward.clear_notification` and
  `pushward.cancel_notifications` take pushes back.
- Live Activities driven by an automation instead of a tracked entity:
  `pushward.create_activity`, then `pushward.update_activity_<template>` (for example
  `update_activity_countdown`), then `pushward.end_activity`.
- `pushward.widget_refresh` pushes a widget now; `pushward.send_email` mails a verified
  address.
- With more than one PushWard account, pass `config_entry_id` to every action.

Read `services.yaml` in the integration (or Developer Tools > Actions) for the full field list
before writing an action call; do not guess field names.

## Configuring it from an agent (ha-mcp)

With the ha-mcp server connected, subentries are created and edited through
`ha_config_set_helper` with `helper_type: "config_subentry"`, the PushWard `entry_id` and a
`subentry_type` from the first table: `action: "create"`, or `action: "update"` with the
`subentry_id`. Follow ha-mcp's own skill guide for the extra keys it requires on that call.
The integration's diagnostics (`ha_get_integration` with `include_diagnostics`) list every
subentry with its stored config, which is how you read one back.

- Put fields in their form sections: `data_sources`, `display_options`, `colors`,
  `tap_actions` and `advanced` for activities, plus the template's own section where it has one
  (`steps_options`, `timeline_options`, `media_options`, `image_options`). Lifecycle fields
  (`entity_id`, `template`, `start_states`, `end_states`, `run_while`, the calendar fields) stay
  at the top level.
- On create, pass every section the form shows, even as `{}`: the form requires each one. An
  update is a patch: pass only what changes, for example
  `{"advanced": {"remove_on_end": true}}`, and the rest keeps its stored value.
- Row editors take lists of objects (`run_while`, `icon_map`, `subtitle_columns`,
  `stat_rows`); `run_while` states are one comma-separated string.
- Each slug belongs to one owner. Two subentries, or a subentry and an automation, writing the
  same slug fight over the card; give a test copy its own slug.

Prefer the panel for anything the user wants to see while they tune it; use ha-mcp for bulk or
repeatable setup.

## Troubleshooting

- **Nothing appears:** check the entity's current state against Start States (and every Run
  Only While row), then the preview in the panel. A calendar needs a timed event within Lead
  Time.
- **The card stays after it ended:** turn on Remove Right Away When It Ends, or lower
  Auto-Remove From Lock Screen.
- **Quota:** the account sensors show usage. At the limit a repair issue appears and updates
  pause until the quota resets; an activity that ends meanwhile is still taken down.
- **Key errors:** a revoked key starts Home Assistant's re-authentication; a missing
  permission shows a persistent notification naming the slug. The user fixes the key in the app.
- Logs: Settings > System > Logs, filtered to `pushward`.
