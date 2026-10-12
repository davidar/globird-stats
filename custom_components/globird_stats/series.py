"""Pure functions: GloBird portal payloads -> hourly Energy-panel statistics. No Home Assistant imports.

Interval data is in NEM market time (AEST, UTC+10, no daylight saving): every day has 48 half-hours,
including the October and April changeover days. Each pair of half-hours is one UTC-aligned hour.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

AEST = timezone(timedelta(hours=10))


def parse_day(s: str) -> date:
    return date.fromisoformat(s[:10].replace("/", "-"))


def day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=AEST)


def hourly_usage(usage_rows: list[dict], suffix_prefix: str = "E") -> dict[datetime, float]:
    """kWh per UTC hour from smart-meter rows (one row per day per register; 48 half-hour values)."""
    out: dict[datetime, float] = {}
    for row in usage_rows:
        arr = row.get("usageArray")
        if not str(row.get("suffix") or "").startswith(suffix_prefix) or not isinstance(arr, list) or len(arr) != 48:
            continue
        t0 = day_start(parse_day(row["readDate"]))
        for i in range(24):
            h = (t0 + timedelta(hours=i)).astimezone(timezone.utc)
            out[h] = out.get(h, 0.0) + float(arr[2 * i] or 0) + float(arr[2 * i + 1] or 0)
    return out


def hourly_cost(cost_rows: list[dict], usage: dict[datetime, float], skip_days: set[date]) -> dict[datetime, float]:
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
        hours = [(day_start(d) + timedelta(hours=i)).astimezone(timezone.utc) for i in range(24)]
        if d in skip_days or any(h not in usage for h in hours):
            continue
        kwh = sum(usage[h] for h in hours)
        for h in hours:
            out[h] = 0.0
        use_amt = cats.pop("USAGE", 0.0)
        for h in hours:
            out[h] += use_amt * usage[h] / kwh if kwh > 0 else 0.0
        if kwh <= 0:
            out[hours[0]] += use_amt
        out[hours[0]] += sum(cats.values())
    return out


def merge(fresh: dict[datetime, float], stored: list[tuple[datetime, float, float]]) -> list[dict]:
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
    rows, total = [], base
    for start in sorted(values):
        total += values[start]
        rows.append({"start": start, "state": round(values[start], 4), "sum": round(total, 4)})
    return rows
