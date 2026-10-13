"""Setup, sensors, the statistics sync against the recorder, reauth on auth failure, unload."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import get_last_statistics, statistics_during_period
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.globird_stats.api import GloBirdAuthError, GloBirdError
from custom_components.globird_stats.const import DOMAIN
from custom_components.globird_stats.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    ACCOUNT_ID,
    ACCOUNT_NUMBER,
    BASIC_SERVICE_ID,
    EMAIL,
    ENTRY_DATA,
    SMART_SERVICE_ID,
    FakePortal,
    cost_rows,
    expected_cost_total,
    expected_import_total,
    usage_row,
)

IMPORT_ID = f"{DOMAIN}:grid_import_{SMART_SERVICE_ID}"
COST_ID = f"{DOMAIN}:grid_cost_{SMART_SERVICE_ID}"


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry for the fake account."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=str(ACCOUNT_ID), title=ACCOUNT_NUMBER, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    return entry


async def _last(hass: HomeAssistant, sid: str) -> dict[str, Any] | None:
    res = await get_instance(hass).async_add_executor_job(get_last_statistics, hass, 1, sid, False, {"sum", "state"})
    rows = res.get(sid)
    return rows[0] if rows else None


async def _count(hass: HomeAssistant, sid: str) -> int:
    res = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {sid},
        "hour",
        None,
        {"sum"},
    )
    return len(res.get(sid, []))


def _usage_days(portal: FakePortal) -> set[str]:
    return {
        r["readDate"]
        for r in portal.usage[SMART_SERVICE_ID]
        if r["suffix"].startswith("E") and len(r.get("usageArray") or []) == 48
    }


async def test_setup_sensors_and_statistics(
    recorder_mock: Recorder, hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock, portal: FakePortal
) -> None:
    """Setup creates both sensors and writes both statistics, with sums matching the payload totals."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    balance = hass.states.get("sensor.globird_balance")
    assert balance is not None
    assert float(balance.state) == -75.16
    assert balance.attributes["unit_of_measurement"] == "AUD"
    assert balance.attributes["max_refundable"] == 70.0
    day = hass.states.get("sensor.globird_latest_data_day")
    assert day is not None
    assert day.state == "2026-10-03"

    # The smart meter is the energised one; the basic-meter service is kept but gets no statistics or cost call.
    data = entry.runtime_data.data
    assert data.services[str(SMART_SERVICE_ID)].serial_number == "SM0001"
    assert data.services[str(BASIC_SERVICE_ID)].usage_rows == []
    assert all(c.args[0] != str(BASIC_SERVICE_ID) for c in mock_client.async_cost_detail.await_args_list)
    assert str(900003) not in data.services  # gas

    await async_wait_recording_done(hass)
    usage = await _last(hass, IMPORT_ID)
    cost = await _last(hass, COST_ID)
    assert usage is not None and cost is not None
    assert usage["sum"] == pytest.approx(expected_import_total(portal.usage[SMART_SERVICE_ID]), abs=1e-3)
    assert cost["sum"] == pytest.approx(
        expected_cost_total(portal.cost[SMART_SERVICE_ID], _usage_days(portal)), abs=1e-3
    )
    assert await _count(hass, IMPORT_ID) == 72
    assert await _last(hass, f"{DOMAIN}:grid_import_{BASIC_SERVICE_ID}") is None


async def test_rolled_window_continues_sums(
    recorder_mock: Recorder, hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock, portal: FakePortal
) -> None:
    """When the portal's window drops a day and adds one, the stored sums carry on from history."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    first_usage = expected_import_total(portal.usage[SMART_SERVICE_ID])
    first_cost = expected_cost_total(portal.cost[SMART_SERVICE_ID], _usage_days(portal))

    new_day = [0.4] * 48
    dropped = ("2026/10/01", "2026/10/04")
    portal.usage[SMART_SERVICE_ID] = [r for r in portal.usage[SMART_SERVICE_ID] if r["readDate"] not in dropped]
    portal.usage[SMART_SERVICE_ID].append(usage_row("2026/10/04", new_day))
    portal.cost[SMART_SERVICE_ID] = [r for r in portal.cost[SMART_SERVICE_ID] if r["date"] not in dropped]
    portal.cost[SMART_SERVICE_ID] += cost_rows("2026/10/04", 4.00, 1.10)

    coordinator = entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    usage = await _last(hass, IMPORT_ID)
    cost = await _last(hass, COST_ID)
    assert usage["sum"] == pytest.approx(first_usage + sum(new_day), abs=1e-3)
    assert cost["sum"] == pytest.approx(first_cost + 5.10, abs=1e-3)
    assert await _count(hass, IMPORT_ID) == 96
    assert hass.states.get("sensor.globird_latest_data_day").state == "2026-10-04"
    # The second refresh used the existing session rather than logging in again.
    assert mock_client.async_login.await_count == 1
    assert mock_client.async_current_user.await_count == 1


async def test_unchanged_data_skips_sync(
    recorder_mock: Recorder, hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A refresh with the same payload doesn't re-import."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with patch("custom_components.globird_stats.coordinator.async_sync_service") as sync:
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    sync.assert_not_called()


async def test_sync_failure_does_not_fail_refresh(
    recorder_mock: Recorder, hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A statistics error is logged, the sensors still update."""
    with patch("custom_components.globird_stats.coordinator.async_sync_service", side_effect=RuntimeError("db")):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.last_update_success
    assert hass.states.get("sensor.globird_balance") is not None


async def test_auth_failure_starts_reauth(
    recorder_mock: Recorder, hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A rejected login at setup puts the entry into reauth and closes the client."""
    mock_client.async_login.side_effect = GloBirdAuthError("bad password")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    mock_client.async_close.assert_awaited()


async def test_portal_error_retries(
    recorder_mock: Recorder, hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A portal error at setup means retry later, not reauth."""
    mock_client.async_login.side_effect = GloBirdError("503")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_legacy_yaml_warns(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The old bare `globird_stats:` key loads and logs the migration hint."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: None})
    await hass.async_block_till_done()
    assert "configure GloBird Energy through the UI" in caplog.text
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_diagnostics_redacted(
    recorder_mock: Recorder, hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Diagnostics carry no email, password, account number, NMI or serials."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = repr(diag)
    for secret in (EMAIL, "correct horse", ACCOUNT_NUMBER, "6300000001", "SM0001", "3000"):
        assert secret not in text, secret
    assert diag["data"]["balance"] == -75.16
    assert diag["entry"]["data"]["password"] == "**REDACTED**"


async def test_unload(
    recorder_mock: Recorder, hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Unload removes the sensors' entry state and closes the portal session."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("sensor.globird_balance").state == "unavailable"
    mock_client.async_close.assert_awaited()
