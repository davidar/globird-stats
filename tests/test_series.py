"""Unit tests for the pure series maths. No Home Assistant needed; all values are invented."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.globird_stats.series import (
    AEST,
    day_start,
    hourly_cost,
    hourly_usage,
    incomplete_cost_days,
    latest_complete_day,
    merge,
    parse_day,
)


def usage_row(
    day: str,
    suffix: str = "E1",
    arr: list | None = None,
    charge_type: str = "Usage",
    **extra,
) -> dict:
    arr = arr if arr is not None else [0.25] * 48
    row = {
        "readDate": day,
        "suffix": suffix,
        "usageArray": arr,
        "usage": sum(v or 0 for v in arr),
        "chargeType": charge_type,
        "chargeCategoryCode": "USAGE",
        "meterStatus": "Energized",
        "minQualityMethod": "A",
    }
    row.update(extra)
    return row


def ramp() -> list[float]:
    """Half-hour i carries i/100 kWh, so hour h = (4h + 1) / 100."""
    return [i / 100 for i in range(48)]


def cost_rows(day: str, usage_amt: float, supply_amt: float, missing: bool = False) -> list[dict]:
    return [
        {
            "chargeCategory": "USAGE",
            "chargeType": None,
            "date": day,
            "amount": usage_amt,
            "quantity": 9.9,
            "isMeterReadMissingOnDate": missing,
        },
        {
            "chargeCategory": "SUPPLY",
            "chargeType": None,
            "date": day,
            "amount": supply_amt,
            "quantity": 1,
            "isMeterReadMissingOnDate": missing,
        },
    ]


def utc_hours(d: date) -> list[datetime]:
    return [(day_start(d) + timedelta(hours=i)).astimezone(UTC) for i in range(24)]


# --- parsing --------------------------------------------------------------------------------------------------


def test_parse_day_formats() -> None:
    assert parse_day("2026/09/21") == date(2026, 9, 21)
    assert parse_day("2026-09-21") == date(2026, 9, 21)
    assert parse_day("2026-09-21T00:00:00") == date(2026, 9, 21)
    assert day_start(date(2026, 9, 21)) == datetime(2026, 9, 20, 14, tzinfo=UTC)
    assert day_start(date(2026, 9, 21)).utcoffset() == AEST.utcoffset(None)


# --- hourly_usage ---------------------------------------------------------------------------------------------


def test_flat_day_sums_pairs_onto_utc_hours() -> None:
    out = hourly_usage([usage_row("2026/09/21", arr=ramp())])
    hours = utc_hours(date(2026, 9, 21))
    assert sorted(out) == hours
    assert hours[0] == datetime(2026, 9, 20, 14, tzinfo=UTC)
    assert hours[-1] == datetime(2026, 9, 21, 13, tzinfo=UTC)
    for h, start in enumerate(hours):
        assert out[start] == pytest.approx((4 * h + 1) / 100)
    assert sum(out.values()) == pytest.approx(sum(ramp()))


def test_dst_start_day_still_24_hours() -> None:
    # 4 Oct 2026: Victoria moves to AEDT at 02:00 local, but NEM data stays on AEST, so 48 values -> 24 hours.
    out = hourly_usage([usage_row("2026/10/04", arr=ramp())])
    assert len(out) == 24
    assert min(out) == datetime(2026, 10, 3, 14, tzinfo=UTC)
    assert max(out) == datetime(2026, 10, 4, 13, tzinfo=UTC)
    deltas = {b - a for a, b in zip(sorted(out), sorted(out)[1:], strict=False)}
    assert deltas == {timedelta(hours=1)}
    assert sum(out.values()) == pytest.approx(sum(ramp()))


def test_tou_duplicate_rows_counted_once_registers_add() -> None:
    e1 = [0.5] * 48
    e2 = [0.1] * 48
    rows = [
        usage_row("2026/09/21", "E1", e1, "Peak Usage"),
        usage_row("2026/09/21", "E1", e1, "Off Peak Usage"),
        usage_row("2026/09/21", "E1", e1, "Shoulder Usage"),
        usage_row("2026/09/21", "E2", e2, "Controlled Load"),
        usage_row("2026/09/21", "E2", e2, "Controlled Load"),
    ]
    out = hourly_usage(rows)
    assert len(out) == 24
    assert all(v == pytest.approx(1.0 + 0.2) for v in out.values())
    assert sum(out.values()) == pytest.approx(24 * 1.2)


def test_same_register_on_different_days_both_count() -> None:
    out = hourly_usage([usage_row("2026/09/21"), usage_row("2026/09/22")])
    assert len(out) == 48


def test_export_registers_ignored() -> None:
    rows = [
        usage_row("2026/09/21", "E1", [0.25] * 48),
        usage_row("2026/09/21", "B1", [9.0] * 48),
    ]
    out = hourly_usage(rows)
    assert all(v == pytest.approx(0.5) for v in out.values())
    # The prefix is the hook for export later.
    assert all(v == pytest.approx(18.0) for v in hourly_usage(rows, suffix_prefix="B").values())


def test_short_or_missing_arrays_skipped() -> None:
    rows = [
        usage_row("2026/09/21", arr=[0.25] * 47),
        {"readDate": "2026/09/22", "suffix": "E1", "usage": 3.0},
        usage_row("2026/09/23", arr=None) | {"usageArray": None},
        usage_row("2026/09/24", arr=[0.25] * 48),
    ]
    out = hourly_usage(rows)
    assert sorted(out) == utc_hours(date(2026, 9, 24))


def test_short_tou_row_does_not_shadow_full_one() -> None:
    rows = [
        usage_row("2026/09/21", arr=[1.0] * 10, charge_type="Peak"),
        usage_row("2026/09/21", charge_type="Off Peak"),
    ]
    out = hourly_usage(rows)
    assert len(out) == 24
    assert all(v == pytest.approx(0.5) for v in out.values())


def test_null_intervals_count_as_zero() -> None:
    arr = [0.25] * 48
    arr[0] = None
    out = hourly_usage([usage_row("2026/09/21", arr=arr)])
    assert out[utc_hours(date(2026, 9, 21))[0]] == pytest.approx(0.25)


# --- hourly_cost ----------------------------------------------------------------------------------------------


def test_cost_spread_by_kwh_supply_in_first_hour() -> None:
    d = date(2026, 9, 21)
    usage = hourly_usage([usage_row("2026/09/21", arr=ramp())])
    out = hourly_cost(cost_rows("2026/09/21", 7.37, 1.21), usage, set())
    hours = utc_hours(d)
    assert sorted(out) == hours
    kwh = sum(usage.values())
    for h in hours[1:]:
        assert out[h] == pytest.approx(7.37 * usage[h] / kwh)
    assert out[hours[0]] == pytest.approx(7.37 * usage[hours[0]] / kwh + 1.21)
    assert round(sum(out.values()), 2) == round(7.37 + 1.21, 2)


def test_cost_zero_usage_books_everything_in_first_hour() -> None:
    usage = hourly_usage([usage_row("2026/09/21", arr=[0.0] * 48)])
    out = hourly_cost(cost_rows("2026/09/21", 0.10, 1.21), usage, set())
    first = utc_hours(date(2026, 9, 21))[0]
    assert out[first] == pytest.approx(1.31)
    assert sum(out.values()) == pytest.approx(1.31)


def test_cost_day_missing_usage_hour_left_out() -> None:
    usage = hourly_usage([usage_row("2026/09/21"), usage_row("2026/09/22")])
    del usage[utc_hours(date(2026, 9, 22))[5]]
    rows = cost_rows("2026/09/21", 2.0, 1.0) + cost_rows("2026/09/22", 2.0, 1.0)
    out = hourly_cost(rows, usage, set())
    assert sorted(out) == utc_hours(date(2026, 9, 21))


def test_cost_day_without_any_usage_left_out() -> None:
    usage = hourly_usage([usage_row("2026/09/21")])
    out = hourly_cost(
        cost_rows("2026/09/21", 2.0, 1.0) + cost_rows("2026/09/23", 2.0, 1.0),
        usage,
        set(),
    )
    assert sorted(out) == utc_hours(date(2026, 9, 21))


def test_cost_skip_days_left_out() -> None:
    usage = hourly_usage([usage_row("2026/09/21"), usage_row("2026/09/22")])
    rows = cost_rows("2026/09/21", 2.0, 1.0) + cost_rows("2026/09/22", 2.0, 1.0, missing=True)
    out = hourly_cost(rows, usage, incomplete_cost_days(rows))
    assert sorted(out) == utc_hours(date(2026, 9, 21))


# --- incomplete_cost_days / latest_complete_day ---------------------------------------------------------------


def test_incomplete_cost_days() -> None:
    rows = cost_rows("2026/09/21", 2.0, 1.0) + cost_rows("2026/09/22", 2.0, 1.0, missing=True)
    rows.append(
        {
            "chargeCategory": "USAGE",
            "date": "2026/09/23",
            "amount": 1.0,
            "isMeterReadMissingOnDate": True,
        }
    )
    rows.append(
        {
            "chargeCategory": "SUPPLY",
            "date": "2026/09/23",
            "amount": 1.0,
            "isMeterReadMissingOnDate": False,
        }
    )
    rows.append({"chargeCategory": "SUPPLY", "date": "2026/09/24", "amount": 1.0})  # flag absent = complete
    assert incomplete_cost_days(rows) == {date(2026, 9, 22), date(2026, 9, 23)}
    assert incomplete_cost_days([]) == set()


def test_latest_complete_day() -> None:
    rows = [
        usage_row("2026/09/20"),
        usage_row("2026/09/21", "E1", charge_type="Peak"),
        usage_row("2026/09/21", "E1", charge_type="Off Peak"),
        usage_row("2026/09/22", arr=[0.1] * 30),  # partial day
        usage_row("2026/09/23", "B1"),  # export only
        {"readDate": "2026/09/24", "suffix": "E1"},  # no array
    ]
    assert latest_complete_day(rows) == date(2026, 9, 21)
    assert latest_complete_day(rows, suffix_prefix="B") == date(2026, 9, 23)
    assert latest_complete_day([]) is None
    assert latest_complete_day([usage_row("2026/09/22", arr=[0.1] * 30)]) is None


# --- merge ----------------------------------------------------------------------------------------------------


def test_merge_empty_fresh() -> None:
    assert merge({}, [(datetime(2026, 9, 1, tzinfo=UTC), 1.0, 1.0)]) == []


def test_merge_no_stored_starts_at_zero() -> None:
    t = datetime(2026, 9, 1, tzinfo=UTC)
    rows = merge({t: 1.0, t + timedelta(hours=1): 2.0}, [])
    assert [r["sum"] for r in rows] == [1.0, 3.0]


def test_merge_continues_base_from_last_stored_before_window() -> None:
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    stored = [(t0 + timedelta(hours=i), 1.0, 100.0 + i + 1) for i in range(5)]  # sums 101..105
    fresh = {t0 + timedelta(hours=5): 0.5, t0 + timedelta(hours=6): 0.25}
    rows = merge(fresh, stored)
    assert [r["start"] for r in rows] == sorted(fresh)
    assert [r["sum"] for r in rows] == [105.5, 105.75]


def test_merge_rolled_window() -> None:
    # Stored covers days 1-7, fresh covers days 5-12 with one stored hour on day 6 missing from fresh.
    day0 = datetime(2026, 9, 1, tzinfo=UTC)
    stored_hours = [day0 + timedelta(hours=i) for i in range(7 * 24)]
    stored, running = [], 1000.0
    for i, h in enumerate(stored_hours):
        running += 1.0 + (i % 3) / 10
        stored.append((h, 1.0 + (i % 3) / 10, running))
    window_start = day0 + timedelta(days=4)
    gap = day0 + timedelta(days=5, hours=3)
    fresh = {window_start + timedelta(hours=i): 0.7 for i in range(8 * 24) if window_start + timedelta(hours=i) != gap}

    rows = merge(fresh, stored)
    by_start = {r["start"]: r for r in rows}

    # Nothing before the window is re-imported; the base is the last stored sum before it.
    assert min(by_start) == window_start
    base = next(total for start, _s, total in reversed(stored) if start < window_start)
    assert rows[0]["sum"] == pytest.approx(base + 0.7)

    # The stored hour fresh lacks is kept with its stored state.
    gap_state = next(s for start, s, _t in stored if start == gap)
    assert by_start[gap]["state"] == pytest.approx(gap_state)

    # Every fresh hour is present, sorted, sums continuous and non-decreasing.
    assert set(fresh) <= set(by_start)
    assert len(rows) == len(fresh) + 1
    starts = [r["start"] for r in rows]
    assert starts == sorted(starts)
    prev = base
    for r in rows:
        assert r["sum"] == pytest.approx(prev + r["state"], abs=1e-3)
        assert r["sum"] >= prev
        prev = r["sum"]
    assert rows[-1]["start"] == day0 + timedelta(days=12) - timedelta(hours=1)
