"""Hourly grid import (kWh) and cost (AUD) as external statistics for the Energy panel.

Each sync continues the running sum from the last stored row before the portal's window, so history accumulates
past the ~21 days the portal shows. Statistic ids are `globird_stats:grid_import_<account service id>` and
`globird_stats:grid_cost_<account service id>`; they must not change (Energy panel prefs and history point at them).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .const import DOMAIN, STAT_GRID_COST, STAT_GRID_IMPORT
from .series import hourly_cost, hourly_usage, incomplete_cost_days, merge

_LOGGER = logging.getLogger(__name__)


def statistic_id(kind: str, account_service_id: str) -> str:
    """Return the external statistic id for one service."""
    return f"{DOMAIN}:{kind}_{account_service_id}"


async def async_sync_service(
    hass: HomeAssistant,
    account_service_id: str,
    usage_rows: list[dict[str, Any]],
    cost_rows: list[dict[str, Any]],
) -> bool:
    """Import one service's usage and cost. False when there is no interval data (basic meter)."""
    if not any(isinstance(r, dict) and "usageArray" in r for r in usage_rows):
        return False
    usage = hourly_usage(usage_rows)
    cost = hourly_cost(cost_rows, usage, incomplete_cost_days(cost_rows))
    for kind, fresh, name, unit, unit_class in (
        (STAT_GRID_IMPORT, usage, "GloBird grid import", UnitOfEnergy.KILO_WATT_HOUR, "energy"),
        (STAT_GRID_COST, cost, "GloBird grid cost", "AUD", None),
    ):
        sid = statistic_id(kind, account_service_id)
        stored = await _stored(hass, sid, min(fresh) if fresh else None)
        stats = merge(fresh, stored)
        if not stats:
            continue
        meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=sid,
            unit_class=unit_class,
            unit_of_measurement=unit,
        )
        async_add_external_statistics(hass, meta, stats)
        _LOGGER.debug(
            "%s: %d hours %s .. %s, total %.2f %s",
            sid,
            len(stats),
            stats[0]["start"].isoformat(),
            stats[-1]["start"].isoformat(),
            stats[-1]["sum"],
            unit,
        )
    return True


def _as_datetime(start: Any) -> datetime:
    return datetime.fromtimestamp(start, UTC) if isinstance(start, (int, float)) else start


async def _stored(hass: HomeAssistant, sid: str, first: datetime | None) -> list[tuple[datetime, float, float]]:
    """Stored hourly rows from a week before the fresh window onward (the last row before it carries the base sum)."""
    if first is None:
        return []
    since = first - timedelta(days=7)
    res = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, since, None, {sid}, "hour", None, {"state", "sum"}
    )
    out = [(_as_datetime(r["start"]), r.get("state"), r.get("sum")) for r in res.get(sid, [])]
    if not out or out[0][0] >= first:
        # Nothing stored just before the window: look further back for the last row (host off > a week, or first run).
        res = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, first - timedelta(days=3650), first, {sid}, "hour", None, {"sum"}
        )
        prev = res.get(sid, [])
        if prev:
            out.insert(0, (_as_datetime(prev[-1]["start"]), 0.0, prev[-1].get("sum")))
    return out
