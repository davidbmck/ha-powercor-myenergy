"""Turn normalised interval kWh into complete UTC-hour energy totals."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal


def hourly_energy(readings):
    """Return complete hours by channel; omit gaps instead of inventing energy.

    HA's long-term statistics are hourly. An hour is usable only when its
    non-missing intervals cover it exactly once. NEM12 dates are fixed AEST,
    while Reading bounds are UTC, so DST needs no local-hour special case.
    """
    grouped = defaultdict(list)
    for reading in readings:
        hour = reading.start.replace(minute=0, second=0, microsecond=0)
        if reading.end > hour + timedelta(hours=1):
            raise ValueError("Interval crosses a UTC hour boundary")
        grouped[(reading.channel, hour)].append(reading)

    result = defaultdict(list)
    for (channel, hour), intervals in sorted(grouped.items(), key=lambda item: item[0][1]):
        cursor = hour
        energy = Decimal(0)
        complete = True
        for reading in sorted(intervals, key=lambda item: item.start):
            if reading.start < cursor:
                raise ValueError("Overlapping intervals in an hour")
            if reading.start > cursor or reading.kwh is None:
                complete = False
                break
            cursor = reading.end
            energy += reading.kwh
        if complete and cursor == hour + timedelta(hours=1):
            result[channel].append((hour, energy))
    return dict(result)
