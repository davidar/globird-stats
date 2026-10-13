"""Pure functions: GloBird portal payloads -> hourly Energy-panel statistics. No Home Assistant imports.

Interval data is in NEM market time (AEST, UTC+10, no daylight saving): every day has 48 half-hours,
including the October and April changeover days. Each pair of half-hours is one UTC-aligned hour.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

AEST = timezone(timedelta(hours=10))
INTERVALS_PER_DAY = 48


def parse_day(s: str) -> date:
    """Portal date ("2026/09/21", "2026-09-21" or an ISO timestamp) -> date."""
    return date.fromisoformat(s[:10].replace("/", "-"))


def day_start(d: date) -> datetime:
    """Midnight of a NEM market day (AEST)."""
    return datetime(d.year, d.month, d.day, tzinfo=AEST)


def _register_days(usage_rows: list[dict[str, Any]], suffix_prefix: str) -> dict[tuple[date, str], list[Any]]:
    """One full-day interval array per (readDate, register suffix).

    On time-of-use plans the portal repeats a register's whole-day usageArray on every charge-type row (Peak,
    Off-peak, Shoulder), so the first complete array seen for a (day, suffix) pair stands for that register's day.
    Rows for other registers, or without a 48-value usageArray, are skipped.
    """
    out: dict[tuple[date, str], list[Any]] = {}
    for row in usage_rows:
        suffix = str(row.get("suffix") or "")
        arr = row.get("usageArray")
        if not suffix.startswith(suffix_prefix) or not isinstance(arr, list) or len(arr) != INTERVALS_PER_DAY:
            continue
        out.setdefault((parse_day(row["readDate"]), suffix), arr)
    return out


def hourly_usage(usage_rows: list[dict[str, Any]], suffix_prefix: str = "E") -> dict[datetime, float]:
    """kWh per UTC hour from smart-meter rows. Each register counts once per day; distinct registers add."""
    out: dict[datetime, float] = {}
    for (d, _suffix), arr in _register_days(usage_rows, suffix_prefix).items():
        t0 = day_start(d)
        for i in range(24):
            h = (t0 + timedelta(hours=i)).astimezone(UTC)
            out[h] = out.get(h, 0.0) + float(arr[2 * i] or 0) + float(arr[2 * i + 1] or 0)
    return out


def latest_complete_day(usage_rows: list[dict[str, Any]], suffix_prefix: str = "E") -> date | None:
    """Newest readDate with a full 48-value import array, or None."""
    return max((d for d, _suffix in _register_days(usage_rows, suffix_prefix)), default=None)


def incomplete_cost_days(cost_rows: list[dict[str, Any]]) -> set[date]:
    """Days the portal flags as incomplete: any cost row with isMeterReadMissingOnDate true."""
    return {parse_day(r["date"]) for r in cost_rows if r.get("isMeterReadMissingOnDate")}


def hourly_cost(
    cost_rows: list[dict[str, Any]], usage: dict[datetime, float], skip_days: set[date]
) -> dict[datetime, float]:
    """AUD per UTC hour. The day's USAGE charge is spread over its hours in proportion to metered kWh (exact on a
    flat rate, an approximation on time-of-use); SUPPLY and any other category is booked in the day's first hour.
    Days without full interval data, or listed in skip_days (portal says incomplete), are left out."""
    days: dict[date, dict[str, float]] = {}
    for r in cost_rows:
        d = parse_day(r["date"])
        cat = r.get("chargeCategory") or "OTHER"
        days.setdefault(d, {})
        days[d][cat] = days[d].get(cat, 0.0) + float(r.get("amount") or 0)
    out: dict[datetime, float] = {}
    for d, cats in days.items():
        hours = [(day_start(d) + timedelta(hours=i)).astimezone(UTC) for i in range(24)]
        if d in skip_days or any(h not in usage for h in hours):
            continue
        kwh = sum(usage[h] for h in hours)
        use_amt = cats.pop("USAGE", 0.0)
        for h in hours:
            out[h] = use_amt * usage[h] / kwh if kwh > 0 else 0.0
        if kwh <= 0:
            out[hours[0]] += use_amt
        out[hours[0]] += sum(cats.values())
    return out


def merge(fresh: dict[datetime, float], stored: list[tuple[datetime, float, float]]) -> list[dict[str, Any]]:
    """Rows to import: fresh hours, plus every stored hour after the first fresh one, with sums continued from the
    last stored row before the window. stored = [(start_utc, state, sum)] sorted by start. Hours already stored
    with the same value are re-imported anyway (cheap, and it repairs any sum drift after the window start)."""
    if not fresh:
        return []
    first = min(fresh)
    base = 0.0
    values: dict[datetime, float] = {}
    for start, state, total in stored:
        if start < first:
            base = total or 0.0
        else:
            values[start] = state or 0.0
    values.update(fresh)
    rows: list[dict[str, Any]] = []
    total = base
    for start in sorted(values):
        total += values[start]
        rows.append({"start": start, "state": round(values[start], 4), "sum": round(total, 4)})
    return rows
