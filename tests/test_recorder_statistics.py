"""The heatmap's daily cells and statistics lookup against a real recorder database.

The widget tests mock the recorder rows; these import hourly statistics into the
test recorder so the "day" reduction, the change column and the local-midnight
row starts are Home Assistant's own, across a DST change.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.loader import DATA_CUSTOM_COMPONENTS
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.pushward.recorder_history import async_daily_values, async_statistics_info, statistics_has_mean


@pytest.fixture(autouse=True)
def enable_custom_integrations(recorder_mock, hass: HomeAssistant) -> None:
    """The conftest one, with the recorder set up first (it must exist before hass)."""
    hass.data.pop(DATA_CUSTOM_COMPONENTS)


def _meta(statistic_id: str, *, total: bool) -> dict:
    return {
        "has_mean": not total,
        "mean_type": StatisticMeanType.NONE if total else StatisticMeanType.ARITHMETIC,
        "has_sum": total,
        "name": None,
        "source": "recorder",
        "statistic_id": statistic_id,
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR if total else UnitOfTemperature.CELSIUS,
        "unit_class": "energy" if total else "temperature",
    }


def _hours(first: date, gap: date) -> list[datetime]:
    """Every hour start (UTC) from local midnight of `first` up to the last full hour, `gap` left out."""
    t = dt_util.as_utc(dt_util.start_of_local_day(first))
    end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    out = []
    while t < end:
        if dt_util.as_local(t).date() != gap:
            out.append(t)
        t += timedelta(hours=1)  # UTC steps, so the 25-hour DST day gets all 25
    return out


async def test_daily_values_from_real_statistics_across_dst(hass: HomeAssistant, freezer) -> None:
    """Warsaw left summer time on 2026-10-25, a 25-hour day. The energy total grows 1 kWh an
    hour, so each day's change is its hour count; 2026-10-23 has no statistics at all."""
    await hass.config.async_set_time_zone("Europe/Warsaw")
    freezer.move_to("2026-10-27T10:00:00+00:00")  # 11:00 local
    hours = _hours(date(2026, 10, 20), gap=date(2026, 10, 23))

    async_import_statistics(
        hass,
        _meta("sensor.energy", total=True),
        [{"start": t, "sum": float(n + 1), "state": float(n + 1)} for n, t in enumerate(hours)],
    )
    # The temperature sits at the day-of-month all day, with min/max one either side.
    async_import_statistics(
        hass,
        _meta("sensor.temperature", total=False),
        [
            {"start": t, "mean": float(d), "min": float(d - 1), "max": float(d + 1)}
            for t in hours
            for d in [dt_util.as_local(t).day]
        ],
    )
    await async_wait_recording_done(hass)

    assert await async_daily_values(hass, "sensor.energy", 7, "change") == [24.0, 24.0, None, 24.0, 25.0, 24.0, 11.0]
    days = [21.0, 22.0, None, 24.0, 25.0, 26.0, 27.0]
    assert await async_daily_values(hass, "sensor.temperature", 7, "mean") == days
    assert await async_daily_values(hass, "sensor.temperature", 7, "max") == [
        None if d is None else d + 1 for d in days
    ]
    assert await async_daily_values(hass, "sensor.temperature", 7, "min") == [
        None if d is None else d - 1 for d in days
    ]

    total = await async_statistics_info(hass, "sensor.energy")
    measurement = await async_statistics_info(hass, "sensor.temperature")
    assert total["has_sum"] and not statistics_has_mean(total)
    assert statistics_has_mean(measurement) and not measurement["has_sum"]


async def test_no_statistics(hass: HomeAssistant) -> None:
    assert await async_statistics_info(hass, "sensor.nothing") is None
    assert await async_daily_values(hass, "sensor.nothing", 7, "mean") == [None] * 7
