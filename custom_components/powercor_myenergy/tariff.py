"""Dated per-kWh prices applied to original intervals, independent of HA APIs."""

from bisect import bisect_left
from calendar import monthrange
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

MODELS = ("flat", "two_rate", "three_rate", "free_midday", "midday_saver", "overnight", "block")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ALL_DAYS = list(range(7))
CLOCKS = {"victorian": "Australia/Melbourne", "aest": "Etc/GMT-10"}


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
        bands.insert(0, {"name": "free", "price": "0", "start": "11:00:00", "end": "14:00:00", "days": ALL_DAYS})
    if model == "overnight":
        bands.insert(0, {"name": "overnight", "start": "00:00:00", "end": "06:00:00", "days": ALL_DAYS})
    return bands


def prepare_plan(plan):
    """Build one canonical snapshot for every form path before saving."""
    result = {key: deepcopy(value) for key, value in plan.items() if key in {
        "model", "export_model", "timezone", "effective_date", "effective_from",
        "import_price", "export_price", "bands", "import_block", "export_block",
        "controlled_load_price", "controlled_load_register",
    }}
    if result["model"] != "block":
        result.pop("import_block", None)
    if result["export_model"] != "block":
        result.pop("export_block", None)
    if result["export_model"] == "none":
        result["export_price"] = None
    if result.get("controlled_load_price") is None:
        result.pop("controlled_load_price", None)
        result.pop("controlled_load_register", None)
    for band in result["bands"]:
        if band["name"] == "free":
            band["price"] = "0"
    validate_plan(result)
    return result


def validate_plan(plan):
    price(plan["import_price"])
    if plan["timezone"] not in CLOCKS.values():
        raise ValueError("invalid_time")
    if plan.get("model") == "block" and (plan["bands"] or not plan.get("import_block")):
        raise ValueError("invalid_block")
    if plan.get("export_model") == "block" and (not plan.get("export_block") or plan.get("export_price") is None):
        raise ValueError("invalid_block")
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


def _merge_periods(periods):
    result = []
    for left, right in sorted(periods):
        if result and left <= result[-1][1]:
            result[-1] = (result[-1][0], max(right, result[-1][1]))
        else:
            result.append((left, right))
    return result


def block_for(plan, kind):
    if kind == "controlled_load":
        return None
    return plan.get("export_block" if kind == "grid_export" else "import_block")


def _cycle(instant, plan, block):
    zone = ZoneInfo(plan["timezone"])
    begin, end = period_bounds(instant.astimezone(zone).date(), block)
    return tuple(datetime.combine(day, time.min, zone).astimezone(timezone.utc) for day in (begin, end))


def history_periods(settings, start, end, today):
    """Return fetch dates and exact UTC periods to reconcile by direction.

    Context before a plan starts still counts towards its allowance. Whole NEM
    days outside the affected cycles are retrieval padding, not imported hours.
    Later dated plans are included when earlier corrections affect their cycle.
    """
    aest = timezone(timedelta(hours=10))
    begin = datetime.combine(start, time.min, aest).astimezone(timezone.utc)
    finish = datetime.combine(end + timedelta(days=1), time.min, aest).astimezone(timezone.utc)
    cutoff = datetime.combine(today, time.min, aest).astimezone(timezone.utc)
    fetch_left, fetch_right = begin, finish
    targets = {}
    plans = settings.get("plans", [])
    boundaries = [datetime.fromisoformat(plan["effective_from"]) if plan["effective_from"]
                  else datetime.min.replace(tzinfo=timezone.utc) for plan in plans] + [cutoff]
    for kind in ("grid_import", "grid_export", "controlled_load"):
        affected = [(begin, finish)]
        periods = []
        for index, plan in enumerate(plans):
            active_left, active_right = boundaries[index:index + 2]
            block = block_for(plan, kind)
            additions = []
            for left, right in _merge_periods(affected):
                if block:
                    left = _cycle(left, plan, block)[0]
                    right = _cycle(right - timedelta(microseconds=1), plan, block)[1]
                left, right = max(left, active_left), min(right, active_right, cutoff)
                if left >= right:
                    continue
                additions.append((left, right))
                context = _cycle(left, plan, block)[0] if block else left
                fetch_left, fetch_right = min(fetch_left, context), max(fetch_right, right)
            periods.extend(additions)
            affected.extend(additions)
        targets[kind] = _merge_periods(periods) if plans else [(begin, finish)]
    first = fetch_left.astimezone(aest).date()
    last = (fetch_right.astimezone(aest) - timedelta(microseconds=1)).date()
    return first, min(last, today - timedelta(days=1)), targets


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


def hourly_costs(readings, hours, settings, *, source_hours=None):
    """Price target hours; only block pricing builds meter-wide context.

    Time bands use original intervals. Two-block prices and reset/version
    boundaries are constant within a UTC hour, so their meter-hour total can
    be priced exactly even when channels have different interval lengths.
    """
    result = defaultdict(dict)
    targets = {channel: {hour for hour, _ in values} for channel, values in hours.items()}
    block_groups = set()
    for reading in readings:
        hour = reading.start.replace(minute=0, second=0, microsecond=0)
        if hour not in targets.get(reading.channel, ()) or reading.kwh is None:
            continue
        plan = plan_at(settings, reading.start)
        kind = reading.channel.kind.value
        if block_for(plan, kind):
            block_groups.add((reading.channel.meter_id, kind))
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
        result[reading.channel][hour] = result[reading.channel].get(hour, Decimal(0)) + reading.kwh * rate
    if block_groups:
        groups = defaultdict(list)
        for reading in readings:
            key = (reading.channel.meter_id, reading.channel.kind.value)
            if key in block_groups:
                groups[key].append(reading)
        for group in groups.values():
            for channel, values in _block_costs(group, source_hours if source_hours is not None else hours, targets, settings).items():
                result[channel].update(values)
    return {channel: sorted(values.items()) for channel, values in result.items()}


def _block_costs(readings, source_hours, targets, settings):
    """Track known usage and complete meter hours for a shared allowance."""
    channels = {reading.channel for reading in readings}
    kind = readings[0].channel.kind.value
    known = defaultdict(Decimal)
    for reading in readings:
        hour = reading.start.replace(minute=0, second=0, microsecond=0)
        known[hour] += reading.kwh or Decimal(0)
    complete = defaultdict(dict)
    for channel in channels:
        for hour, energy in source_hours.get(channel, []):
            complete[hour][channel] = energy
    timeline = sorted(known)
    prefix = [Decimal(0)]
    for hour in timeline:
        prefix.append(prefix[-1] + known[hour])
    result = defaultdict(dict)
    run_start = previous_end = None
    for index, hour in enumerate(timeline):
        values = complete[hour]
        whole = len(values) == len(channels)
        plan = plan_at(settings, hour)
        block = block_for(plan, kind)
        selected = {channel: energy for channel, energy in values.items() if hour in targets.get(channel, ())}
        if block and selected:
            if plan_at(settings, hour + timedelta(hours=1) - timedelta(microseconds=1)) is not plan:
                raise ValueError("A tariff boundary falls inside a source hour")
            rate = interval_price(plan, hour, kind)
            if rate is not None:
                after, limit = price(block["after_price"]), price(block["kwh"])
                boundary = _cycle(hour, plan, block)[0]
                used = prefix[index] - prefix[bisect_left(timeline, boundary)]
                exact = hour == boundary or (previous_end == hour and run_start is not None and run_start <= boundary)
                if used >= limit or rate == after:
                    for channel, energy in selected.items():
                        result[channel][hour] = energy * (after if used >= limit else rate)
                elif exact and whole:
                    total = sum(values.values(), Decimal(0))
                    first = min(total, limit - used)
                    amount = first * rate + (total - first) * after
                    # Shared thresholds have no physical channel ordering. Split
                    # each hour proportionally; assign any Decimal residual once.
                    remaining = amount
                    nonzero = sorted((channel for channel, energy in values.items() if energy), key=lambda channel: channel.channel_id)
                    for position, channel in enumerate(nonzero):
                        share = remaining if position == len(nonzero) - 1 else amount * values[channel] / total
                        if channel in selected:
                            result[channel][hour] = share
                        remaining -= share
                for channel, energy in selected.items():
                    if not energy:
                        result[channel][hour] = Decimal(0)
        if whole:
            if previous_end != hour or run_start is None:
                run_start = hour
            previous_end = hour + timedelta(hours=1)
        else:
            run_start = previous_end = None
    return result
