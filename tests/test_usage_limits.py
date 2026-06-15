"""Tests for the usage-limit Repair issues raised by the usage coordinator.

The coordinator (``coordinator.py``) evaluates every metered resource after each
``GET /auth/me`` poll and raises a non-fixable WARNING Repair issue when a resource
is at/over its limit, deleting it once back under (or on unload). Uncapped premium
resources (no ``*_limit`` key) never trip.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.pushward as pushward_pkg
from custom_components.pushward.api import PushWardApiError
from custom_components.pushward.const import (
    APP_STORE_URL,
    CONF_INTEGRATION_KEY,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    DOMAIN,
    USAGE_LIMIT_RESOURCES,
    usage_limit_issue_id,
)
from custom_components.pushward.coordinator import PushWardUsageCoordinator, _format_reset
from custom_components.pushward.sensor import USAGE_SENSORS

from .conftest import make_premium_usage_payload, make_usage_payload


def _entry(unique_id: str = DOMAIN) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PushWard",
        data={CONF_SERVER_URL: DEFAULT_SERVER_URL, CONF_INTEGRATION_KEY: "test-key"},
        version=2,
        unique_id=unique_id,
    )


def _coordinator(
    hass: HomeAssistant, payload: dict, *, unique_id: str = DOMAIN
) -> tuple[PushWardUsageCoordinator, MockConfigEntry]:
    """Coordinator whose injected ``get_me`` returns ``payload`` (no network)."""
    entry = _entry(unique_id)
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.get_me = AsyncMock(return_value=payload)
    return PushWardUsageCoordinator(hass, api, entry), entry


def _issue(hass: HomeAssistant, entry: MockConfigEntry, used_key: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, usage_limit_issue_id(entry.entry_id, used_key))


# --- raise / clear ---


async def test_issue_raised_when_over_limit(hass: HomeAssistant) -> None:
    coordinator, entry = _coordinator(hass, make_usage_payload(notifications_used=600, notifications_limit=500))
    await coordinator._async_update_data()

    issue = _issue(hass, entry, "notifications_used")
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.is_persistent is False  # premise of the async_unload_entry cleanup
    assert issue.translation_key == "usage_limit_notifications"
    assert issue.learn_more_url == APP_STORE_URL
    assert issue.translation_placeholders == {
        "used": "600",
        "limit": "500",
        "resets_at": "2026-07-01",
    }
    # Resources still under limit get no issue.
    assert _issue(hass, entry, "emails_used") is None


async def test_no_issue_when_under_limit(hass: HomeAssistant) -> None:
    coordinator, entry = _coordinator(hass, make_usage_payload())
    await coordinator._async_update_data()
    for resource in USAGE_LIMIT_RESOURCES:
        assert _issue(hass, entry, resource.used_key) is None


async def test_at_limit_boundary_trips(hass: HomeAssistant) -> None:
    # used == limit counts as over (the next push is rejected).
    coordinator, entry = _coordinator(hass, make_usage_payload(emails_used=500, emails_limit=500))
    await coordinator._async_update_data()
    assert _issue(hass, entry, "emails_used") is not None


async def test_zero_limit_never_trips(hass: HomeAssistant) -> None:
    # A non-positive limit reads as "no cap" — never a false alarm.
    coordinator, entry = _coordinator(hass, make_usage_payload(emails_used=5, emails_limit=0))
    await coordinator._async_update_data()
    assert _issue(hass, entry, "emails_used") is None


async def test_compact_payload_raises_no_issue(hass: HomeAssistant) -> None:
    # Old server that doesn't return usage: missing keys must not crash or flag.
    coordinator, entry = _coordinator(hass, {"id": "u", "activity_count": 0})
    await coordinator._async_update_data()
    for resource in USAGE_LIMIT_RESOURCES:
        assert _issue(hass, entry, resource.used_key) is None


async def test_issue_cleared_when_back_under_limit(hass: HomeAssistant) -> None:
    coordinator, entry = _coordinator(hass, make_usage_payload(widget_updates_used=50, widget_updates_limit=50))
    await coordinator._async_update_data()
    assert _issue(hass, entry, "widget_updates_used") is not None

    # Quota resets: usage drops below the cap → issue deleted on next poll.
    coordinator._api.get_me = AsyncMock(return_value=make_usage_payload(widget_updates_used=0))
    await coordinator._async_update_data()
    assert _issue(hass, entry, "widget_updates_used") is None


async def test_transient_failure_keeps_existing_issue(hass: HomeAssistant) -> None:
    # A failed poll leaves a real over-limit warning in place rather than falsely clearing it.
    coordinator, entry = _coordinator(hass, make_usage_payload(notifications_used=600, notifications_limit=500))
    await coordinator._async_update_data()
    assert _issue(hass, entry, "notifications_used") is not None

    coordinator._api.get_me = AsyncMock(side_effect=PushWardApiError("boom"))
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert _issue(hass, entry, "notifications_used") is not None


# --- premium edge cases ---


async def test_premium_uncapped_resources_never_trip(hass: HomeAssistant) -> None:
    # Premium omits LA/widget limit keys; huge usage must not raise an issue.
    coordinator, entry = _coordinator(
        hass,
        make_premium_usage_payload(live_activity_updates_used=999999, widget_updates_used=999999),
    )
    await coordinator._async_update_data()
    assert _issue(hass, entry, "live_activity_updates_used") is None
    assert _issue(hass, entry, "widget_updates_used") is None


async def test_premium_notifications_use_daily_reset(hass: HomeAssistant) -> None:
    coordinator, entry = _coordinator(
        hass, make_premium_usage_payload(notifications_used=5000, notifications_limit=5000)
    )
    await coordinator._async_update_data()
    issue = _issue(hass, entry, "notifications_used")
    assert issue is not None
    # Daily-capped on premium → the daily reset, not the monthly one.
    assert issue.translation_placeholders["resets_at"] == "2026-06-15"


# --- multi-entry isolation ---


async def test_two_entries_independent_issues(hass: HomeAssistant) -> None:
    coord_over, entry_over = _coordinator(
        hass, make_usage_payload(notifications_used=600, notifications_limit=500), unique_id="over"
    )
    coord_under, entry_under = _coordinator(hass, make_usage_payload(), unique_id="under")
    await coord_over._async_update_data()
    await coord_under._async_update_data()

    # Per-entry issue ids keep the two from colliding.
    assert _issue(hass, entry_over, "notifications_used") is not None
    assert _issue(hass, entry_under, "notifications_used") is None


# --- unload cleanup ---


async def test_unload_clears_outstanding_issue(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.get_me = AsyncMock(return_value=make_usage_payload(notifications_used=600, notifications_limit=500))

    with patch("custom_components.pushward.PushWardApiClient", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert _issue(hass, entry, "notifications_used") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "notifications_used") is None


# --- reset formatting ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-01T00:00:00Z", "2026-07-01"),
        ("2026-07-01", "2026-07-01"),  # already date-only, no "T"
        (None, "the next reset"),
        ("", "the next reset"),
        (1751328000, "the next reset"),  # server sends an int → graceful fallback
    ],
)
def test_format_reset(value, expected) -> None:
    assert _format_reset(value) == expected


# --- drift guards (no HomeAssistant needed) ---


def test_usage_limit_resources_match_sensors() -> None:
    """The repair list must track the sensor list, or a resource is silently uncovered."""
    sensor_keys = {(d.key, d.limit_key) for d in USAGE_SENSORS}
    resource_keys = {(r.used_key, r.limit_key) for r in USAGE_LIMIT_RESOURCES}
    assert resource_keys == sensor_keys


def test_usage_limit_translations_exist() -> None:
    en = json.loads((Path(pushward_pkg.__file__).parent / "translations" / "en.json").read_text())
    issues = en["issues"]
    injected = {"used", "limit", "resets_at"}  # the only placeholders the coordinator supplies
    for resource in USAGE_LIMIT_RESOURCES:
        issue = issues[resource.translation_key]
        assert issue["title"] and issue["description"]
        # Any {token} the coordinator never supplies would render as a literal brace.
        tokens = set(re.findall(r"{(\w+)}", issue["title"] + issue["description"]))
        assert tokens <= injected
