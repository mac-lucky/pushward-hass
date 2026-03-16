# PushWard for Home Assistant

[![CI](https://github.com/mac-lucky/pushward-hass/actions/workflows/ci.yml/badge.svg)](https://github.com/mac-lucky/pushward-hass/actions/workflows/ci.yml)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

Custom [HACS](https://hacs.xyz) integration that tracks Home Assistant entities as [PushWard](https://pushward.app) Live Activities on iPhone.

When an entity enters a configured "start" state (e.g., washer turns on), a Live Activity appears on the iPhone Dynamic Island and Lock Screen. When it enters an "end" state, the activity dismisses with a completion animation.

## Features

- Track any HA entity as a PushWard Live Activity
- Two-phase end: shows completion state before dismissing
- Periodic updates with content deduplication
- Domain-based defaults (binary_sensor, switch, climate, vacuum, etc.)
- Configurable templates: generic, countdown, alert, pipeline
- Rapid on/off handling (cancels pending end)
- Automatic resume on HA restart

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots in the top right corner and select "Custom repositories"
3. Add `https://github.com/mac-lucky/pushward-hass` with category "Integration"
4. Search for "PushWard" and install it
5. Restart Home Assistant

### Manual

Copy the `custom_components/pushward` directory into your Home Assistant `custom_components/` folder and restart.

## Configuration

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for "PushWard"
3. Enter your PushWard integration key
4. Add entities to track via the integration's configuration

## Requirements

- Home Assistant 2025.7.0 or newer
- A running [PushWard server](https://pushward.app)
- The [PushWard iOS app](https://pushward.app) installed on your iPhone
