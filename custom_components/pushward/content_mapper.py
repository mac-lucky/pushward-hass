"""Map Home Assistant state/attributes to PushWard content."""

from __future__ import annotations

import contextlib
import logging
import re
import time
from urllib.parse import urlparse

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTime
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.color import (
    color_hs_to_RGB,
    color_temperature_to_rgb,
    color_xy_to_RGB,
)
from homeassistant.util.unit_conversion import DurationConverter

from .const import (
    BOARD_MAX_TILES,
    BOARD_TILE_ICON_MAX,
    BOARD_TILE_LABEL_MAX,
    BOARD_TILE_UNIT_MAX,
    BOARD_TILE_VALUE_MAX,
    BOARD_TRENDS,
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ALARM,
    CONF_BACKGROUND_COLOR,
    CONF_BACKGROUND_COLOR_ATTRIBUTE,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_CURRENT_STEP_ENTITY,
    CONF_DECIMALS,
    CONF_ENTITY_ID,
    CONF_FIRED_AT_ATTRIBUTE,
    CONF_FIRED_AT_ENTITY,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_LABEL,
    CONF_LOG_COLUMNS,
    CONF_LOG_LEVEL_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ATTR,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SCALE,
    CONF_SECONDARY_URL,
    CONF_SECONDARY_URL_FOREGROUND,
    CONF_SECONDARY_URL_TITLE,
    CONF_SERIES,
    CONF_SERIES_ENTITIES,
    CONF_SEVERITY,
    CONF_SEVERITY_LABEL,
    CONF_SMOOTHING,
    CONF_SNOOZE_SECONDS,
    CONF_STATE_LABELS,
    CONF_STEP_LABELS,
    CONF_STEP_ROWS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_SUBTITLE_ENTITY,
    CONF_TAP_ACTION_FOREGROUND,
    CONF_TAP_ACTION_URL,
    CONF_TEMPLATE,
    CONF_TEXT_COLOR,
    CONF_TEXT_COLOR_ATTRIBUTE,
    CONF_THRESHOLDS,
    CONF_TILES,
    CONF_TOTAL_STEPS,
    CONF_UNIT,
    CONF_UNITS,
    CONF_URL,
    CONF_URL_FOREGROUND,
    CONF_URL_TITLE,
    CONF_VALUE_ATTRIBUTE,
    CONF_VALUE_ENTITY,
    CONF_WARNING_THRESHOLD,
    DEFAULT_DECIMALS,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    DEFAULT_SEVERITY,
    DEFAULT_TAP_ACTION_FOREGROUND,
    DEFAULT_TOTAL_STEPS,
    DEVICE_CLASS_ICONS,
    DOMAIN_DEFAULTS,
    LOG_COLUMN_LABEL_MAX,
    LOG_COLUMN_VALUE_MAX,
    LOG_LEVELS,
    LOG_LINE_TEXT_MAX,
    normalize_slug,
)

_LOGGER = logging.getLogger(__name__)

# Timeline display fields carried forward to completion content.
# Keep in sync with the fields emitted in map_content's timeline branch.
_TIMELINE_CARRY_FIELDS = ("unit", "scale", "decimals", "smoothing", "thresholds", "units")

# Common (all-template) fields carried from last_content into completion.
_COMMON_CARRY_FIELDS = ("background_color", "text_color")

# Mirrors server-side ValidateColor: 6- or 8-digit hex with optional '#', or
# one of the server's allowlisted named colors. Must stay in sync with
# pushward-server/internal/model/activity.go:validNamedColors.
_COLOR_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")
_COLOR_NAMED = frozenset(
    {
        "red",
        "orange",
        "yellow",
        "green",
        "blue",
        "purple",
        "pink",
        "indigo",
        "teal",
        "cyan",
        "mint",
        "brown",
    }
)


def sanitize_slug(entity_id: str) -> str:
    """Convert an HA entity_id to a PushWard slug.

    sensor.washing_machine_status -> ha-sensor-washing-machine-status
    """
    return f"ha-{normalize_slug(entity_id)}"


def color_to_str(value: object) -> str:
    """Convert an HA color attribute to a string the API accepts.

    Handles rgb_color (3-tuple), rgbw/rgbww (4/5-tuple, takes RGB),
    xy_color (2-tuple 0-1), hs_color (2-tuple hue 0-360, sat 0-100),
    color_temp_kelvin (int), and plain named-color / hex strings.

    Returns "" for anything unrecognized so callers fall back to the default
    accent instead of emitting a garbage color string the server will reject.
    """
    if isinstance(value, (list, tuple)):
        try:
            if len(value) >= 3:
                r, g, b = int(value[0]), int(value[1]), int(value[2])
                return f"#{r:02x}{g:02x}{b:02x}"
            if len(value) == 2:
                a, b_val = float(value[0]), float(value[1])
                if a <= 1.0 and b_val <= 1.0:
                    r, g, b = color_xy_to_RGB(a, b_val)
                else:
                    r, g, b = color_hs_to_RGB(a, b_val)
                return f"#{r:02x}{g:02x}{b:02x}"
        except (TypeError, ValueError):
            return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            rf, gf, bf = color_temperature_to_rgb(float(value))
            return f"#{int(rf):02x}{int(gf):02x}{int(bf):02x}"
        except (TypeError, ValueError):
            return ""
    if isinstance(value, str):
        stripped = value.strip()
        if _COLOR_HEX_RE.match(stripped) or stripped.lower() in _COLOR_NAMED:
            return stripped
    return ""


def resolve_color(state: State, config: dict, static_key: str, attr_key: str) -> str:
    """Resolve a color value: dynamic attribute > static config > empty string."""
    attr_name = config.get(attr_key)
    if attr_name:
        raw = state.attributes.get(attr_name)
        if raw:
            resolved = color_to_str(raw)
            if resolved:
                return resolved
    return config.get(static_key, "")


def build_tap_action(url: str, foreground: bool, title: str = "") -> dict | None:
    """Build a server-side TapAction dict from URL + foreground + optional button title.

    Returns None when url is empty. For http(s) URLs with foreground=False,
    auto-injects ``method="POST"`` so iOS treats it as a silent webhook
    (without an HTTP shape, iOS drops the tap per pushward-server fa4a98f).
    Custom-scheme URLs (e.g. ``homeassistant://``) ignore foreground.
    """
    url = (url or "").strip()
    if not url:
        return None
    action: dict = {"url": url, "foreground": bool(foreground)}
    if not foreground:
        scheme = urlparse(url).scheme.lower()
        if scheme in ("http", "https"):
            action["method"] = "POST"
    title = (title or "").strip()
    if title:
        action["title"] = title
    return action


def add_tap_action(content: dict, config: dict, *, key: str = "tap_action") -> None:
    """Attach a structured tap-action to content[key] from CONF_TAP_ACTION_URL/FOREGROUND.

    Shared between activity and widget mappers — the widget-wide tap target has
    the same wire shape on both surfaces.
    """
    action = build_tap_action(
        config.get(CONF_TAP_ACTION_URL, ""),
        config.get(CONF_TAP_ACTION_FOREGROUND, DEFAULT_TAP_ACTION_FOREGROUND),
    )
    if action is not None:
        content[key] = action


# (url_action_slot_key, url_config_key, foreground_config_key, title_config_key)
_BUTTON_SLOTS: tuple[tuple[str, str, str, str], ...] = (
    ("url_action", CONF_URL, CONF_URL_FOREGROUND, CONF_URL_TITLE),
    ("secondary_url_action", CONF_SECONDARY_URL, CONF_SECONDARY_URL_FOREGROUND, CONF_SECONDARY_URL_TITLE),
)


def _add_tap_actions(content: dict, entity_config: dict) -> None:
    """Add structured tap_action / url_action / secondary_url_action to content.

    tap_action is universal (every template); url_action and secondary_url_action
    are emitted only for steps/alert templates that render button affordances.
    """
    add_tap_action(content, entity_config)

    if entity_config.get(CONF_TEMPLATE, "generic") not in ("steps", "alert"):
        return

    for slot_key, url_key, foreground_key, title_key in _BUTTON_SLOTS:
        action = build_tap_action(
            entity_config.get(url_key, ""),
            entity_config.get(foreground_key, DEFAULT_TAP_ACTION_FOREGROUND),
            entity_config.get(title_key, ""),
        )
        if action is not None:
            content[slot_key] = action


def _resolve_device_class_icon(domain: str, device_class: str) -> str:
    """Resolve MDI icon from entity domain + device_class.

    Modern HA integrations use frontend-only icon translations, so
    state.attributes["icon"] and entity_registry.original_icon are empty.
    This mirrors the HA frontend's device-class icon tables.
    The ``number`` domain shares sensor device-class icons.
    """
    if not domain or not device_class:
        return ""
    icon = DEVICE_CLASS_ICONS.get(f"{domain}.{device_class}", "")
    if not icon and domain == "number":
        icon = DEVICE_CLASS_ICONS.get(f"sensor.{device_class}", "")
    return icon


def lookup_registry_icon(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """Return the entity registry's icon for entity_id, or None.

    Defensive against a hass-like object without an entity registry (e.g. a test
    stub) so per-tile board icon resolution never crashes the mapper.
    """
    if not entity_id:
        return None
    try:
        registry = er.async_get(hass)
    except (AttributeError, KeyError):
        return None
    entry = registry.async_get(entity_id)
    if entry is None:
        return None
    return entry.icon or entry.original_icon or None


def resolve_icon(state: State, config: dict, registry_icon: str | None = None) -> str:
    """Resolve an entity's display icon via a 6-level fallback chain.

    Order: icon_attribute (dynamic from HA attribute) → static CONF_ICON
    (user-configured) → state.attributes["icon"] (legacy / _attr_icon) →
    entity registry icon → device_class icon (mirrors HA frontend tables)
    → domain default. Returns "" only when every step yields nothing,
    which is rare given the domain-default fallback.
    """
    icon = ""
    icon_attr = config.get(CONF_ICON_ATTRIBUTE)
    if icon_attr:
        dynamic_icon = state.attributes.get(icon_attr)
        if dynamic_icon:
            icon = str(dynamic_icon)
    if not icon:
        icon = config.get(CONF_ICON, "") or ""
    if not icon:
        entity_icon = state.attributes.get("icon")
        if entity_icon:
            icon = str(entity_icon)
    if not icon and registry_icon:
        icon = registry_icon
    if not icon:
        device_class = state.attributes.get("device_class", "")
        icon = _resolve_device_class_icon(state.domain, device_class)
    if not icon:
        icon = get_domain_defaults(state.domain)["icon"]
    return icon


def _format_state_label(state: State, entity_config: dict) -> str:
    """Format an entity state into a display label.

    Uses a custom mapping from CONF_STATE_LABELS when one matches, else the raw
    state with underscores spaced out and capitalized. Shared by the activity
    ``state`` field and the log template's per-line text.
    """
    state_labels = entity_config.get(CONF_STATE_LABELS) or {}
    if state.state in state_labels:
        return state_labels[state.state]
    return state.state.replace("_", " ").capitalize()


def _state_epoch(state: State) -> int | None:
    """Return ``state.last_updated`` as a unix-second int, or None if unavailable.

    Defensive against mock/partial State objects (tests) whose ``last_updated`` is
    not a real datetime — a bad value yields None so the optional ``at`` field is
    simply omitted rather than crashing the mapper.
    """
    last_updated = getattr(state, "last_updated", None)
    if last_updated is None:
        return None
    try:
        return int(last_updated.timestamp())
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def _resolve_log_columns(state: State, config: dict, hass: HomeAssistant | None) -> list[str]:
    """Resolve CONF_LOG_COLUMNS into rendered column strings for one log line.

    Each column is ``{label?, entity_id?, attribute?, unit?}``:
      - no entity_id            → an attribute of the tracked (anchor) entity
      - entity_id + attribute   → that attribute of the companion entity
      - entity_id, no attribute → the companion entity's state

    Columns whose source is missing/unavailable/unknown/empty are skipped (same
    guard as ``_build_board_tiles``). When ``hass`` is None, companion-entity
    columns are skipped (their values can't be read); bare-attribute columns of the
    tracked entity still resolve. Each rendered column is the raw value capped at
    LOG_COLUMN_VALUE_MAX, with ``unit`` appended as a literal suffix (no
    conversion) and prefixed ``Label: `` when a label is set.
    """
    out: list[str] = []
    for column in config.get(CONF_LOG_COLUMNS) or []:
        if not isinstance(column, dict):
            continue
        entity_id = column.get(CONF_ENTITY_ID)
        attr = column.get("attribute")
        if entity_id:
            if hass is None:
                continue
            source = hass.states.get(entity_id)
            if source is None or source.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            raw = source.attributes.get(attr) if attr else source.state
        else:
            if not attr:
                continue
            raw = state.attributes.get(attr)
        if raw is None or str(raw) == "":
            continue
        rendered = str(raw)[:LOG_COLUMN_VALUE_MAX]
        unit = column.get(CONF_UNIT)
        if unit:
            rendered = f"{rendered}{unit}"
        label = str(column.get(CONF_LABEL, "") or "").strip()
        if label:
            rendered = f"{label[:LOG_COLUMN_LABEL_MAX]}: {rendered}"
        out.append(rendered)
    return out


def _build_log_line(state: State, entity_config: dict, hass: HomeAssistant | None = None) -> dict:
    """Build a single LogLine dict from an entity state for the log template.

    ``text`` is the formatted state label, optionally followed by ``" · "``-joined
    extra columns (CONF_LOG_COLUMNS) composed from the tracked entity's attributes
    and/or other entities (whole text capped at LOG_LINE_TEXT_MAX). When every
    column resolves empty the text is just the state label (never a blank line).
    ``at`` is the state's last_updated epoch (omitted when not resolvable);
    ``level`` comes from CONF_LOG_LEVEL_ATTRIBUTE when it resolves to a valid
    info/warn/error tag. ``hass`` is required only for columns sourced from other
    entities; bare-attribute columns resolve without it (keeps 2-arg test calls).
    """
    text = _format_state_label(state, entity_config)
    columns = _resolve_log_columns(state, entity_config, hass)
    if columns:
        text = " · ".join([text, *columns])
    line: dict = {"text": text[:LOG_LINE_TEXT_MAX]}
    at = _state_epoch(state)
    if at is not None and at > 0:
        line["at"] = at
    level_attr = entity_config.get(CONF_LOG_LEVEL_ATTRIBUTE)
    if level_attr:
        raw_level = state.attributes.get(level_attr)
        if raw_level is not None:
            level = str(raw_level).strip().lower()
            if level in LOG_LEVELS:
                line["level"] = level
    return line


def _build_board_tiles(entity_config: dict, hass: HomeAssistant | None) -> list[dict]:
    """Build the board ``tiles`` list by reading each tile's bound entity.

    Each configured tile is ``{label, entity_id, value_attribute?, unit?, icon?}``.
    Tiles whose entity is missing/unavailable or whose value is empty are skipped
    (mirrors the stat_list widget mapper). Returns at most BOARD_MAX_TILES tiles;
    an empty list when ``hass`` is unavailable (the values can't be read).
    """
    if hass is None:
        return []
    tiles_out: list[dict] = []
    for tile in entity_config.get(CONF_TILES) or []:
        if not isinstance(tile, dict):
            continue
        entity_id = tile.get(CONF_ENTITY_ID)
        label = str(tile.get(CONF_LABEL, "") or "").strip()
        if not entity_id or not label:
            continue
        tile_state = hass.states.get(entity_id)
        if tile_state is None or tile_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue
        attr = tile.get(CONF_VALUE_ATTRIBUTE)
        raw = tile_state.attributes.get(attr) if attr else tile_state.state
        if raw is None or str(raw) == "":
            continue
        out: dict = {
            "label": label[:BOARD_TILE_LABEL_MAX],
            "value": str(raw)[:BOARD_TILE_VALUE_MAX],
        }
        unit = tile.get(CONF_UNIT)
        if unit:
            out["unit"] = str(unit)[:BOARD_TILE_UNIT_MAX]
        icon = resolve_icon(tile_state, tile, registry_icon=lookup_registry_icon(hass, entity_id))
        if icon:
            out["icon"] = icon[:BOARD_TILE_ICON_MAX]
        color = color_to_str(tile.get(CONF_ACCENT_COLOR, "") or "")
        if color:
            out["color"] = color
        trend = tile.get("trend")
        if trend in BOARD_TRENDS:
            out["trend"] = trend
        tiles_out.append(out)
        if len(tiles_out) >= BOARD_MAX_TILES:
            break
    return tiles_out


def map_content(
    state: State,
    entity_config: dict,
    *,
    registry_icon: str | None = None,
    hass: HomeAssistant | None = None,
) -> dict:
    """Map HA state + attributes to a PushWard content dict.

    ``hass`` is required only when a value field is configured to read from a
    separate companion entity (CONF_*_ENTITY); without it those fields fall back
    to the tracked entity.
    """
    # State label: use custom label if configured, else default formatting
    state_text = _format_state_label(state, entity_config)

    icon = resolve_icon(state, entity_config, registry_icon=registry_icon)

    # Subtitle: subtitle_entity/subtitle_attribute > friendly_name
    raw_subtitle, _ = _resolve_raw(state, entity_config, CONF_SUBTITLE_ENTITY, CONF_SUBTITLE_ATTRIBUTE, hass)
    subtitle = str(raw_subtitle) if raw_subtitle is not _NO_VALUE else state.attributes.get("friendly_name", "")

    # Accent color resolution: accent_color_attribute > static accent_color > "blue"
    accent = resolve_color(state, entity_config, CONF_ACCENT_COLOR, CONF_ACCENT_COLOR_ATTRIBUTE) or "blue"

    background_color = resolve_color(state, entity_config, CONF_BACKGROUND_COLOR, CONF_BACKGROUND_COLOR_ATTRIBUTE)
    text_color = resolve_color(state, entity_config, CONF_TEXT_COLOR, CONF_TEXT_COLOR_ATTRIBUTE)

    content: dict = {
        "template": entity_config.get(CONF_TEMPLATE, "generic"),
        "progress": _get_progress(state, entity_config, hass),
        "state": state_text,
        "icon": icon,
        "subtitle": subtitle,
        "accent_color": accent,
    }

    if background_color:
        content["background_color"] = background_color
    if text_color:
        content["text_color"] = text_color

    # Single clock read shared by remaining_time and the countdown start/end_date
    # below, so the emitted fields stay mutually consistent.
    now = int(time.time())
    remaining, absolute_end = _get_remaining_seconds(state, entity_config, hass, now=now)
    if remaining is not None:
        content["remaining_time"] = remaining

    _add_tap_actions(content, entity_config)

    # Template-specific required fields
    template = content["template"]
    if template == "countdown":
        # A timestamp (finish-time) source anchors end_date to the absolute finish
        # time, so it doesn't drift as the clock ticks. Otherwise derive it from
        # remaining seconds against the shared ``now`` read above.
        if absolute_end is not None:
            content["end_date"] = absolute_end
        else:
            content["end_date"] = now + (remaining if remaining is not None else 0)
        completion_msg = entity_config.get(CONF_COMPLETION_MESSAGE)
        if completion_msg:
            content["completion_message"] = completion_msg
        if remaining is not None:
            content["start_date"] = now
        warning_threshold = entity_config.get(CONF_WARNING_THRESHOLD)
        if warning_threshold is not None:
            content["warning_threshold"] = int(warning_threshold)
        if entity_config.get(CONF_ALARM):
            content["alarm"] = True
            snooze_seconds = entity_config.get(CONF_SNOOZE_SECONDS)
            if snooze_seconds is not None:
                content["snooze_seconds"] = int(snooze_seconds)
    elif template == "steps":
        total = entity_config.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)
        current = _get_current_step(state, entity_config, hass)
        content["total_steps"] = total
        content["current_step"] = current
        # Auto-derive progress when no explicit progress source is configured
        if not entity_config.get(CONF_PROGRESS_ATTRIBUTE) and not entity_config.get(CONF_PROGRESS_ENTITY) and total > 0:
            content["progress"] = max(0.0, min(1.0, current / total))
        step_labels = entity_config.get(CONF_STEP_LABELS) or {}
        if step_labels:
            labels_list = [step_labels.get(str(i), "") for i in range(1, total + 1)]
            if any(labels_list):
                content["step_labels"] = labels_list
        step_rows = entity_config.get(CONF_STEP_ROWS) or []
        if len(step_rows) == total:
            content["step_rows"] = [max(1, min(10, int(r))) for r in step_rows]
    elif template == "alert":
        content["severity"] = entity_config.get(CONF_SEVERITY, DEFAULT_SEVERITY)
        if label := entity_config.get(CONF_SEVERITY_LABEL):
            content["severity_label"] = label
        raw_fired_at, _ = _resolve_raw(state, entity_config, CONF_FIRED_AT_ENTITY, CONF_FIRED_AT_ATTRIBUTE, hass)
        if raw_fired_at is not _NO_VALUE:
            fired_at = _coerce_epoch(raw_fired_at)
            if fired_at is not None:
                content["fired_at"] = fired_at
            else:
                _LOGGER.debug("Could not parse fired_at for %s", state.entity_id)
    elif template == "gauge":
        min_val, max_val = _gauge_base_fields(content, entity_config)
        value = _get_gauge_value(state, entity_config, hass)
        value = max(min_val, min(max_val, value))
        content["value"] = value
        if max_val > min_val:
            content["progress"] = (value - min_val) / (max_val - min_val)
        else:
            content["progress"] = 1.0
    elif template == "timeline":
        values = _get_timeline_values(state, entity_config, hass)
        if values:
            content["value"] = values
        unit = entity_config.get(CONF_UNIT, "")
        if unit:
            content["unit"] = unit
        scale = entity_config.get(CONF_SCALE, "")
        if scale and scale != "linear":
            content["scale"] = scale
        decimals = entity_config.get(CONF_DECIMALS)
        if decimals is not None and decimals != DEFAULT_DECIMALS:
            content["decimals"] = decimals
        smoothing = entity_config.get(CONF_SMOOTHING)
        if smoothing:
            content["smoothing"] = smoothing
        thresholds = entity_config.get(CONF_THRESHOLDS, [])
        if thresholds:
            content["thresholds"] = thresholds
        units = _get_timeline_units(entity_config, values, hass)
        if units:
            content["units"] = units
        content["progress"] = 0.0
    elif template == "board":
        tiles = _build_board_tiles(entity_config, hass)
        if tiles:
            content["tiles"] = tiles
        # Board has no progress bar — the server requires the field in [0,1].
        content["progress"] = 0.0
    elif template == "log":
        # The current state is one log line; the manager overrides this with the
        # full ring buffer (newest-first) when one has accumulated.
        content["lines"] = [_build_log_line(state, entity_config, hass)]
        content["progress"] = 0.0

    return content


def map_completion_content(entity_config: dict, last_content: dict | None = None) -> dict:
    """Build content for the "Complete" phase of two-phase end.

    Preserves progress and subtitle from the last live update so the end
    screen reflects the actual value (e.g. lamp brightness) rather than
    jumping to 100%.
    """
    completion_msg = entity_config.get(CONF_COMPLETION_MESSAGE) or "Complete"

    content: dict = {
        "template": entity_config.get(CONF_TEMPLATE, "generic"),
        "progress": last_content.get("progress", 1.0) if last_content else 1.0,
        "state": completion_msg,
        "icon": "checkmark.circle.fill",
        "subtitle": last_content.get("subtitle", "") if last_content else "",
        "accent_color": "green",
    }

    _add_tap_actions(content, entity_config)

    # Template-specific required fields for server validation
    template = content["template"]
    if template == "countdown":
        content["end_date"] = int(time.time())
    elif template == "steps":
        total = entity_config.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)
        content["total_steps"] = total
        content["current_step"] = total
        content["progress"] = 1.0
    elif template == "alert":
        content["severity"] = entity_config.get(CONF_SEVERITY, DEFAULT_SEVERITY)
        if label := entity_config.get(CONF_SEVERITY_LABEL):
            content["severity_label"] = label
    elif template == "gauge":
        _, max_val = _gauge_base_fields(content, entity_config)
        if last_content and "value" in last_content:
            content["value"] = last_content["value"]
        else:
            content["value"] = max_val
            content["progress"] = 1.0
    elif template == "timeline":
        if last_content and "value" in last_content:
            content["value"] = last_content["value"]
        for key in _TIMELINE_CARRY_FIELDS:
            if last_content and key in last_content:
                content[key] = last_content[key]
    elif template == "board" and last_content and last_content.get("tiles"):
        # The server requires ≥1 tile even on the ENDED frame — carry the last
        # rendered tiles so the completion screen keeps the final values.
        content["tiles"] = last_content["tiles"]
    elif template == "log" and last_content and last_content.get("lines"):
        # The server requires ≥1 line even on the ENDED frame — carry the last
        # rendered lines (newest-first) so the log doesn't blank out on completion.
        content["lines"] = last_content["lines"]

    if last_content:
        for key in _COMMON_CARRY_FIELDS:
            if key in last_content:
                content[key] = last_content[key]

    return content


def _gauge_base_fields(content: dict, entity_config: dict) -> tuple[float, float]:
    """Set shared gauge fields (min_value, max_value, unit) on content and return the range."""
    min_val = entity_config.get(CONF_MIN_VALUE, DEFAULT_MIN_VALUE)
    max_val = entity_config.get(CONF_MAX_VALUE, DEFAULT_MAX_VALUE)
    content["min_value"] = min_val
    content["max_value"] = max_val
    unit = entity_config.get(CONF_UNIT, "")
    if unit:
        content["unit"] = unit
    return min_val, max_val


def get_domain_defaults(domain: str) -> dict:
    """Return default icon, start_states, and end_states for an HA domain."""
    return DOMAIN_DEFAULTS.get(
        domain,
        {"icon": "mdi:eye", "start_states": [], "end_states": []},
    )


_ATTRS_0_255 = frozenset({"brightness"})


def _rescale_attr(value: float, attr_name: str) -> float:
    """Rescale 0-255 attributes (e.g. brightness) to 0-100."""
    if attr_name in _ATTRS_0_255:
        return round(value / 255.0 * 100.0)
    return value


# Sentinel returned when a field has no resolvable value (unconfigured, or a
# configured companion entity is unavailable). Callers apply the field default.
_NO_VALUE: object = object()


def _resolve_source(
    primary_state: State,
    entity_config: dict,
    entity_key: str,
    hass: HomeAssistant | None,
) -> State | object:
    """Resolve the State a value should be read from for ``entity_key``.

    Returns the tracked (primary) State when no companion entity is configured.
    When a companion IS configured, returns that entity's State, or _NO_VALUE if
    it is missing/unavailable/unknown (so callers fall back to the field default
    rather than silently reading the primary). Also returns _NO_VALUE when a
    companion is configured but ``hass`` is unavailable, to avoid silently
    sourcing the value from the wrong (primary) entity.
    """
    companion_id = entity_config.get(entity_key)
    if not companion_id:
        return primary_state
    if hass is None:
        return _NO_VALUE
    companion = hass.states.get(companion_id)
    if companion is None or companion.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return _NO_VALUE
    return companion


def _resolve_raw(
    primary_state: State,
    entity_config: dict,
    entity_key: str,
    attr_key: str,
    hass: HomeAssistant | None,
    *,
    state_fallback: bool = False,
) -> tuple[object, State | None]:
    """Resolve the raw value for a field, honoring an optional companion entity.

    Returns ``(raw, source_state)``. ``raw`` is _NO_VALUE when nothing is
    configured/available. Precedence:
      - companion entity set + attribute set  -> companion.attributes[attr]
      - companion entity set + attribute empty -> companion.state
      - companion empty + attribute set        -> primary.attributes[attr]
      - companion empty + attribute empty      -> primary.state if
        ``state_fallback`` else _NO_VALUE
    ``source_state`` exposes the chosen entity (for device_class/unit lookups).
    """
    attr = entity_config.get(attr_key)
    if entity_config.get(entity_key):
        source = _resolve_source(primary_state, entity_config, entity_key, hass)
        if source is _NO_VALUE:
            return _NO_VALUE, None
        raw = source.attributes.get(attr) if attr else source.state
        return (raw if raw is not None else _NO_VALUE), source
    if attr:
        raw = primary_state.attributes.get(attr)
        return (raw if raw is not None else _NO_VALUE), primary_state
    if state_fallback:
        return primary_state.state, primary_state
    return _NO_VALUE, primary_state


def _parse_clock_string(raw: str) -> int | None:
    """Parse an 'H:MM:SS' / 'MM:SS' (or any HA duration) string into clamped seconds.

    Delegates to ``dt_util.parse_duration``, which rejects malformed input
    (returns None) instead of raising, and handles sign/format edge cases.
    """
    delta = dt_util.parse_duration(raw.strip())
    if delta is None:
        return None
    return max(0, int(delta.total_seconds()))


def _coerce_epoch(raw: object) -> int | None:
    """Coerce a raw value to a unix epoch int: numeric seconds, or an ISO datetime."""
    try:
        return int(float(raw))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        parsed = dt_util.parse_datetime(str(raw))
        return int(parsed.timestamp()) if parsed is not None else None


def _coerce_remaining_seconds(
    raw: object, source: State | None, now: int | None = None
) -> tuple[int | None, int | None]:
    """Coerce a raw value into ``(remaining_seconds, absolute_end_epoch)``.

    Smart-parses several appliance time formats:
      - device_class 'timestamp' (ISO finish time) -> absolute end + derived remaining
      - device_class 'duration' (with unit_of_measurement) -> seconds
      - 'H:MM:SS' / 'MM:SS' string -> seconds
      - plain number -> seconds
    ``absolute_end_epoch`` is non-None only for a timestamp source. ``now`` lets
    the caller share a single clock read across the emitted fields.
    """
    device_class = source.attributes.get("device_class") if source is not None else None

    if device_class == "timestamp":
        parsed = dt_util.parse_datetime(str(raw))
        if parsed is None:
            return None, None
        end = int(parsed.timestamp())
        if now is None:
            now = int(time.time())
        return max(0, end - now), end

    if isinstance(raw, str) and ":" in raw:
        secs = _parse_clock_string(raw)
        if secs is None and source is not None:
            _LOGGER.debug("Could not parse remaining time clock string for %s", source.entity_id)
        return secs, None

    try:
        value = float(raw)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None, None

    unit = source.attributes.get("unit_of_measurement") if source is not None else None
    if device_class == "duration" and unit:
        # Unknown/unsupported unit → treat the raw number as seconds.
        with contextlib.suppress(HomeAssistantError):
            value = DurationConverter.convert(value, str(unit), UnitOfTime.SECONDS)
    return int(value), None


def _get_progress(state: State, entity_config: dict, hass: HomeAssistant | None = None) -> float:
    """Extract progress (0.0-1.0) from a companion entity or the tracked entity.

    Attributes in the 0-255 range (e.g. brightness) are divided by 255;
    all others are treated as 0-100 percentages.
    """
    raw, _source = _resolve_raw(state, entity_config, CONF_PROGRESS_ENTITY, CONF_PROGRESS_ATTRIBUTE, hass)
    if raw is _NO_VALUE:
        return 0.0
    attr_name = entity_config.get(CONF_PROGRESS_ATTRIBUTE)
    try:
        value = float(raw)  # type: ignore[arg-type]
        scale = 255.0 if attr_name in _ATTRS_0_255 else 100.0
        return round(max(0.0, min(1.0, value / scale)), 2)
    except (ValueError, TypeError):
        _LOGGER.debug("Could not parse progress for %s", state.entity_id)
        return 0.0


def _get_current_step(state: State, entity_config: dict, hass: HomeAssistant | None = None) -> int:
    """Extract current step from a companion entity or the tracked entity, clamped to 0..total."""
    total = entity_config.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)
    raw, _source = _resolve_raw(state, entity_config, CONF_CURRENT_STEP_ENTITY, CONF_CURRENT_STEP_ATTR, hass)
    if raw is _NO_VALUE:
        return 0
    try:
        value = int(float(raw))  # type: ignore[arg-type]
        return max(0, min(total, value))
    except (ValueError, TypeError):
        _LOGGER.debug("Could not parse current_step for %s", state.entity_id)
        return 0


def _get_remaining_seconds(
    state: State, entity_config: dict, hass: HomeAssistant | None = None, *, now: int | None = None
) -> tuple[int | None, int | None]:
    """Resolve remaining time as ``(remaining_seconds, absolute_end_epoch)``.

    Reads from a companion entity (CONF_REMAINING_TIME_ENTITY) or an attribute of
    the tracked entity (CONF_REMAINING_TIME_ATTR), with smart format parsing.
    """
    raw, source = _resolve_raw(state, entity_config, CONF_REMAINING_TIME_ENTITY, CONF_REMAINING_TIME_ATTR, hass)
    if raw is _NO_VALUE:
        return None, None
    return _coerce_remaining_seconds(raw, source, now)


def _coerce_scaled_value(raw: object, attr_name: str | None) -> float | None:
    """Coerce a raw value to float, rescaling 0-255 attributes to 0-100. None if unparseable."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
    return _rescale_attr(value, attr_name) if attr_name else value


def _get_timeline_values(state: State, entity_config: dict, hass: HomeAssistant | None = None) -> dict[str, float]:
    """Extract the labeled value map for a timeline template.

    Series come from two sources that can be combined: CONF_SERIES maps attributes
    of the tracked entity to labels, and CONF_SERIES_ENTITIES binds separate
    entities (each an entity's state, or one of its attributes) as named lines.
    When neither is configured it falls back to a single series read from
    CONF_VALUE_ENTITY/CONF_VALUE_ATTRIBUTE (or the tracked entity's state),
    labelled with the tracked entity's friendly_name. Non-numeric or unavailable
    sources are skipped, dropping only that series' key.
    """
    values: dict[str, float] = {}

    for attr_name, label in (entity_config.get(CONF_SERIES) or {}).items():
        raw = state.attributes.get(attr_name)
        if raw is None:
            continue
        coerced = _coerce_scaled_value(raw, attr_name)
        if coerced is None:
            _LOGGER.debug("Could not parse timeline series attribute %s for %s", attr_name, state.entity_id)
            continue
        values[label] = coerced

    for series in entity_config.get(CONF_SERIES_ENTITIES) or []:
        if not isinstance(series, dict):
            continue
        label = series.get(CONF_LABEL)
        entity_id = series.get(CONF_ENTITY_ID)
        if not label or not entity_id or hass is None:
            continue
        source = hass.states.get(entity_id)
        if source is None or source.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue
        attr = series.get("attribute")
        raw = source.attributes.get(attr) if attr else source.state
        if raw is None:
            continue
        coerced = _coerce_scaled_value(raw, attr)
        if coerced is None:
            _LOGGER.debug("Could not parse timeline series entity %s for %s", entity_id, state.entity_id)
            continue
        values[label] = coerced

    if entity_config.get(CONF_SERIES) or entity_config.get(CONF_SERIES_ENTITIES):
        return values

    # Single series: keep the label anchored to the tracked entity so samples
    # from a companion value entity land in the same series.
    label = state.attributes.get("friendly_name", state.entity_id)
    raw, _source = _resolve_raw(
        state, entity_config, CONF_VALUE_ENTITY, CONF_VALUE_ATTRIBUTE, hass, state_fallback=True
    )
    if raw is _NO_VALUE:
        return {}
    value = _coerce_scaled_value(raw, entity_config.get(CONF_VALUE_ATTRIBUTE))
    if value is None:
        _LOGGER.debug("Could not parse timeline value for %s", state.entity_id)
        return {}
    return {label: value}


def _get_timeline_units(
    entity_config: dict, values: dict[str, float], hass: HomeAssistant | None = None
) -> dict[str, str]:
    """Build the per-series unit map for a timeline.

    Auto-defaults each state-sourced series entity's unit from its source entity's
    ``unit_of_measurement`` (an attribute value has no standalone unit, so
    attribute-sourced series are skipped), overlays the explicit CONF_UNITS map,
    then keeps only labels present in ``values`` (the server requires the units
    keys to be a subset of the value keys).
    """
    units: dict[str, str] = {}
    if hass is not None:
        for series in entity_config.get(CONF_SERIES_ENTITIES) or []:
            if not isinstance(series, dict) or series.get("attribute"):
                continue
            label = series.get(CONF_LABEL)
            entity_id = series.get(CONF_ENTITY_ID)
            if not label or not entity_id:
                continue
            source = hass.states.get(entity_id)
            if source is None:
                continue
            uom = source.attributes.get("unit_of_measurement")
            if uom:
                units[label] = str(uom)
    units.update(entity_config.get(CONF_UNITS) or {})
    return {label: unit for label, unit in units.items() if label in values}


def _timeline_recorder_sources(state: State, entity_config: dict) -> dict[str, str]:
    """Map each recorder-eligible timeline series label to its source entity_id.

    Only state-sourced series can be seeded from the recorder: series entities
    read as a state (no attribute), and the single-series fallback (the value
    entity when set, else the tracked entity, and only when it reads a state).
    Attribute-sourced series are excluded because HA 2024.8+ strips attributes
    from the recorder (those seed from the live ring buffer instead). The labels
    match ``_get_timeline_values`` so seeded points join the live series.
    """
    sources: dict[str, str] = {}
    series_entities = entity_config.get(CONF_SERIES_ENTITIES) or []
    for series in series_entities:
        if not isinstance(series, dict) or series.get("attribute"):
            continue
        label = series.get(CONF_LABEL)
        entity_id = series.get(CONF_ENTITY_ID)
        if label and entity_id:
            sources[label] = entity_id

    if not entity_config.get(CONF_SERIES) and not series_entities and not entity_config.get(CONF_VALUE_ATTRIBUTE):
        label = state.attributes.get("friendly_name", state.entity_id)
        sources[label] = entity_config.get(CONF_VALUE_ENTITY) or state.entity_id

    return sources


def _get_gauge_value(state: State, entity_config: dict, hass: HomeAssistant | None = None) -> float:
    """Extract gauge value from a companion entity or the tracked entity.

    Reads CONF_VALUE_ENTITY/CONF_VALUE_ATTRIBUTE, falling back to the tracked
    entity's state. Attributes in the 0-255 range (e.g. brightness) are rescaled
    to 0-100.
    """
    raw, _source = _resolve_raw(
        state, entity_config, CONF_VALUE_ENTITY, CONF_VALUE_ATTRIBUTE, hass, state_fallback=True
    )
    if raw is _NO_VALUE:
        return 0.0
    value = _coerce_scaled_value(raw, entity_config.get(CONF_VALUE_ATTRIBUTE))
    if value is None:
        _LOGGER.debug("Could not parse gauge value for %s", state.entity_id)
        return 0.0
    return value
