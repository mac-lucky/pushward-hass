"""Map Home Assistant state/attributes to PushWard content."""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTime
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.color import (
    color_hs_to_RGB,
    color_temperature_to_rgb,
    color_xy_to_RGB,
)
from homeassistant.util.unit_conversion import DurationConverter

from .const import (
    APPROVAL_DETAIL_LABEL_MAX,
    APPROVAL_DETAIL_VALUE_MAX,
    APPROVAL_DETAILS_MAX,
    APPROVAL_OPTION_ID_PATTERN,
    APPROVAL_OPTION_STYLES,
    APPROVAL_OPTION_TITLE_MAX,
    APPROVAL_OPTIONS_MAX,
    APPROVAL_OPTIONS_MIN,
    APPROVAL_SOURCE_MAX,
    BOARD_MAX_TILES,
    BOARD_TILE_ICON_MAX,
    BOARD_TILE_LABEL_MAX,
    BOARD_TILE_UNIT_MAX,
    BOARD_TILE_VALUE_MAX,
    BOARD_TRENDS,
    CHECKLIST_ITEM_ID_PATTERN,
    CHECKLIST_ITEM_LABEL_MAX,
    CHECKLIST_ITEMS_MAX,
    COMPACT_TRAILING_ICON_PATTERN,
    CONF_ACCENT_COLOR,
    CONF_ACCENT_COLOR_ATTRIBUTE,
    CONF_ACCENT_FROM_PROGRESS,
    CONF_ALARM,
    CONF_APPROVAL_DETAILS,
    CONF_APPROVAL_OPTIONS,
    CONF_APPROVAL_SOURCE,
    CONF_BACKGROUND_COLOR,
    CONF_BACKGROUND_COLOR_ATTRIBUTE,
    CONF_CALENDAR_LEAD_MINUTES,
    CONF_CALENDAR_SUBTITLE_BEFORE,
    CONF_CALENDAR_SUBTITLE_DURING,
    CONF_CHECKLIST_ITEMS,
    CONF_COMPACT_LABEL,
    CONF_COMPACT_TRAILING_ICON,
    CONF_COMPACT_TRAILING_LABEL,
    CONF_COMPLETION_MESSAGE,
    CONF_CURRENT_STEP_ATTR,
    CONF_CURRENT_STEP_ENTITY,
    CONF_DECIMALS,
    CONF_ENTITY_ID,
    CONF_FIRED_AT_ATTRIBUTE,
    CONF_FIRED_AT_ENTITY,
    CONF_ICON,
    CONF_ICON_ATTRIBUTE,
    CONF_ICON_MAP,
    CONF_ICON_MAP_ENTITY,
    CONF_IMAGE_SHAPE,
    CONF_IMAGE_THUMBHASH,
    CONF_IMAGE_URL,
    CONF_LABEL,
    CONF_LIVE_PROGRESS,
    CONF_LOG_COLUMNS,
    CONF_LOG_LEVEL_ATTRIBUTE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_PRIMARY_SERIES,
    CONF_PROGRESS_ATTRIBUTE,
    CONF_PROGRESS_ENTITY,
    CONF_PROGRESS_MODE,
    CONF_REMAINING_TIME_ATTR,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SCALE,
    CONF_SECONDARY_URL,
    CONF_SECONDARY_URL_FOREGROUND,
    CONF_SECONDARY_URL_TITLE,
    CONF_SERIES,
    CONF_SERIES_ATTRIBUTE,
    CONF_SERIES_ENTITIES,
    CONF_SEVERITY,
    CONF_SEVERITY_LABEL,
    CONF_SMOOTHING,
    CONF_SNOOZE_SECONDS,
    CONF_STATE_LABELS,
    CONF_STATE_TEXT_ATTRIBUTE,
    CONF_STATE_TEXT_ENTITY,
    CONF_STATE_TEXT_FORMAT,
    CONF_STATE_TEXT_PATTERN,
    CONF_STEP_COLORS,
    CONF_STEP_LABELS,
    CONF_STEP_ROWS,
    CONF_STEP_WEIGHTS,
    CONF_SUBTITLE_ATTRIBUTE,
    CONF_SUBTITLE_COLUMNS,
    CONF_SUBTITLE_ENTITY,
    CONF_TAP_ACTION_FOREGROUND,
    CONF_TAP_ACTION_URL,
    CONF_TEMPLATE,
    CONF_TEXT_COLOR,
    CONF_TEXT_COLOR_ATTRIBUTE,
    CONF_THRESHOLDS,
    CONF_TILE_COLOR,
    CONF_TILE_URL_ACTION,
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
    DEFAULT_CALENDAR_LEAD_MINUTES,
    DEFAULT_DECIMALS,
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    DEFAULT_SEVERITY,
    DEFAULT_TAP_ACTION_FOREGROUND,
    DEFAULT_TOTAL_STEPS,
    DEVICE_CLASS_ICONS,
    DOMAIN_DEFAULTS,
    IMAGE_SHAPES,
    IMAGE_TEMPLATES,
    LIVE_PROGRESS_TEMPLATES,
    LOG_COLUMN_LABEL_MAX,
    LOG_COLUMN_VALUE_MAX,
    LOG_LEVELS,
    LOG_LINE_TEXT_MAX,
    MAX_COMPACT_LABEL_LEN,
    MAX_COMPACT_TRAILING_ICON_LEN,
    MAX_COMPACT_TRAILING_LABEL_LEN,
    MAX_SEVERITY_LABEL_LEN,
    MAX_TAP_ACTION_ICON_LEN,
    MAX_TEXT_LEN,
    MEDIA_DURATION_MAX,
    MEDIA_POSITION_MAX_AGE,
    MEDIA_TITLE_MAX,
    NAMED_COLORS,
    PROGRESS_MODE_REMAINING,
    STATE_TEXT_PLACEHOLDER,
    TAP_ACTION_METHODS,
    TIMELINE_SERIES_LABEL_MAX,
    is_valid_image_url,
    is_valid_thumbhash,
    normalize_slug,
    url_host,
)
from .media_control import EMITTED_CONTROL_SLOTS, control_urls

_LOGGER = logging.getLogger(__name__)

# Timeline display fields carried forward to completion content.
# Keep in sync with the fields emitted in map_content's timeline branch.
_TIMELINE_CARRY_FIELDS = ("unit", "scale", "decimals", "smoothing", "thresholds", "units")

# Common (all-template) fields carried from last_content into completion.
_COMMON_CARRY_FIELDS = ("background_color", "text_color")

# Mirrors server-side ValidateColor: 6- or 8-digit hex with optional '#', or
# one of the server's allowlisted named colors (NAMED_COLORS, defined in const).
_COLOR_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")
_COLOR_NAMED = frozenset(NAMED_COLORS)


def is_valid_color(value: str) -> bool:
    """Return True if value is a color the server accepts.

    Same rule as ValidateColor: an allowlisted named color or 6/8-digit hex
    (optional leading '#'). 3-digit '#rgb' is not accepted - the server rejects
    it too. Empty/non-string is False.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    return bool(_COLOR_HEX_RE.match(stripped)) or stripped.lower() in _COLOR_NAMED


def sanitize_slug(entity_id: str) -> str:
    """Convert an HA entity_id to a PushWard slug.

    sensor.washing_machine_status -> ha-sensor-washing-machine-status
    """
    return normalize_slug(f"ha-{entity_id}")


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


def _add_button_actions(content: dict, config: dict) -> None:
    """Attach url_action / secondary_url_action to content from the CONF_URL* keys.

    The button slots have the same wire shape on both surfaces; the caller owns the
    question of whether the surface renders them.
    """
    for slot_key, url_key, foreground_key, title_key in _BUTTON_SLOTS:
        action = build_tap_action(
            config.get(url_key, ""),
            config.get(foreground_key, DEFAULT_TAP_ACTION_FOREGROUND),
            config.get(title_key, ""),
        )
        if action is not None:
            content[slot_key] = action


def add_all_tap_actions(content: dict, config: dict) -> None:
    """Attach the whole widget action set: tap_action plus both button slots.

    This is the widget policy in one call - all three slots ride every widget
    template, because the server accepts them everywhere. The activity policy is
    deliberately a different function (`_add_activity_actions`): it gates the two
    button slots on the steps/alert templates that draw a button row.
    """
    add_tap_action(content, config)
    _add_button_actions(content, config)


def _add_activity_actions(content: dict, entity_config: dict) -> None:
    """Add structured tap_action / url_action / secondary_url_action to content.

    tap_action is universal (every template); url_action and secondary_url_action
    are emitted only for steps/alert templates that render button affordances.
    """
    add_tap_action(content, entity_config)

    if entity_config.get(CONF_TEMPLATE, "generic") not in ("steps", "alert"):
        return

    _add_button_actions(content, entity_config)


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


def _mapped_icon(state: State, config: dict, hass: HomeAssistant | None) -> str:
    """The CONF_ICON_MAP icon for the current state of the tracked entity or CONF_ICON_MAP_ENTITY, else ""."""
    icon_map = config.get(CONF_ICON_MAP)
    if not icon_map or not isinstance(icon_map, dict):
        return ""
    source_id = config.get(CONF_ICON_MAP_ENTITY)
    source = state
    if source_id and source_id != state.entity_id:
        source = hass.states.get(source_id) if hass is not None else None
        if source is None:
            return ""
    return str(icon_map.get(source.state) or "")


def resolve_icon(
    state: State, config: dict, registry_icon: str | None = None, hass: HomeAssistant | None = None
) -> str:
    """Resolve an entity's display icon via a 7-level fallback chain.

    Order: icon_map (an icon per state of the tracked or a companion entity;
    the companion needs ``hass``) -> icon_attribute (dynamic from HA attribute) ->
    static CONF_ICON (user-configured) -> state.attributes["icon"] (legacy /
    _attr_icon) -> entity registry icon -> device_class icon (mirrors HA frontend
    tables) -> domain default. Returns "" only when every step yields nothing,
    which is rare given the domain-default fallback.
    """
    icon = _mapped_icon(state, config, hass)
    icon_attr = config.get(CONF_ICON_ATTRIBUTE)
    if not icon and icon_attr:
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


def _state_at_display_precision(hass: HomeAssistant, entity_id: str, raw: str) -> str:
    """Round a numeric sensor state the way HA's own UI shows it.

    The state string carries the device's full precision ("23.06"); HA displays it
    at the user's display_precision, else the integration's suggested one. A
    non-numeric state, or an entity with neither set, is returned unchanged.
    """
    try:
        entry = er.async_get(hass).async_get(entity_id)
    except (AttributeError, KeyError):
        return raw
    if entry is None:
        return raw
    opts = entry.options.get("sensor") or {}
    precision = opts.get("display_precision")
    if precision is None:
        precision = opts.get("suggested_display_precision")
    if not isinstance(precision, int) or isinstance(precision, bool):
        return raw
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return raw
    if not math.isfinite(value):
        return raw
    return f"{value:.{precision}f}"


def _finite_number(raw: object) -> float | None:
    """A finite float from a state or attribute value, or None (bools are not numbers here)."""
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _distance_text(metres: float) -> str:
    """'850 m' under a kilometre, else '3.2 km'."""
    rounded = round(metres)
    return f"{rounded} m" if abs(rounded) < 1000 else f"{metres / 1000:.1f} km"


def _duration_text(seconds: float) -> str:
    """'45 s', '15 min' or '1 h 5 min'."""
    secs = max(0, round(seconds))
    if secs < 60:
        return f"{secs} s"
    hours, minutes = divmod(round(secs / 60), 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _state_text(state: State, entity_config: dict, hass: HomeAssistant | None) -> str | None:
    """The configured state-text source as display text, or None when unset, missing or unavailable.

    Read like the subtitle source; ``state_text_format`` shapes the value and an
    optional ``state_text_pattern`` places it at ``{value}`` (plain replace).
    """
    entity_id = entity_config.get(CONF_STATE_TEXT_ENTITY)
    attr = entity_config.get(CONF_STATE_TEXT_ATTRIBUTE)
    if not entity_id and not attr:
        return None
    raw, source = _resolve_raw(state, entity_config, CONF_STATE_TEXT_ENTITY, CONF_STATE_TEXT_ATTRIBUTE, hass)
    if raw is _NO_VALUE or str(raw) == "":
        return None
    number = _finite_number(raw)
    fmt = entity_config.get(CONF_STATE_TEXT_FORMAT)
    if number is not None and fmt == "distance_m":
        text = _distance_text(number)
    elif number is not None and fmt in ("duration_s", "duration_min"):
        text = _duration_text(number * 60 if fmt == "duration_min" else number)
    elif number is not None and not attr and source is not None:
        # An entity's numeric state reads the way HA shows it: display precision plus unit.
        text = _state_at_display_precision(hass, source.entity_id, str(raw)) if hass is not None else str(raw)
        if unit := source.attributes.get("unit_of_measurement"):
            text = f"{text}{'' if unit == '%' else ' '}{unit}"
    else:
        text = str(raw)
    pattern = entity_config.get(CONF_STATE_TEXT_PATTERN) or ""
    if pattern:
        text = pattern.replace(STATE_TEXT_PLACEHOLDER, text)
    return text[:MAX_TEXT_LEN]


def progress_accent(progress: float) -> str:
    """Hex on a red -> yellow -> green gradient: 0 is #FF0000, 0.5 #FFFF00, 1 #00FF00."""
    p = max(0.0, min(1.0, progress))
    red, green = (255, round(510 * p)) if p <= 0.5 else (round(510 * (1 - p)), 255)
    return f"#{red:02X}{green:02X}00"


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


def _resolve_log_columns(
    state: State, config: dict, hass: HomeAssistant | None, key: str = CONF_LOG_COLUMNS
) -> list[str]:
    """Resolve CONF_LOG_COLUMNS (or the same-shaped rows under ``key``) into rendered column strings.

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
    for column in config.get(key) or []:
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

    Each configured tile is ``{label, entity_id, value_attribute?, unit?, icon?,
    color?, url_action?}``. ``color`` (named or hex) and ``url_action`` (a URL,
    wrapped into a foreground tap-action) are emitted only when configured. Tiles
    whose entity is missing/unavailable or whose value is empty are skipped
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
        color = color_to_str(tile.get(CONF_TILE_COLOR, "") or "")
        if color:
            out[CONF_TILE_COLOR] = color
        trend = tile.get("trend")
        if trend in BOARD_TRENDS:
            out["trend"] = trend
        url_action = build_tap_action(str(tile.get(CONF_TILE_URL_ACTION) or ""), foreground=True)
        if url_action is not None:
            out[CONF_TILE_URL_ACTION] = url_action
        tiles_out.append(out)
        if len(tiles_out) >= BOARD_MAX_TILES:
            break
    return tiles_out


_CHECKLIST_ITEM_ID_RE = re.compile(CHECKLIST_ITEM_ID_PATTERN)


def _build_checklist_items(entity_config: dict) -> list[dict]:
    """Build the checklist ``items`` list from the configured rows, the way board tiles are built.

    Lenient like the other row parsers: a row without a valid id or a label is
    skipped, a repeated id keeps its first row, and the list stops at the cap.
    ``checked`` goes out only when the row sets it, so the server keeps a tick
    made on a phone.
    """
    # Latent tick-wipe hazard, unreachable today: TEMPLATES does not offer checklist,
    # so no config flow writes CONF_CHECKLIST_ITEMS and only tests reach this. The
    # server lets an explicit `checked` override the stored tick, so a row that sets
    # it would be re-sent on every state change of the tracked entity and undo a tick
    # made on a phone. Before the flow offers checklist, keep `checked` off the rows
    # (or send it on the first frame only).
    items: list[dict] = []
    for raw in entity_config.get(CONF_CHECKLIST_ITEMS) or []:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or "").strip()
        label = str(raw.get("label") or "").strip()
        if not _CHECKLIST_ITEM_ID_RE.match(item_id) or not label or any(i["id"] == item_id for i in items):
            continue
        item = {"id": item_id, "label": label[:CHECKLIST_ITEM_LABEL_MAX]}
        if isinstance(raw.get("checked"), bool):
            item["checked"] = raw["checked"]
        items.append(item)
        if len(items) >= CHECKLIST_ITEMS_MAX:
            break
    return items


def _clean_step_weights(weights: list) -> list[float]:
    """Coerce configured step weights to what the server accepts, or [] to skip.

    The server takes step_weights only as positive finite numbers, so one bad
    entry drops the whole array rather than earning a 400. The bool guard is
    defensive: float(True) is 1.0, so a stray True would silently weigh 1.0.
    """
    cleaned: list[float] = []
    for w in weights:
        if isinstance(w, bool):
            return []
        try:
            value = float(w)
        except (TypeError, ValueError):
            return []
        if not math.isfinite(value) or value <= 0:
            return []
        cleaned.append(value)
    return cleaned


def _apply_steps_fields(
    content: dict,
    entity_config: dict,
    state: State,
    hass: HomeAssistant | None,
) -> None:
    """Write the steps template's fields into ``content`` in place.

    Everything but live_progress, which stays in map_content because it needs
    the shared clock read and remaining/end window computed there.
    """
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
    step_weights = entity_config.get(CONF_STEP_WEIGHTS) or []
    if len(step_weights) == total and (cleaned := _clean_step_weights(step_weights)):
        content["step_weights"] = cleaned
    step_colors = entity_config.get(CONF_STEP_COLORS) or []
    if len(step_colors) == total:
        # An unrecognized entry becomes "", which the server reads as
        # "use accent_color" rather than rejecting the whole push. All-empty
        # says nothing the accent doesn't already, so skip the bytes.
        colors_list = [color_to_str(c) for c in step_colors]
        if any(colors_list):
            content["step_colors"] = colors_list


# What the big line of the player card reads, in order of preference. A podcast or
# a show carries no media_title, a radio stream carries neither, and a player idling
# on an input carries only its source.
_MEDIA_TITLE_ATTRS = ("media_title", "media_series_title", "media_channel", "source")
# The line under it. Music gives an artist, a show gives the album/season field, and
# a casting device at least names the app.
_MEDIA_SUBTITLE_ATTRS = ("media_artist", "media_album_name", "app_name")
# HA state -> wire playback_state. Everything else (off, idle, standby, whatever a
# player invents) is stopped.
_PLAYBACK_STATES = {"playing": "playing", "paused": "paused", "buffering": "buffering"}


def _media_number(raw: object) -> float | None:
    """A finite float from a player attribute, or None.

    NaN and inf both survive float() and are both a 422 on the wire.
    """
    value = _coerce_scaled_value(raw, None)
    return value if value is not None and math.isfinite(value) else None


def _first_attr(state: State, names: tuple[str, ...]) -> str:
    """The first non-empty attribute of ``names``, as text."""
    for name in names:
        value = state.attributes.get(name)
        if value is not None and (text := str(value).strip()):
            return text
    return ""


def _media_position(state: State, now: int) -> tuple[float, int | None] | None:
    """The playhead sample as (position_seconds, position_at), or None.

    Never a bare anchor: a position_at without its position says nothing. The
    anchor half is None when the player's sample is missing or older than
    MEDIA_POSITION_MAX_AGE - the position then goes out ALONE, which makes the
    server stamp its receipt time AND clear any anchor inherited from an earlier
    frame. That last part is load-bearing: the server merge keeps a stored
    position_at until a patch overrides it, so omitting the pair here would let
    an old anchor age past the validator's floor and 422 every later push,
    the end frames included.
    """
    position = _media_number(state.attributes.get("media_position"))
    if position is None or position < 0:
        return None
    position = min(position, MEDIA_DURATION_MAX)
    raw_at = state.attributes.get("media_position_updated_at")
    if isinstance(raw_at, datetime):
        # A naive datetime reads as local time through .timestamp(), skewing the
        # anchor by the UTC offset. HA convention is UTC everywhere, so that is
        # what a naive value from a third-party integration is taken to mean.
        at = int((raw_at if raw_at.tzinfo else raw_at.replace(tzinfo=UTC)).timestamp())
    else:
        at = _coerce_epoch(raw_at)
    if at is None or at <= 0 or at < now - MEDIA_POSITION_MAX_AGE:
        return position, None
    # A player whose clock runs ahead would otherwise stamp the future, which the
    # server rejects as skew.
    return position, min(at, now)


def _apply_media_fields(
    content: dict,
    entity_config: dict,
    state: State,
    hass: HomeAssistant | None,
    *,
    now: int,
) -> None:
    """Write the media template's player fields into ``content`` in place."""
    if title := _first_attr(state, _MEDIA_TITLE_ATTRS):
        content["media_title"] = title[:MEDIA_TITLE_MAX]

    # The base subtitle is the friendly name unless the user pointed it somewhere.
    # On a player the artist says more than "Living Room", so it wins - but only
    # over that default, never over a configured source.
    configured_subtitle = entity_config.get(CONF_SUBTITLE_ENTITY) or entity_config.get(CONF_SUBTITLE_ATTRIBUTE)
    if not configured_subtitle and (subtitle := _first_attr(state, _MEDIA_SUBTITLE_ATTRS)):
        content["subtitle"] = subtitle[:MAX_TEXT_LEN]

    content["playback_state"] = _PLAYBACK_STATES.get(state.state, "stopped")

    duration = _media_number(state.attributes.get("media_duration"))
    if duration is not None and duration > 0:
        duration = min(duration, MEDIA_DURATION_MAX)
        content["duration_seconds"] = duration

    if (sample := _media_position(state, now)) is not None:
        position, position_at = sample
        # A player over-reporting its playhead would draw the scrubber past the
        # end; the device clamps too, but the wire should not carry the overshoot.
        content["position_seconds"] = min(position, duration) if duration else position
        if position_at is not None:
            content["position_at"] = position_at

    volume = _media_number(state.attributes.get("volume_level"))
    if volume is not None:
        content["volume"] = max(0.0, min(1.0, volume))

    controls = {
        slot: action
        for slot, url in control_urls(hass, state, entity_config).items()
        if (action := build_tap_action(url, foreground=False)) is not None
    }
    # Always the full slot map: the server deep-merges `controls`, so a slot this
    # frame does not carry explicitly would keep an earlier frame's button alive
    # on the card - dead, since this side stopped offering it. The nulls are what
    # delete those buttons (controls toggled off, a capability lost mid-activity,
    # the base URL going away).
    content["controls"] = {slot: controls.get(slot) for slot in EMITTED_CONTROL_SLOTS}

    # The player card draws a scrubber off position/duration, not the progress bar,
    # and the server still wants the field in [0,1].
    content["progress"] = 0.0


_APPROVAL_OPTION_ID_RE = re.compile(APPROVAL_OPTION_ID_PATTERN)


def _approval_wire_option(raw: object) -> dict | None:
    """One configured answer button as its wire object, or None when unusable.

    Lenient like the other row parsers: a row missing its id or title is skipped
    rather than failing the whole push. An http(s) url makes the option a silent
    webhook, so an empty method is filled to POST the way build_tap_action does;
    a url-less option goes out bare and the server signs an answer URL into it.
    """
    if not isinstance(raw, dict):
        return None
    option_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not _APPROVAL_OPTION_ID_RE.match(option_id) or not title:
        return None
    option: dict = {"id": option_id, "title": title[:APPROVAL_OPTION_TITLE_MAX]}
    style = str(raw.get("style") or "").strip()
    if style in APPROVAL_OPTION_STYLES:
        option["style"] = style
    if icon := str(raw.get("icon") or "").strip():
        option["icon"] = icon[:MAX_TAP_ACTION_ICON_LEN]
    url = str(raw.get("url") or "").strip()
    if not url:
        return option
    option["url"] = url
    if urlparse(url).scheme.lower() in ("http", "https"):
        method = str(raw.get("method") or "").strip().upper()
        # An unknown or missing method falls back to POST, the way build_tap_action
        # shapes its silent webhooks - lenient, this is the mapper path.
        option["method"] = method if method in TAP_ACTION_METHODS else "POST"
        headers = raw.get("headers")
        if isinstance(headers, dict) and headers:
            option["headers"] = {str(k): str(v) for k, v in headers.items()}
        if body := str(raw.get("body") or ""):
            option["body"] = body
    return option


def _apply_approval_fields(content: dict, entity_config: dict) -> None:
    """Write the approval template's question-card fields into ``content`` in place.

    ``options`` only goes out as a complete valid set: a partial list would 422
    the whole push, and a changed option set starts a new round (the server clears
    any answer it already recorded; an identical set on the same step keeps it) -
    so nothing is emitted rather than something wrong. The question itself rides
    the existing ``state`` text.
    """
    # Latent answer-wipe hazard, unreachable today: no config flow offers approval,
    # so no tracked entity routes through here. If one ever does, every frame would
    # re-send `options`; an identical set keeps the recorded answer, but any change
    # (a reordered list, an edited title or url) starts a new round and clears it -
    # each such update would wipe the one thing the card exists to record. Before the
    # flow offers approval, this must emit options on the first frame only, AND the
    # 404-recreate path in
    # activity_manager._send_update must force a re-send: an approval create
    # without options 422s.
    configured = entity_config.get(CONF_APPROVAL_OPTIONS) or []
    options = [o for o in map(_approval_wire_option, configured) if o is not None][:APPROVAL_OPTIONS_MAX]
    ids = {o["id"] for o in options}
    # Three or more buttons render icon-only on the card, so the server requires
    # an icon on each as soon as a third option appears.
    icons_ok = len(options) < 3 or all("icon" in o for o in options)
    if len(options) >= APPROVAL_OPTIONS_MIN and len(ids) == len(options) and icons_ok:
        content["options"] = options
    elif configured:
        _LOGGER.debug("Skipping incomplete approval options for %s", entity_config.get(CONF_ENTITY_ID))

    if source := str(entity_config.get(CONF_APPROVAL_SOURCE) or "").strip():
        content["source"] = source[:APPROVAL_SOURCE_MAX]

    details = []
    for raw in entity_config.get(CONF_APPROVAL_DETAILS) or []:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or "").strip()
        value = str(raw.get("value") or "").strip()
        if not label or not value:
            continue
        details.append({"label": label[:APPROVAL_DETAIL_LABEL_MAX], "value": value[:APPROVAL_DETAIL_VALUE_MAX]})
        if len(details) == APPROVAL_DETAILS_MAX:
            break
    if details:
        content["details"] = details

    # The card has no progress bar; the server still wants the field in [0, 1].
    content["progress"] = 0.0


def _apply_image_fields(content: dict, entity_config: dict) -> None:
    """Write the optional image trio into ``content``, for the templates that take it.

    Nothing is emitted without a URL: the shape and the hash both describe an image,
    and the server rejects the whole trio on a template with no image slot.

    Both values are re-validated here even though the config flow already checked
    them. A stored config predates any later tightening, and a single bad value would
    otherwise 422 *every* push for that entity rather than just dropping the artwork.
    """
    if content.get("template") not in IMAGE_TEMPLATES:
        return
    url = (entity_config.get(CONF_IMAGE_URL) or "").strip()
    if not url:
        return
    if not is_valid_image_url(url):
        # The URL itself stays out of the log: a snapshot URL routinely carries
        # credentials in its userinfo, and this line is emitted on every push.
        _LOGGER.debug("Dropping an image_url for %s that the server would reject", url_host(url))
        return
    content["image_url"] = url

    shape = entity_config.get(CONF_IMAGE_SHAPE) or ""
    if shape in IMAGE_SHAPES:
        content["image_shape"] = shape

    thumbhash = (entity_config.get(CONF_IMAGE_THUMBHASH) or "").strip()
    if not thumbhash:
        return
    if not is_valid_thumbhash(thumbhash):
        _LOGGER.debug("Dropping an image_thumbhash the device could not decode")
        return
    content["image_thumbhash"] = thumbhash


def _resolve_live_end(now: int, remaining: int | None, absolute_end: int | None) -> int | None:
    """Resolve the epoch live_progress animates toward, or None when nothing can.

    A timestamp source pins an absolute end so it doesn't drift as the clock ticks;
    otherwise derive it from the remaining seconds against the shared ``now``. A past
    or absent end wouldn't animate.
    """
    if absolute_end is None and remaining is None:
        return None
    end = absolute_end if absolute_end is not None else now + remaining
    return end if end > now else None


def _carry_window(
    last_content: dict | None,
    *,
    same_run: bool,
    now: int,
    end: int,
    remaining: int | None,
    absolute_end: int | None,
) -> tuple[int, int]:
    """(start_date, end_date) for a bar that fills across the window, held from the previous frame.

    HA only knows how much time is left, so the window's start has to be remembered:
    carry the previous frame's start_date forward while ``same_run`` holds, and
    restamp it to now when it does not. Without the carry-forward a fresh
    start_date=now would ship on every update and the bar would snap back to empty
    on each push.
    """
    prev = last_content or {}

    # A relative source re-derives end_date against the wall clock on every frame,
    # so an unmoved deadline still emits a moving end_date. The server treats that
    # as a structural change: a high-priority push that skips update coalescing.
    # Keep the deadline already shipped until the source genuinely re-estimates.
    prev_end = prev.get("end_date")
    if (
        absolute_end is None
        and same_run
        and type(prev_end) is int
        and prev_end > now
        and remaining == prev.get("remaining_time")
    ):
        end = prev_end

    prev_start = prev.get("start_date")
    start = prev_start if same_run and type(prev_start) is int else now
    # time.time() is not monotonic: an NTP correction or a restored snapshot can
    # move the clock behind the carried anchor and invert the window.
    if start >= end:
        start = now
    return start, end


def _apply_steps_live_progress(
    content: dict,
    last_content: dict | None,
    *,
    now: int,
    remaining: int | None,
    absolute_end: int | None,
) -> None:
    """Anchor the steps live_progress animation to the current step's window.

    On steps, end_date is the end of the *current* step and the bar fills across
    start_date..end_date. The step's start is the moment current_step last
    changed, so the window carries while the step number holds (``_carry_window``).

    The caller sets live_progress False first, so an early return here clears a
    previously-armed animation rather than leaving it running under merge-patch.
    """
    end = _resolve_live_end(now, remaining, absolute_end)
    if end is None:
        return
    same_step = (last_content or {}).get("current_step") == content.get("current_step")
    content["live_progress"] = True
    content["start_date"], content["end_date"] = _carry_window(
        last_content, same_run=same_step, now=now, end=end, remaining=remaining, absolute_end=absolute_end
    )


# Dynamic Island compact ear overrides: (option key, wire key, cap).
_COMPACT_EAR_FIELDS = (
    (CONF_COMPACT_LABEL, "compact_label", MAX_COMPACT_LABEL_LEN),
    (CONF_COMPACT_TRAILING_LABEL, "compact_trailing_label", MAX_COMPACT_TRAILING_LABEL_LEN),
    (CONF_COMPACT_TRAILING_ICON, "compact_trailing_icon", MAX_COMPACT_TRAILING_ICON_LEN),
)
_COMPACT_TRAILING_ICON_RE = re.compile(COMPACT_TRAILING_ICON_PATTERN)


def _add_compact_ear_fields(content: dict, entity_config: dict) -> None:
    """Copy the island ear overrides into content, trimmed and capped.

    Sent whenever the option exists, "" included: the server merge-patches content
    and keeps an absent key, so clearing a label or icon in the flow must ship "".
    """
    for key, wire_key, cap in _COMPACT_EAR_FIELDS:
        if key in entity_config:
            content[wire_key] = entity_config[key].strip()[:cap]
    # The flow's icon picker takes free text, and the server 422s a name outside
    # [A-Za-z0-9._:-]. Send "" instead so the ear keeps the template default and the
    # rest of the push still lands.
    icon = content.get("compact_trailing_icon")
    if icon and not _COMPACT_TRAILING_ICON_RE.match(icon):
        content["compact_trailing_icon"] = ""


def is_calendar_config(entity_config: dict) -> bool:
    """True when a calendar entity runs on its event window rather than start/end states."""
    entity_id = str(entity_config.get(CONF_ENTITY_ID) or "")
    return entity_id.startswith("calendar.") and CONF_CALENDAR_LEAD_MINUTES in entity_config


def _calendar_window(state: State, entity_config: dict) -> tuple[int, int, int] | None:
    """(window open, event start, event end) in unix seconds for the entity's current or next event.

    None for an all-day event or one without a parseable start_time. A missing or
    inverted end reads as the start, so such an event closes the moment it begins.
    """
    attrs = state.attributes
    if attrs.get("all_day"):
        return None
    start = parse_ha_datetime(attrs.get("start_time"))
    if start is None:
        return None
    end = parse_ha_datetime(attrs.get("end_time"))
    start_ts = int(start.timestamp())
    end_ts = max(int(end.timestamp()), start_ts) if end is not None else start_ts
    lead = int(entity_config.get(CONF_CALENDAR_LEAD_MINUTES, DEFAULT_CALENDAR_LEAD_MINUTES) or 0)
    return start_ts - lead * 60, start_ts, end_ts


def calendar_phase(state: State, entity_config: dict, now: float) -> tuple[int, int] | None:
    """(phase start, countdown target) while the event window is open at ``now``, else None.

    Before the event the countdown runs from the window opening to the start; during
    it, from the start to the end.
    """
    window = _calendar_window(state, entity_config)
    if window is None:
        return None
    opens, start, end = window
    if opens <= now < start:
        return opens, start
    if start <= now < end:
        return start, end
    return None


def calendar_next_boundary(state: State, entity_config: dict, now: float) -> int | None:
    """The next window open / event start / event end after ``now``, or None."""
    window = _calendar_window(state, entity_config)
    return next((ts for ts in window if ts > now), None) if window else None


# Keys a frame may leave out where leaving one out means "no value": the optional
# fields of map_content and of the artwork helpers. Updates are RFC 7396 merge
# patches, so the server keeps whatever a frame omits; with_cleared_keys nulls these
# instead. Not listed: keys every frame of their template carries, keys the server
# requires (timeline value, approval options, checklist items, board tiles, log
# lines), the write-only timeline history, and the server-owned log_backlog.
OPTIONAL_CONTENT_KEYS = frozenset(
    {
        # every template
        "remaining_time",
        "background_color",
        "text_color",
        "compact_label",
        "compact_trailing_label",
        "compact_trailing_icon",
        "tap_action",
        "url_action",
        "secondary_url_action",
        "image_url",
        "image_shape",
        "image_thumbhash",
        # countdown, steps, generic
        "live_progress",
        "start_date",
        "end_date",
        "completion_message",
        "warning_threshold",
        "alarm",
        "snooze_seconds",
        "step_labels",
        "step_rows",
        "step_weights",
        "step_colors",
        # alert
        "severity_label",
        "fired_at",
        # gauge, timeline
        "unit",
        "scale",
        "decimals",
        "smoothing",
        "thresholds",
        "units",
        "primary_series",
        # media
        "media_title",
        "duration_seconds",
        "position_seconds",
        "position_at",
        "volume",
        # approval
        "source",
        "details",
    }
)


def with_cleared_keys(content: dict, last_content: dict | None) -> dict:
    """``content`` plus a null for each optional key the server may still hold that this frame omits.

    A null deletes the key under the merge patch. The keys the server may hold are
    the ones the previous frame carried, or every optional key on a run's first
    frame (``last_content`` None): the create upsert never resets content, so the
    previous run's keys are still stored.
    """
    held = OPTIONAL_CONTENT_KEYS if last_content is None else OPTIONAL_CONTENT_KEYS & last_content.keys()
    return content | dict.fromkeys(held - content.keys())


def map_content(
    state: State,
    entity_config: dict,
    *,
    registry_icon: str | None = None,
    hass: HomeAssistant | None = None,
    last_content: dict | None = None,
    progress_max: float | None = None,
) -> dict:
    """Map HA state + attributes to a PushWard content dict.

    ``hass`` is required only when a value field is configured to read from a
    separate companion entity (CONF_*_ENTITY); without it those fields fall back
    to the tracked entity.

    ``last_content`` is the previously sent frame. It is read by the steps
    template's live_progress anchor, which has to know when the current step
    began (see ``_apply_steps_live_progress``), by the countdown window, held the
    same way (``_carry_window``), and by remaining-mode progress, which holds its
    bar while the source is missing.

    ``progress_max`` is the largest progress-source value seen since the activity
    started; only progress_mode "remaining" reads it (see ``_get_progress``).
    """
    # State text: a configured source when it resolves, else the custom label or default formatting
    custom_state_text = _state_text(state, entity_config, hass)
    state_text = custom_state_text or _format_state_label(state, entity_config)

    icon = resolve_icon(state, entity_config, registry_icon=registry_icon, hass=hass)

    # Subtitle: subtitle_entity/subtitle_attribute > friendly_name
    raw_subtitle, _ = _resolve_raw(state, entity_config, CONF_SUBTITLE_ENTITY, CONF_SUBTITLE_ATTRIBUTE, hass)
    subtitle = str(raw_subtitle) if raw_subtitle is not _NO_VALUE else str(state.attributes.get("friendly_name") or "")

    # Accent color resolution: accent_color_attribute > static accent_color > "blue"
    accent = resolve_color(state, entity_config, CONF_ACCENT_COLOR, CONF_ACCENT_COLOR_ATTRIBUTE) or "blue"

    background_color = resolve_color(state, entity_config, CONF_BACKGROUND_COLOR, CONF_BACKGROUND_COLOR_ATTRIBUTE)
    text_color = resolve_color(state, entity_config, CONF_TEXT_COLOR, CONF_TEXT_COLOR_ATTRIBUTE)

    content: dict = {
        "template": entity_config.get(CONF_TEMPLATE, "generic"),
        "progress": _get_progress(
            state,
            entity_config,
            hass,
            progress_max=progress_max,
            last_progress=(last_content or {}).get("progress"),
        ),
        "state": state_text,
        "icon": icon,
        "subtitle": subtitle[:MAX_TEXT_LEN],
        "accent_color": accent,
    }

    _add_compact_ear_fields(content, entity_config)
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

    calendar = calendar_phase(state, entity_config, now) if is_calendar_config(entity_config) else None
    # A calendar's on/off state says nothing; the event's title does, unless a state-text source resolved.
    if (
        calendar is not None
        and custom_state_text is None
        and (message := str(state.attributes.get("message") or "").strip())
    ):
        content["state"] = message[:MAX_TEXT_LEN]
    # Per-phase subtitle text, unless a subtitle source resolved. During the event the
    # phase starts at the event start (a phase implies a window).
    if calendar is not None and raw_subtitle is _NO_VALUE:
        during = calendar[0] == _calendar_window(state, entity_config)[1]
        phase_subtitle = entity_config.get(CONF_CALENDAR_SUBTITLE_DURING if during else CONF_CALENDAR_SUBTITLE_BEFORE)
        if phase_subtitle:
            content["subtitle"] = phase_subtitle

    _add_activity_actions(content, entity_config)

    # Template-specific required fields
    template = content["template"]
    if template == "countdown":
        # A timestamp (finish-time) source anchors end_date to the absolute finish
        # time, so it doesn't drift as the clock ticks. Otherwise derive it from
        # remaining seconds against the shared ``now`` read above. The bar fills
        # across start_date..end_date, held between frames like the steps window.
        prev = last_content or {}
        if remaining is not None:
            content["start_date"], content["end_date"] = _carry_window(
                last_content,
                same_run=True,
                now=now,
                end=absolute_end if absolute_end is not None else now + remaining,
                remaining=remaining,
                absolute_end=absolute_end,
            )
        elif type(prev.get("end_date")) is int:
            # The remaining time went missing: keep the window already shipped.
            content["end_date"] = prev["end_date"]
            if type(prev.get("start_date")) is int:
                content["start_date"] = prev["start_date"]
        else:
            content["end_date"] = now
        completion_msg = entity_config.get(CONF_COMPLETION_MESSAGE)
        if completion_msg:
            content["completion_message"] = completion_msg
        if calendar is not None:
            # Both ends pinned to the event, so the bar fills across the phase.
            content["start_date"], content["end_date"] = calendar
        warning_threshold = entity_config.get(CONF_WARNING_THRESHOLD)
        if warning_threshold is not None:
            content["warning_threshold"] = int(warning_threshold)
        if entity_config.get(CONF_ALARM):
            content["alarm"] = True
            snooze_seconds = entity_config.get(CONF_SNOOZE_SECONDS)
            if snooze_seconds is not None:
                content["snooze_seconds"] = int(snooze_seconds)
    elif template == "steps":
        _apply_steps_fields(content, entity_config, state, hass)
        if entity_config.get(CONF_LIVE_PROGRESS):
            # Updates are merge-patches: an omitted key preserves a prior
            # live_progress=true against a now-expired end_date, leaving the bar
            # animating toward a deadline that has passed. Clear it first; the
            # anchor overwrites with True whenever it can derive a window.
            content["live_progress"] = False
            _apply_steps_live_progress(content, last_content, now=now, remaining=remaining, absolute_end=absolute_end)
    elif template == "alert":
        content["severity"] = entity_config.get(CONF_SEVERITY, DEFAULT_SEVERITY)
        if label := entity_config.get(CONF_SEVERITY_LABEL):
            # Truncate so a pre-cap stored config cannot 400 every push.
            content["severity_label"] = label[:MAX_SEVERITY_LABEL_LEN]
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
        primary = _get_timeline_primary(state, entity_config)
        if primary and len(primary) <= TIMELINE_SERIES_LABEL_MAX:
            content["primary_series"] = primary
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
    elif template == "media":
        _apply_media_fields(content, entity_config, state, hass, now=now)
    elif template == "approval":
        _apply_approval_fields(content, entity_config)
    elif template == "checklist":
        if items := _build_checklist_items(entity_config):
            content["items"] = items
        # The server fills both from the ticks.
        del content["progress"], content["state"]
    elif template == "generic" and entity_config.get(CONF_LIVE_PROGRESS):
        # Opt-in: hand the end time to iOS so it interpolates the progress bar to
        # 1.0 by end_date and shows a counting-down ETA. False first, for the same
        # merge-patch reason as the steps branch above.
        content["live_progress"] = False
        if (end := _resolve_live_end(now, remaining, absolute_end)) is not None:
            content["live_progress"] = True
            content["end_date"] = end

    # After the template branch, so the columns also win over the media card's artist.
    if subtitle_columns := _resolve_log_columns(state, entity_config, hass, CONF_SUBTITLE_COLUMNS):
        content["subtitle"] = " · ".join(subtitle_columns)[:MAX_TEXT_LEN]
    # After the template branch too: gauge sets its progress there.
    if entity_config.get(CONF_ACCENT_FROM_PROGRESS) and "progress" in content:
        content["accent_color"] = progress_accent(content["progress"])

    _apply_image_fields(content, entity_config)

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
    # The island is still up during the completion frame, so the ear overrides stay too.
    _add_compact_ear_fields(content, entity_config)

    _add_activity_actions(content, entity_config)

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
            content["severity_label"] = label[:MAX_SEVERITY_LABEL_LEN]
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
    elif template == "media":
        # Merge patch keeps the player fields the live frames already sent. The
        # transport state is the exception: a finished card still claiming to be
        # playing would tick its scrubber forward forever.
        content["playback_state"] = "stopped"
        content["progress"] = 0.0
    elif template == "approval":
        # The stored options/details survive the merge patch on their own, and
        # re-sending options would clear the recorded answer - the one thing the
        # completion screen is there to show. So nothing template-specific goes
        # out beyond the flat progress the server requires.
        content["progress"] = 0.0
    elif template == "checklist":
        # The stored items survive the merge patch, and progress and the state
        # text are the server's to fill.
        del content["progress"], content["state"]

    # The activity is done, so stop the opt-in ETA interpolation. Without this the
    # completion card keeps filling toward a deadline that no longer means anything.
    # Gate on the opt-in rather than on last_content: a frame whose remaining-time
    # source went away omits live_progress, so the last frame is not a reliable
    # record of whether the server still has it armed.
    if template in LIVE_PROGRESS_TEMPLATES and (
        entity_config.get(CONF_LIVE_PROGRESS) or (last_content and last_content.get("live_progress"))
    ):
        content["live_progress"] = False

    # The artwork is deliberately not restated here. Updates are RFC 7396 merge
    # patches, so a key the patch omits keeps its stored value: the image survives the
    # completion frame on its own, and re-sending it would mean re-hashing an image
    # nothing is going to change. The shutdown end path never sent it either, so
    # leaving it out is what makes all three end paths identical.

    if last_content:
        for key in _COMMON_CARRY_FIELDS:
            if key in last_content:
                content[key] = last_content[key]
        # The run is over, so the last live frame's remaining time would read as time
        # still left. The other keys this frame omits stay on purpose: they are the
        # final values the completion card keeps showing.
        if "remaining_time" in last_content:
            content["remaining_time"] = None

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


def parse_ha_datetime(raw: object) -> datetime | None:
    """Parse an HA attribute into an aware UTC datetime, or None.

    Accepts what integrations actually publish: an RFC 3339 / ISO string, a real
    datetime, or a unix epoch number. A naive datetime is read as HA local time,
    matching how HA renders one (a calendar's start_time is naive local), never as
    the process time zone.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        return dt_util.as_utc(raw) if raw.tzinfo else dt_util.as_utc(dt_util.as_local(raw))
    if isinstance(raw, (int, float)):
        if not math.isfinite(raw):
            return None
        try:
            return datetime.fromtimestamp(raw, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    parsed = dt_util.parse_datetime(raw.strip())
    if parsed is None:
        return None
    return dt_util.as_utc(parsed) if parsed.tzinfo else dt_util.as_utc(dt_util.as_local(parsed))


def _coerce_epoch(raw: object) -> int | None:
    """Coerce a raw value to a unix epoch int: numeric seconds, or an ISO datetime."""
    try:
        return int(float(raw))  # type: ignore[arg-type]
    except (ValueError, TypeError, OverflowError):
        parsed = parse_ha_datetime(raw)
        return int(parsed.timestamp()) if parsed is not None else None


def _coerce_remaining_seconds(
    raw: object, source: State | None, now: int | None = None, *, from_state: bool = True
) -> tuple[int | None, int | None]:
    """Coerce a raw value into ``(remaining_seconds, absolute_end_epoch)``.

    Smart-parses several appliance time formats:
      - device_class 'timestamp' (ISO finish time) -> absolute end + derived remaining
      - a number with a time unit_of_measurement (s, min, h, d, ...) -> seconds
      - 'H:MM:SS' / 'MM:SS' string -> seconds
      - plain number -> seconds
    ``absolute_end_epoch`` is non-None only for a timestamp source. ``now`` lets
    the caller share a single clock read across the emitted fields. ``from_state``
    is False when ``raw`` is an attribute: the unit describes the state, so an
    attribute converts only under the 'duration' device class, as it always has.
    """
    device_class = source.attributes.get("device_class") if source is not None else None

    if device_class == "timestamp":
        parsed = parse_ha_datetime(raw)
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
    # float() accepts "nan"/"inf"; int() then raises ValueError/OverflowError, and
    # map_content is called outside any error guard.
    if not math.isfinite(value):
        return None, None

    unit = source.attributes.get("unit_of_measurement") if source is not None else None
    # A time unit on the state converts whatever the device_class (a 'min' sensor often
    # has none); no unit or any other unit leaves the raw number as seconds.
    if isinstance(unit, str) and unit in DurationConverter.VALID_UNITS and (from_state or device_class == "duration"):
        value = DurationConverter.convert(value, unit, UnitOfTime.SECONDS)
    # An overrun estimate can report negative seconds; the server rejects those
    # (remaining_time must be non-negative) and the activity would freeze.
    return max(0, int(value)), None


def progress_remaining_value(state: State, entity_config: dict, hass: HomeAssistant | None = None) -> float | None:
    """The progress source as a plain number under progress_mode "remaining", else None."""
    if entity_config.get(CONF_PROGRESS_MODE) != PROGRESS_MODE_REMAINING:
        return None
    raw, _source = _resolve_raw(state, entity_config, CONF_PROGRESS_ENTITY, CONF_PROGRESS_ATTRIBUTE, hass)
    return None if raw is _NO_VALUE else _finite_number(raw)


def _get_progress(
    state: State,
    entity_config: dict,
    hass: HomeAssistant | None = None,
    *,
    progress_max: float | None = None,
    last_progress: object = None,
) -> float:
    """Extract progress (0.0-1.0) from a companion entity or the tracked entity.

    Attributes in the 0-255 range (e.g. brightness) are divided by 255;
    all others are treated as 0-100 percentages, so a raw 1 means 1%, not done.
    Progress widgets follow a different convention: widget_mapper._map_progress
    auto-detects fraction vs percent, with a value_scale config override. Do not
    unify the two -- auto-detect here would flip the meaning of the most common
    readings (0 and 1) for every existing activity.

    progress_mode "remaining" reads the source as a value falling toward 0 instead:
    1 - value / the largest value seen this run (``progress_max``, or the value itself).
    While that value is missing the bar holds ``last_progress``, the last frame's.
    """
    if entity_config.get(CONF_PROGRESS_MODE) == PROGRESS_MODE_REMAINING:
        value = progress_remaining_value(state, entity_config, hass)
        if value is None:
            return float(last_progress) if isinstance(last_progress, (int, float)) else 0.0
        peak = max(progress_max or 0.0, value)
        return round(max(0.0, min(1.0, 1 - value / peak)), 2) if peak > 0 else 0.0
    raw, _source = _resolve_raw(state, entity_config, CONF_PROGRESS_ENTITY, CONF_PROGRESS_ATTRIBUTE, hass)
    if raw is _NO_VALUE:
        return 0.0
    attr_name = entity_config.get(CONF_PROGRESS_ATTRIBUTE)
    value = _coerce_scaled_value(raw, None)
    if value is None:
        _LOGGER.debug("Could not parse progress for %s", state.entity_id)
        return 0.0
    # Open-coded rather than _rescale_attr: that helper maps 0-255 to 0-100
    # while this one needs 0-1.
    scale = 255.0 if attr_name in _ATTRS_0_255 else 100.0
    return round(max(0.0, min(1.0, value / scale)), 2)


def _get_current_step(state: State, entity_config: dict, hass: HomeAssistant | None = None) -> int:
    """Extract current step from a companion entity or the tracked entity, clamped to 0..total."""
    total = entity_config.get(CONF_TOTAL_STEPS, DEFAULT_TOTAL_STEPS)
    raw, _source = _resolve_raw(state, entity_config, CONF_CURRENT_STEP_ENTITY, CONF_CURRENT_STEP_ATTR, hass)
    if raw is _NO_VALUE:
        return 0
    try:
        value = int(float(raw))  # type: ignore[arg-type]
        return max(0, min(total, value))
    except (ValueError, TypeError, OverflowError):
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
    return _coerce_remaining_seconds(raw, source, now, from_state=not entity_config.get(CONF_REMAINING_TIME_ATTR))


def _coerce_scaled_value(raw: object, attr_name: str | None) -> float | None:
    """Coerce a raw value to float, rescaling 0-255 attributes to 0-100. None if unparseable or not finite."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
    # float() takes "nan" and "inf"; the server refuses a non-finite number.
    if not math.isfinite(value):
        return None
    return _rescale_attr(value, attr_name) if attr_name else value


def _single_series_label(state: State) -> str:
    """The single-series label: the tracked entity's friendly name, cut to the server's label cap."""
    return str(state.attributes.get("friendly_name", state.entity_id))[:TIMELINE_SERIES_LABEL_MAX]


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
        # The label is emitted verbatim as a value-map key, which the server caps at
        # TIMELINE_SERIES_LABEL_MAX runes; clamp defensively so a legacy/over-length
        # series-map label can't 400 the whole timeline push (the config flow also caps
        # it, but this guards stored configs that predate that cap).
        values[str(label)[:TIMELINE_SERIES_LABEL_MAX]] = coerced

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
        attr = series.get(CONF_SERIES_ATTRIBUTE)
        raw = source.attributes.get(attr) if attr else source.state
        if raw is None:
            continue
        coerced = _coerce_scaled_value(raw, attr)
        if coerced is None:
            _LOGGER.debug("Could not parse timeline series entity %s for %s", entity_id, state.entity_id)
            continue
        # Frozen to <= TIMELINE_SERIES_LABEL_MAX at config time; clamp again defensively
        # so the value-map key never exceeds the server cap.
        values[str(label)[:TIMELINE_SERIES_LABEL_MAX]] = coerced

    if entity_config.get(CONF_SERIES) or entity_config.get(CONF_SERIES_ENTITIES):
        return values

    # Single series: keep the label anchored to the tracked entity so samples
    # from a companion value entity land in the same series.
    label = _single_series_label(state)
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


def _get_timeline_primary(state: State, entity_config: dict) -> str | None:
    """Pick the primary series label for a timeline template.

    An explicit CONF_PRIMARY_SERIES wins. Otherwise the tracked entity's own
    series takes precedence: the first CONF_SERIES attribute label when any is
    configured, then the first CONF_SERIES_ENTITIES label, and finally the
    tracked entity's friendly name (the single-series fallback label used by
    _get_timeline_values).
    """
    explicit = (entity_config.get(CONF_PRIMARY_SERIES) or "").strip()
    if explicit:
        return explicit
    for label in (entity_config.get(CONF_SERIES) or {}).values():
        return label
    for series in entity_config.get(CONF_SERIES_ENTITIES) or []:
        if isinstance(series, dict) and series.get(CONF_LABEL):
            return series[CONF_LABEL]
    return _single_series_label(state)


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
        sources[_single_series_label(state)] = entity_config.get(CONF_VALUE_ENTITY) or state.entity_id

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
