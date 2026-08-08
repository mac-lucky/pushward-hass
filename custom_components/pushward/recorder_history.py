"""Recorder-backed history reads shared by the activity and widget managers.

Both surfaces seed a chart from HA's recorder on start: the timeline activity
seeds its sparkline series, the trend widget seeds its points buffer. The query
strategy is identical, so it lives here rather than in either manager.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import timedelta
from functools import partial

from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Statistics-seed period cutoff (minutes). 5-minute short-term stats share raw-state
# retention (purged at purge_keep_days, which users often lower), so use them only
# for recent windows; past this, read never-purged hourly stats so a multi-day
# window stays fully covered regardless of retention.
STATS_SHORT_TERM_MAX_MINUTES = 1440  # 24 h


def downsample_evenly(points: list, max_points: int) -> list:
    """Reduce a time-sorted point list to at most max_points.

    Keeps the first and last sample and spreads the rest evenly across the span.
    A tail slice would collapse a wide window to its most recent samples, so a
    multi-day seed would arrive showing only the last few hours. Even sampling
    keeps the full time range; the server does the final downsample that fits the
    push payload.
    """
    count = len(points)
    if count <= max_points:
        return points
    if max_points <= 2:
        return [points[0], points[-1]][:max_points]
    # count > max_points forces step > 1, so round(i * step) comes out unique and
    # ascending across i (and i == max_points - 1 maps to the last index) - no
    # dedup or re-sort needed.
    step = (count - 1) / (max_points - 1)
    return [points[round(i * step)] for i in range(max_points)]


async def async_recorder_states(
    hass: HomeAssistant, entity_ids: list[str], period_minutes: int
) -> dict[str, list[dict]]:
    """Batch-query history for several entities' numeric points.

    Entities with a ``state_class`` read from long-term statistics (5-minute
    buckets): pre-aggregated and row-bounded, so a fast-changing sensor over a
    multi-day window never materializes its full raw series. Entities without
    one read raw recorder states. Both return ``{entity_id: [{timestamp,
    value}, ...]}`` ascending; entities that yield nothing are absent and seed
    from the live ring buffer instead. Mirrors HA's own history UI, which
    gates on the same ``state_class`` attribute.
    """
    if not entity_ids:
        return {}

    stats_ids: list[str] = []
    raw_ids: list[str] = []
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state and state.attributes.get(ATTR_STATE_CLASS):
            stats_ids.append(entity_id)
        else:
            raw_ids.append(entity_id)

    result: dict[str, list[dict]] = {}
    if stats_ids:
        result.update(await _statistics_points(hass, stats_ids, period_minutes))
    if raw_ids:
        result.update(await _raw_recorder_points(hass, raw_ids, period_minutes))
    return result


async def _statistics_points(hass: HomeAssistant, entity_ids: list[str], period_minutes: int) -> dict[str, list[dict]]:
    """Read long-term statistics for state_class entities.

    ``statistics_during_period`` returns pre-aggregated buckets keyed by
    statistic_id (the entity_id for a sensor), each carrying a float epoch
    ``start`` plus ``mean`` (measurement) or ``state`` (total /
    total_increasing). Bounded by bucket count, not by write frequency.
    """
    try:
        from homeassistant.components.recorder.statistics import statistics_during_period
    except ImportError:
        return {}

    from homeassistant.helpers.recorder import get_instance

    now = dt_util.utcnow()
    start = now - timedelta(minutes=period_minutes)
    period = "5minute" if period_minutes <= STATS_SHORT_TERM_MAX_MINUTES else "hour"

    try:
        stats = await get_instance(hass).async_add_executor_job(
            partial(
                statistics_during_period,
                hass,
                start,
                now,
                set(entity_ids),
                period,
                None,
                {"mean", "state"},
            )
        )
    except Exception:
        _LOGGER.debug("Failed to query statistics for %s", entity_ids, exc_info=True)
        return {}

    result: dict[str, list[dict]] = {}
    for entity_id in entity_ids:
        points: list[dict] = []
        for row in stats.get(entity_id, []):
            start_ts = row.get("start")
            value = row.get("mean")
            if value is None:  # total / total_increasing have no mean
                value = row.get("state")
            if start_ts is None or value is None:
                continue
            with contextlib.suppress(ValueError, TypeError):
                points.append({"timestamp": int(start_ts), "value": float(value)})
        if points:
            result[entity_id] = points
    return result


async def _raw_recorder_points(
    hass: HomeAssistant, entity_ids: list[str], period_minutes: int
) -> dict[str, list[dict]]:
    """Read raw recorder states for entities without long-term statistics.

    HA 2024.8+ strips attributes from the recorder, so attribute-sourced
    series can't seed this way and fall back to the live ring buffer.
    """
    try:
        from homeassistant.components.recorder.history import get_significant_states
    except ImportError:
        return {}

    from homeassistant.helpers.recorder import get_instance

    now = dt_util.utcnow()
    start = now - timedelta(minutes=period_minutes)

    # Only {timestamp, value} is needed, and the window can span up to 10 days:
    # significant_changes_only skips redundant same-value re-writes at the SQL
    # layer (a numeric chart wants transitions), and no_attributes skips the
    # attributes join we never read, keeping a long-window query cheap.
    try:
        states = await get_instance(hass).async_add_executor_job(
            partial(
                get_significant_states,
                hass,
                start,
                now,
                list(entity_ids),
                significant_changes_only=True,
                no_attributes=True,
            )
        )
    except Exception:
        _LOGGER.debug("Failed to query recorder for %s", entity_ids, exc_info=True)
        return {}

    result: dict[str, list[dict]] = {}
    for entity_id in entity_ids:
        points: list[dict] = []
        for state_obj in states.get(entity_id, []):
            if state_obj.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            with contextlib.suppress(ValueError, TypeError):
                points.append({"timestamp": int(state_obj.last_updated.timestamp()), "value": float(state_obj.state)})
        if points:
            result[entity_id] = points
    return result
