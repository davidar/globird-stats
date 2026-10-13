"""Data update coordinator: one portal login per config entry, polled hourly."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GloBirdAuthError, GloBirdClient, GloBirdError, PowerService, power_services
from .const import CONF_EMAIL, CONF_PASSWORD, COST_DAYS, DOMAIN, UPDATE_INTERVAL, USAGE_DAYS
from .series import AEST, latest_complete_day
from .statistics import async_sync_service

_LOGGER = logging.getLogger(__name__)

type GloBirdConfigEntry = ConfigEntry[GloBirdCoordinator]

_LIVE_METER_STATUS = ("energized", "energised", "active", "current", "connected")


@dataclass
class ServiceData:
    """One electricity service and its raw portal rows."""

    service: PowerService
    serial_number: str | None
    usage_rows: list[dict[str, Any]] = field(default_factory=list)  # [] for a basic meter
    cost_rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GloBirdData:
    """Everything one refresh produced."""

    account_id: str
    balance: float | None  # portal sign: amount owing, so negative = in credit
    max_refundable: float | None
    services: dict[str, ServiceData]  # key = account_service_id
    latest_data_day: date | None


def pick_meter(meters: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer an energised meter, then one whose read type is not Basic; keep portal order otherwise."""
    usable = [m for m in meters if isinstance(m, dict) and m.get("serialNumber")]
    if not usable:
        return None

    def rank(m: dict[str, Any]) -> tuple[int, int]:
        # The portal sends meterStatus on smart-meter read rows and serialStatus on readmeters rows (seen 13 Oct 2026).
        status = str(m.get("meterStatus") or m.get("serialStatus") or "").lower()
        read_type = str(m.get("meterReadType") or "").lower()
        return (0 if status.startswith(_LIVE_METER_STATUS) else 1, 1 if read_type == "basic" else 0)

    return min(usable, key=rank)


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class GloBirdCoordinator(DataUpdateCoordinator[GloBirdData]):
    """Fetch the account's balance and each electricity service's interval and cost rows."""

    config_entry: GloBirdConfigEntry

    def __init__(self, hass: HomeAssistant, entry: GloBirdConfigEntry) -> None:
        """Create the coordinator and its portal client (the client owns its own cookie jar)."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.client = GloBirdClient(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
        self._logged_in = False
        self._synced: dict[str, int] = {}

    async def async_shutdown(self) -> None:
        """Stop polling and close the portal session."""
        await super().async_shutdown()
        await self.client.async_close()

    async def _async_update_data(self) -> GloBirdData:
        try:
            data = await self._fetch()
        except GloBirdAuthError as err:  # includes GloBirdCaptchaRequired
            self._logged_in = False
            raise ConfigEntryAuthFailed(str(err) or "GloBird login rejected") from err
        except GloBirdError as err:
            raise UpdateFailed(f"GloBird portal error: {err}") from err
        await self._sync_statistics(data)
        return data

    async def _fetch(self) -> GloBirdData:
        if self._logged_in:
            user = await self.client.async_current_user()
        else:
            user = await self.client.async_login()
            self._logged_in = True

        accounts = [a for a in (user.get("accounts") or []) if isinstance(a, dict)]
        account_id = self.config_entry.unique_id or (str(accounts[0].get("accountId")) if accounts else "")

        today = datetime.now(AEST).date()
        services: dict[str, ServiceData] = {}
        for service in power_services(user):
            services[service.account_service_id] = await self._fetch_service(service, today)

        balance = max_refundable = None
        if account_id:
            bal = await self.client.async_balance(account_id) or {}
            balance = _float(bal.get("balance"))
            max_refundable = _float(bal.get("maxRefundableAmount"))

        days = [d for s in services.values() if (d := latest_complete_day(s.usage_rows)) is not None]
        return GloBirdData(
            account_id=account_id,
            balance=balance,
            max_refundable=max_refundable,
            services=services,
            latest_data_day=max(days) if days else None,
        )

    async def _fetch_service(self, service: PowerService, today: date) -> ServiceData:
        sid = service.account_service_id
        meter = pick_meter(await self.client.async_read_meters(sid))
        if meter is None:
            _LOGGER.debug("Service %s has no meter; skipping", sid)
            return ServiceData(service=service, serial_number=None)
        serial = str(meter["serialNumber"])
        identifier = service.identifier or meter.get("siteIdentifier") or meter.get("nmi")
        if not identifier:
            _LOGGER.warning("Service %s has no site identifier; skipping", sid)
            return ServiceData(service=service, serial_number=serial)
        identifier = str(identifier)

        usage = await self.client.async_smart_meter_reads(
            sid, identifier, serial, today - timedelta(days=USAGE_DAYS), today
        )
        if not any(isinstance(r, dict) and isinstance(r.get("usageArray"), list) for r in usage):
            # Basic (accumulation) meter: no interval data, so no statistics.
            return ServiceData(service=service, serial_number=serial)
        cost = await self.client.async_cost_detail(sid, identifier, today - timedelta(days=COST_DAYS), today)
        return ServiceData(service=service, serial_number=serial, usage_rows=usage, cost_rows=cost)

    async def _sync_statistics(self, data: GloBirdData) -> None:
        for sid, svc in data.services.items():
            if not svc.usage_rows:
                continue
            fingerprint = hash((repr(svc.usage_rows), repr(svc.cost_rows)))
            if self._synced.get(sid) == fingerprint:
                continue
            try:
                if await async_sync_service(self.hass, sid, svc.usage_rows, svc.cost_rows):
                    self._synced[sid] = fingerprint
            except Exception:
                _LOGGER.exception("GloBird statistics sync failed for service %s", sid)
