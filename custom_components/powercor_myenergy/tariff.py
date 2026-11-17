"""Dated per-kWh prices applied to original intervals, independent of HA APIs."""

from bisect import bisect_left
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

MODELS = ("flat", "two_rate", "three_rate", "free_midday", "midday_saver", "overnight", "block")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ALL_DAYS = list(range(7))


def price(value):
    """Keep decimal prices exact; zero is a price, never an unset value."""
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid_price") from None
    if not result.is_finite() or result < 0:
        raise ValueError("invalid_price")
    return result


def minute(value):
    parts = value.split(":")
    hour, minutes = int(parts[0]), int(parts[1])
    if not 0 <= hour < 24 or not 0 <= minutes < 60 or minutes % 5 or any(int(v) for v in parts[2:]):
        raise ValueError("invalid_time")
    return hour * 60 + minutes


def preset(model):
    """Editable time examples, not retailer prices or a claim of eligibility."""
    bands = []
    if model in ("two_rate", "three_rate", "midday_saver"):
        bands.append({"name": "peak", "start": "15:00:00" if model == "three_rate" else "16:00:00",
                      "end": "21:00:00", "days": list(range(5)) if model == "three_rate" else ALL_DAYS})
    if model in ("three_rate", "midday_saver"):
        bands.append({"name": "shoulder", "start": "07:00:00" if model == "three_rate" else "14:00:00",
                      "end": "22:00:00" if model == "three_rate" else "16:00:00", "days": ALL_DAYS})
    if model in ("free_midday", "midday_saver"):
        bands.insert(0, {"name": "free", "start": "11:00:00", "end": "14:00:00", "days": ALL_DAYS})
    if model == "overnight":
        bands.insert(0, {"name": "overnight", "start": "00:00:00", "end": "06:00:00", "days": ALL_DAYS})
    return bands


def validate_plan(plan):
    price(plan["import_price"])
    ZoneInfo(plan["timezone"])
    for key in ("export_price", "controlled_load_price"):
        if plan.get(key) is not None:
            price(plan[key])
    for band in plan["bands"]:
        price(band["price"])
        if minute(band["start"]) == minute(band["end"]) or not band["days"] or any(day not in ALL_DAYS for day in band["days"]):
            raise ValueError("invalid_time")
    for key in ("import_block", "export_block"):
        if block := plan.get(key):
            if price(block["kwh"]) <= 0 or block["reset"] not in ("daily", "monthly", "quarterly"):
                raise ValueError("invalid_block")
            price(block["after_price"])
            date.fromisoformat(block["anchor"])


def period_bounds(day, block):
    """Local half-open billing cycle; month-end anchors clamp without drifting."""
    if block["reset"] == "daily":
        return day, day + timedelta(days=1)
    anchor = date.fromisoformat(block["anchor"])
    step = 1 if block["reset"] == "monthly" else 3
    month_index = lambda d: d.year * 12 + d.month - 1
    index = month_index(anchor) + ((month_index(day) - month_index(anchor)) // step) * step

    def at(index):
        year, month = divmod(index, 12)
        return date(year, month + 1, min(anchor.day, monthrange(year, month + 1)[1]))

    if at(index) > day:
        index -= step
    return at(index), at(index + step)


def request_dates(settings, start, end, today):
    """Fetch whole affected block cycles, plus the adjacent NEM day for DST."""
    left, right = start, end
    aest = timezone(timedelta(hours=10))
    for plan in settings.get("plans", []):
        zone = ZoneInfo(plan["timezone"])
        for key in ("import_block", "export_block"):
            if block := plan.get(key):
                local_start = datetime.combine(start, time.min, aest).astimezone(zone).date()
                local_end = (datetime.combine(end + timedelta(days=1), time.min, aest) - timedelta(microseconds=1)).astimezone(zone).date()
                begin, _ = period_bounds(local_start, block)
                _, finish = period_bounds(local_end, block)
                left = min(left, datetime.combine(begin, time.min, zone).astimezone(aest).date())
                right = max(right, (datetime.combine(finish, time.min, zone).astimezone(aest) - timedelta(microseconds=1)).date())
    return left, min(right, today - timedelta(days=1))


def plan_at(settings, instant):
    for plan in reversed(settings["plans"]):
        if plan["effective_from"] is None or instant >= datetime.fromisoformat(plan["effective_from"]):
            return plan
    raise ValueError("No rate covers this interval")


def interval_price(plan, instant, kind):
    if kind == "grid_export":
        return None if plan.get("export_price") is None else price(plan["export_price"])
    if kind == "controlled_load":
        return None if plan.get("controlled_load_price") is None else price(plan["controlled_load_price"])
    local = instant.astimezone(ZoneInfo(plan["timezone"]))
    clock = local.hour * 60 + local.minute
    for band in plan["bands"]:
        start, end = minute(band["start"]), minute(band["end"])
        # After-midnight part of a wrapping window belongs to its start day.
        day = (local.weekday() - 1) % 7 if start > end and clock < end else local.weekday()
        if day in band["days"] and (start <= clock < end if start < end else clock >= start or clock < end):
            return price(band["price"])
    return price(plan["import_price"])


def hourly_costs(readings, hours, settings):
    """Cost complete source hours; never spread hourly kWh across tariff bands.

    A block allowance is shared by a meter's channels of the same direction.
    Missing intervals before a block calculation make that block position unknown;
    omit those costs until a complete retrieval can reconcile them.
    """
    groups = defaultdict(list)
    for reading in readings:
        groups[(reading.channel.meter_id, reading.channel.kind.value)].append(reading)
    result = defaultdict(dict)
    incomplete = set()
    for (_, kind), group in groups.items():
        group.sort(key=lambda r: (r.start, r.end, r.channel.channel_id))
        # Prefix totals allow each dated plan to use its own reset calendar,
        # including when a rate changes partway through a billing cycle.
        starts = sorted({r.start for r in group})
        by_start = defaultdict(list)
        for reading in group:
            by_start[reading.start].append(reading)
        prefix = [Decimal(0)]
        coverage = []
        channels = {r.channel for r in group}
        for instant in starts:
            batch = by_start[instant]
            prefix.append(prefix[-1] + sum((r.kwh or Decimal(0) for r in batch), Decimal(0)))
            coverage.append((instant, min(r.end for r in batch),
                             len({r.channel for r in batch}) == len(channels) and all(r.kwh is not None for r in batch)))
        bad = [0]
        for index, (instant, end, complete) in enumerate(coverage):
            bad.append(bad[-1] + int(not complete or (index > 0 and coverage[index - 1][1] != instant)))
        for index, instant in enumerate(starts):
            batch = by_start[instant]
            plan = plan_at(settings, instant)
            block = plan.get("export_block" if kind == "grid_export" else "import_block") if kind != "controlled_load" else None
            used = Decimal(0)
            if block:
                zone = ZoneInfo(plan["timezone"])
                begin, _ = period_bounds(instant.astimezone(zone).date(), block)
                boundary = datetime.combine(begin, time.min, zone).astimezone(timezone.utc)
                left = bisect_left(starts, boundary)
                if (left >= len(starts) or starts[left] != boundary or not coverage[left][2]
                        or bad[index + 1] != bad[left + 1]):
                    incomplete.update((r.channel, r.start.replace(minute=0, second=0, microsecond=0)) for r in batch)
                    continue
                used = prefix[index] - prefix[left]
            for reading in batch:
                hour = reading.start.replace(minute=0, second=0, microsecond=0)
                if reading.kwh is None:
                    continue
                rate = interval_price(plan, reading.start, kind)
                if rate is None:
                    continue
                cursor = reading.start + timedelta(minutes=5)
                while cursor < reading.end:
                    current = plan_at(settings, cursor)
                    if current is not plan or interval_price(current, cursor, kind) != rate:
                        raise ValueError("A tariff boundary falls inside a source interval")
                    cursor += timedelta(minutes=5)
                amount = reading.kwh * rate
                if block:
                    first = min(reading.kwh, max(Decimal(0), price(block["kwh"]) - used))
                    amount = first * rate + (reading.kwh - first) * price(block["after_price"])
                    used += reading.kwh
                result[reading.channel][hour] = result[reading.channel].get(hour, Decimal(0)) + amount
    return {channel: [(hour, values[hour]) for hour, _ in complete if hour in values and (channel, hour) not in incomplete]
            for channel, complete in hours.items() if (values := result.get(channel))}
