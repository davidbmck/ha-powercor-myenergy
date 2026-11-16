"""Synthetic interval-to-hour checks independent of Home Assistant installation."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest

from myenergy.model import Channel, ChannelKind, Quality, Reading

path = Path(__file__).resolve().parents[1] / "custom_components/powercor_myenergy/hourly.py"
spec = importlib.util.spec_from_file_location("myenergy_hourly_test", path)
hourly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hourly)

AEST = timezone(timedelta(hours=10))
IMPORT = Channel("meter-001", "channel-import", ChannelKind.GRID_IMPORT)
EXPORT = Channel("meter-001", "channel-export", ChannelKind.GRID_EXPORT)


class HourlyTests(unittest.TestCase):
    def test_directions_totals_and_incomplete_hour(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        readings = [
            Reading(IMPORT, start, start + timedelta(minutes=30), Decimal("0.125")),
            Reading(IMPORT, start + timedelta(minutes=30), start + timedelta(hours=1), Decimal("0.375")),
            Reading(EXPORT, start, start + timedelta(hours=1), Decimal("0.2")),
            Reading(IMPORT, start + timedelta(hours=1), start + timedelta(hours=1, minutes=30), Decimal("9")),
            Reading(IMPORT, start + timedelta(hours=1, minutes=30), start + timedelta(hours=2), None, Quality.MISSING),
            Reading(IMPORT, start + timedelta(hours=2), start + timedelta(hours=3), Decimal("0")),
        ]
        result = hourly.hourly_energy(reversed(readings))
        self.assertEqual(result[IMPORT], [(start, Decimal("0.500")), (start + timedelta(hours=2), Decimal(0))])
        self.assertEqual(result[EXPORT], [(start, Decimal("0.2"))])

    def test_aest_days_at_both_dst_changes_remain_24_utc_hours(self):
        for day in (date(2026, 4, 5), date(2026, 10, 4)):
            with self.subTest(day=day):
                start = datetime.combine(day, datetime.min.time(), AEST)
                readings = [Reading(IMPORT, start + hour * timedelta(hours=1),
                                    start + (hour + 1) * timedelta(hours=1), Decimal("1"))
                            for hour in range(24)]
                rows = hourly.hourly_energy(readings)[IMPORT]
                self.assertEqual(len(rows), 24)
                self.assertEqual(rows[0][0].tzinfo, timezone.utc)
                self.assertEqual(rows[-1][0] - rows[0][0], timedelta(hours=23))

    def test_overlapping_intervals_fail_instead_of_double_counting(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        readings = [
            Reading(IMPORT, start, start + timedelta(minutes=30), Decimal("1")),
            Reading(IMPORT, start, start + timedelta(hours=1), Decimal("1")),
        ]
        with self.assertRaises(ValueError):
            hourly.hourly_energy(readings)


if __name__ == "__main__":
    unittest.main()
