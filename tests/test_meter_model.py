"""Synthetic interval examples, including both Victorian clock changes."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from myenergy.model import Channel, ChannelKind, Quality, Reading


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.channel = Channel("meter-001", "channel-import", ChannelKind.GRID_IMPORT)
        self.start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        self.end = self.start + timedelta(minutes=30)

    def test_zero_missing_and_estimated_energy_remain_distinct(self):
        zero = Reading(self.channel, self.start, self.end, Decimal("0"), Quality.ACTUAL)
        missing = Reading(self.channel, self.start, self.end, None, Quality.MISSING)
        estimate = Reading(self.channel, self.start, self.end, Decimal("0.125"), Quality.ESTIMATED)
        self.assertEqual(zero.kwh, Decimal("0"))
        self.assertIsNone(missing.kwh)
        self.assertEqual(estimate.kwh, Decimal("0.125"))
        for value, quality in ((None, Quality.ACTUAL), (Decimal("0"), Quality.MISSING)):
            with self.subTest(value=value, quality=quality), self.assertRaises(ValueError):
                Reading(self.channel, self.start, self.end, value, quality)

    def test_invalid_energy_is_rejected(self):
        for value in (Decimal("-1"), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity")):
            with self.subTest(value=str(value)), self.assertRaises(ValueError):
                Reading(self.channel, self.start, self.end, value)
        with self.assertRaises(TypeError):
            Reading(self.channel, self.start, self.end, 0.1)

    def test_naive_empty_and_reversed_periods_are_rejected(self):
        for start, end in (
            (self.start.replace(tzinfo=None), self.end),
            (self.start, self.end.replace(tzinfo=None)),
            (self.start, self.start),
            (self.end, self.start),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                Reading(self.channel, start, end, Decimal("0.25"))

    def test_spring_transition_has_no_artificial_missing_hour(self):
        melbourne = ZoneInfo("Australia/Melbourne")
        reading = Reading(
            self.channel,
            datetime(2026, 10, 4, 1, 30, tzinfo=melbourne),
            datetime(2026, 10, 4, 3, 0, tzinfo=melbourne),
            Decimal("0.25"), Quality.ACTUAL,
        )
        self.assertEqual(reading.start, datetime(2026, 10, 3, 15, 30, tzinfo=timezone.utc))
        self.assertEqual(reading.end, datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(reading.end - reading.start, timedelta(minutes=30))

    def test_autumn_repeated_hour_remains_two_distinct_intervals(self):
        melbourne = ZoneInfo("Australia/Melbourne")
        readings = [Reading(
            self.channel,
            datetime(2026, 4, 5, 2, 0, tzinfo=melbourne, fold=fold),
            datetime(2026, 4, 5, 2, 30, tzinfo=melbourne, fold=fold),
            Decimal("0.25"), Quality.ACTUAL,
        ) for fold in (0, 1)]
        self.assertNotEqual(readings[0].start, readings[1].start)
        self.assertEqual(readings[1].start - readings[0].start, timedelta(hours=1))
        for reading in readings:
            self.assertEqual(reading.end - reading.start, timedelta(minutes=30))
            self.assertIs(reading.start.tzinfo, timezone.utc)
        # A period crossing the backward clock change still has positive duration.
        crossing = Reading(
            self.channel,
            datetime(2026, 4, 5, 2, 30, tzinfo=melbourne, fold=0),
            datetime(2026, 4, 5, 2, 0, tzinfo=melbourne, fold=1),
            Decimal("0.25"),
        )
        self.assertEqual(crossing.end - crossing.start, timedelta(minutes=30))


if __name__ == "__main__":
    unittest.main()
