"""Shared fixtures: custom integrations enabled, and a fake portal client with invented payloads."""

from __future__ import annotations

from collections.abc import Generator
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.globird_stats.const import CONF_EMAIL, CONF_PASSWORD

ACCOUNT_ID = 123456
ACCOUNT_NUMBER = "G0001234"
SMART_SERVICE_ID = 900001
BASIC_SERVICE_ID = 900002
EMAIL = "someone@example.com"
PASSWORD = "correct horse"


def _day(base: float, step: float) -> list[float]:
    return [round(base + step * (i % 6), 3) for i in range(48)]


CURRENT_USER: dict[str, Any] = {
    "emailAddress": EMAIL,
    "accounts": [
        {
            "accountId": ACCOUNT_ID,
            "accountNumber": ACCOUNT_NUMBER,
            "accountName": "A Customer",
            "services": [
                {
                    "accountServiceId": SMART_SERVICE_ID,
                    "serviceType": "Power",
                    "status": "Switched",
                    "postCode": "3000",
                    "state": "VIC",
                    "siteIdentifier": "6300000001",
                    "siteAddress": "1 Example St",
                    "closedDate": None,
                },
                {
                    "accountServiceId": BASIC_SERVICE_ID,
                    "serviceType": "Power",
                    "status": "Switched",
                    "postCode": "3000",
                    "state": "VIC",
                    "siteIdentifier": "6300000002",
                    "closedDate": None,
                },
                {
                    "accountServiceId": 900003,
                    "serviceType": "Gas",
                    "status": "Switched",
                    "siteIdentifier": "5300000003",
                    "closedDate": None,
                },
            ],
        }
    ],
}

METERS: dict[int, list[dict[str, Any]]] = {
    SMART_SERVICE_ID: [
        {"serialNumber": "OLD0001", "meterReadType": "Smart", "meterStatus": "De-energised", "nmi": "6300000001"},
        {"serialNumber": "SM0001", "meterReadType": "Smart", "meterStatus": "Energized", "nmi": "6300000001"},
    ],
    BASIC_SERVICE_ID: [
        {"serialNumber": "BA0002", "meterReadType": "Basic", "meterStatus": "Energized", "nmi": "6300000002"},
    ],
}


def usage_row(day: str, values: list[float], suffix: str = "E1") -> dict[str, Any]:
    """One smart-meter row as the portal sends it."""
    return {
        "readDate": day,
        "suffix": suffix,
        "usageArray": values,
        "usage": round(sum(values), 3),
        "chargeType": "Peak Usage",
        "chargeCategoryCode": "USAGE",
        "meterStatus": "Energized",
        "serial": "SM0001",
        "minQualityMethod": "A",
        "timePeriods": [{"startTime": "00:00:00", "endTime": "1.00:00:00"}],
    }


def cost_rows(day: str, usage: float, supply: float, missing: bool = False) -> list[dict[str, Any]]:
    """The two cost rows the portal sends for one day."""
    return [
        {
            "chargeCategory": "USAGE",
            "chargeType": None,
            "date": day,
            "amount": usage,
            "quantity": 20.0,
            "isMeterReadMissingOnDate": missing,
        },
        {
            "chargeCategory": "SUPPLY",
            "chargeType": None,
            "date": day,
            "amount": supply,
            "quantity": 1.0,
            "isMeterReadMissingOnDate": missing,
        },
    ]


USAGE_ROWS = [
    usage_row("2026/10/01", _day(0.20, 0.05)),
    usage_row("2026/10/02", _day(0.30, 0.02)),
    usage_row("2026/10/03", _day(0.10, 0.10)),
    usage_row("2026/10/03", [0.05] * 48, suffix="B1"),  # export register: ignored
    {"readDate": "2026/10/04", "suffix": "E1", "usageArray": [0.1] * 20, "usage": 2.0},  # partial day: ignored
]
COST_ROWS = [
    *cost_rows("2026/10/01", 6.12, 1.10),
    *cost_rows("2026/10/02", 5.43, 1.10),
    *cost_rows("2026/10/03", 7.01, 1.10),
    *cost_rows("2026/10/04", 0.80, 1.10, missing=True),  # incomplete: left out
]
BASIC_USAGE_ROWS = [{"readDate": "2026/10/01", "suffix": "E1", "usage": 12.5, "chargeType": "Usage"}]

BALANCE = {"balance": -75.16, "maxRefundableAmount": 70.0}

ENTRY_DATA = {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}


def expected_import_total(rows: list[dict[str, Any]]) -> float:
    """kWh over complete E-register days."""
    return sum(
        sum(r["usageArray"])
        for r in rows
        if r["suffix"].startswith("E") and isinstance(r.get("usageArray"), list) and len(r["usageArray"]) == 48
    )


def expected_cost_total(rows: list[dict[str, Any]], usage_days: set[str]) -> float:
    """AUD over complete days that also have interval data."""
    return sum(r["amount"] for r in rows if not r["isMeterReadMissingOnDate"] and r["date"] in usage_days)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Load custom_components/ in every test that uses hass.

    The recorder fixture has to come up before hass, so when a test asks for it, it is requested first here.
    Tests without hass (the pure series and client tests) are left alone.
    """
    if "hass" not in request.fixturenames:
        return
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_mock")
    request.getfixturevalue("enable_custom_integrations")


class FakePortal:
    """Mutable payloads the fake client serves; tests change them between refreshes."""

    def __init__(self) -> None:
        self.current_user = deepcopy(CURRENT_USER)
        self.meters = deepcopy(METERS)
        self.usage = {SMART_SERVICE_ID: deepcopy(USAGE_ROWS), BASIC_SERVICE_ID: deepcopy(BASIC_USAGE_ROWS)}
        self.cost = {SMART_SERVICE_ID: deepcopy(COST_ROWS), BASIC_SERVICE_ID: []}
        self.balance = dict(BALANCE)


def make_client(portal: FakePortal) -> MagicMock:
    """An AsyncMock-based stand-in for GloBirdClient serving `portal`."""
    client = MagicMock()
    client.async_login = AsyncMock(side_effect=lambda: deepcopy(portal.current_user))
    client.async_current_user = AsyncMock(side_effect=lambda: deepcopy(portal.current_user))
    client.async_balance = AsyncMock(side_effect=lambda account_id: dict(portal.balance))
    client.async_read_meters = AsyncMock(side_effect=lambda sid: deepcopy(portal.meters.get(int(sid), [])))
    client.async_smart_meter_reads = AsyncMock(
        side_effect=lambda sid, identifier, serial, start, end: deepcopy(portal.usage.get(int(sid), []))
    )
    client.async_cost_detail = AsyncMock(
        side_effect=lambda sid, identifier, start, end: deepcopy(portal.cost.get(int(sid), []))
    )
    client.async_close = AsyncMock()
    return client


@pytest.fixture
def portal() -> FakePortal:
    """The payloads behind the fake client."""
    return FakePortal()


@pytest.fixture
def mock_client(portal: FakePortal) -> Generator[MagicMock]:
    """Patch GloBirdClient in the config flow and the coordinator with the fake."""
    client = make_client(portal)
    with (
        patch("custom_components.globird_stats.config_flow.GloBirdClient", return_value=client) as flow_cls,
        patch("custom_components.globird_stats.coordinator.GloBirdClient", return_value=client) as coord_cls,
    ):
        client.flow_cls = flow_cls
        client.coord_cls = coord_cls
        yield client
