"""GloBird energy statistics: hourly grid import (kWh) and cost (AUD) for the Energy panel.

Reads the data the globird_ha integration (bolagnaise) already fetches, which holds 21 days of half-hourly smart-meter
reads and per-day costs, and writes them as external statistics dated to the hour they belong to. Each run continues
the running totals from what is already stored, so history accumulates past the portal's rolling window.
Replaces `hactl energy-sync` (daily resolution, ran outside HA with a token). Configure with `globird_stats:`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics, statistics_during_period
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, UnitOfEnergy
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .series import hourly_cost, hourly_usage, merge, parse_day

DOMAIN = "globird_stats"
SOURCE_DOMAIN = "globird_ha"
INTERVAL = timedelta(minutes=30)
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    last: dict[str, object] = {}

    async def run(*_):
        for coordinator in list(hass.data.get(SOURCE_DOMAIN, {}).values()):
            data = getattr(coordinator, "data", None)
            if not isinstance(data, dict):
                continue
            for service_id, svc in (data.get("service_data") or {}).items():
                key = f"{service_id}"
                fingerprint = (repr((svc.get("usage") or {}).get("data")), repr((svc.get("cost") or {}).get("data")))
                if last.get(key) == fingerprint:
                    continue
                try:
                    if await _sync_service(hass, service_id, svc):
                        last[key] = fingerprint
                except Exception:
                    _LOGGER.exception("GloBird statistics sync failed for service %s", service_id)

    async def start(*_):
        await run()
        async_track_time_interval(hass, run, INTERVAL)

    if hass.state is CoreState.running:
        await start()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, start)
    return True


async def _sync_service(hass: HomeAssistant, service_id: str, svc: dict) -> bool:
    if (svc.get("service") or {}).get("serviceType") != "Power":
        return False
    rows = (svc.get("usage") or {}).get("data")
    if not isinstance(rows, list) or not any("usageArray" in r for r in rows if isinstance(r, dict)):
        return False  # basic meter: no interval data
    usage = hourly_usage(rows)
    incomplete = {parse_day(d if isinstance(d, str) else d.get("date", ""))
                  for d in (svc.get("cost_summary") or {}).get("incomplete_days", []) if d}
    cost = hourly_cost((svc.get("cost") or {}).get("data") or [], usage, incomplete)
    for kind, fresh, name, unit, unit_class in (
        ("grid_import", usage, "GloBird grid import", UnitOfEnergy.KILO_WATT_HOUR, "energy"),
        ("grid_cost", cost, "GloBird grid cost", "AUD", None),
    ):
        sid = f"{DOMAIN}:{kind}_{service_id}"
        stored = await _stored(hass, sid, min(fresh) if fresh else None)
        stats = merge(fresh, stored)
        if not stats:
            continue
        meta = StatisticMetaData(mean_type=StatisticMeanType.NONE, has_sum=True, name=name, source=DOMAIN,
                                 statistic_id=sid, unit_class=unit_class, unit_of_measurement=unit)
        async_add_external_statistics(hass, meta, stats)
        _LOGGER.info("%s: %d hours %s .. %s, total %.2f %s", sid, len(stats), stats[0]["start"].isoformat(),
                     stats[-1]["start"].isoformat(), stats[-1]["sum"], unit)
    return True


async def _stored(hass: HomeAssistant, sid: str, first: datetime | None) -> list[tuple[datetime, float, float]]:
    """Stored hourly rows from a week before the fresh window onward (the last row before it carries the base sum)."""
    if first is None:
        return []
    since = first - timedelta(days=7)
    res = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, since, None, {sid}, "hour", None, {"state", "sum"})
    out = []
    for r in res.get(sid, []):
        s = r["start"]
        s = datetime.fromtimestamp(s, timezone.utc) if isinstance(s, (int, float)) else s
        out.append((s, r.get("state"), r.get("sum")))
    if not out or out[0][0] >= first:
        # Nothing stored just before the window: look further back for the last row (host off > a week, or first run).
        res = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, first - timedelta(days=3650), first, {sid}, "hour", None, {"sum"})
        prev = res.get(sid, [])
        if prev:
            s = prev[-1]["start"]
            s = datetime.fromtimestamp(s, timezone.utc) if isinstance(s, (int, float)) else s
            out.insert(0, (s, 0.0, prev[-1].get("sum")))
    return out
