"""Synthetic NEM12 shapes; every interval value is invented."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from myenergy import ChannelKind, DataUnavailable, Quality, UnexpectedResponse
from myenergy.nem12 import parse_nem12

DAY = date(2026, 10, 4)


def stream(suffix="E1", day=DAY, code="A", *, unit="KWH", value="0.25", events=(), minutes=30):
    count = 1440 // minutes
    return (f"200,nmi-supply,E1B1,register-example,{suffix},,meter-example,{unit},{minutes},\n"
            + ",".join(["300", day.strftime("%Y%m%d")] + [value] * count + [code, "", "", "20261006120000", ""]) + "\n"
            + "".join(event + "\n" for event in events))


def nem12(*streams):
    return "100,NEM12,202610061200,participant-example,recipient-example\n" + "".join(streams) + "900\n"


def parse(text, day=DAY, **kwargs):
    return parse_nem12(text, nmi="nmi-supply", start=day, end=day, **kwargs)


class NEM12Tests(unittest.TestCase):
    def test_channels_units_quality_and_stable_identity_across_corrections(self):
        text = nem12(stream(), stream("B1", unit="WH", value="125", code="E14"))
        readings = parse(text)
        self.assertEqual(len(readings), 96)
        imports = [r for r in readings if r.channel.kind == ChannelKind.GRID_IMPORT]
        exports = [r for r in readings if r.channel.kind == ChannelKind.GRID_EXPORT]
        self.assertEqual(imports[0].kwh, Decimal("0.25"))
        self.assertEqual(exports[0].kwh, Decimal("0.125"))
        self.assertEqual(exports[0].quality, Quality.ESTIMATED)
        self.assertEqual(imports[0].channel.meter_id, exports[0].channel.meter_id)
        revised = parse(nem12(stream(value="0.5", code="F14")))
        self.assertEqual(revised[0].channel, imports[0].channel)
        self.assertNotIn("meter-example", repr(imports[0].channel))
        self.assertEqual(revised[0].quality, Quality.SUBSTITUTED)

    def test_discovery_check_digit_does_not_change_stream_identity(self):
        text = nem12(stream().replace("nmi-supply", "1234567890"))
        a = parse_nem12(text, nmi="1234567890", start=DAY, end=DAY)
        b = parse_nem12(text, nmi="12345678901", start=DAY, end=DAY)
        self.assertEqual(a, b)

    def test_interval_bounds_use_fixed_aest_across_both_dst_transitions(self):
        melbourne = ZoneInfo("Australia/Melbourne")
        for day in (date(2026, 4, 5), DAY):
            with self.subTest(day=day):
                readings = parse(nem12(stream(day=day)), day)
                self.assertEqual(len(readings), 48)
                self.assertEqual(readings[0].start, datetime(day.year, day.month, day.day, tzinfo=timezone(timedelta(hours=10))))
                self.assertTrue(all(r.end - r.start == timedelta(minutes=30) for r in readings))
                local = [r.start.astimezone(melbourne) for r in readings]
                if day.month == 4:
                    repeated = [d for d in local if d.hour == 2 and d.minute == 0]
                    self.assertEqual([d.fold for d in repeated], [0, 1])
                else:
                    self.assertFalse(any(d.hour == 2 for d in local))
                self.assertEqual(readings[-1].end - readings[0].start, timedelta(days=1))

    def test_variable_quality_requires_complete_nonoverlapping_events(self):
        events = ("400,1,2,E14,,", "400,3,48,A,,")
        readings = parse(nem12(stream(code="V", events=events)))
        self.assertEqual([r.quality for r in readings[:3]], [Quality.ESTIMATED, Quality.ESTIMATED, Quality.ACTUAL])
        for bad in (events[:1], (events[0], "400,2,48,A,,")):
            with self.subTest(events=bad), self.assertRaises(UnexpectedResponse):
                parse(nem12(stream(code="V", events=bad)))

    def test_unknown_channel_requires_explicit_kind_and_five_minute_data_works(self):
        text = nem12(stream("E2", minutes=5))
        with self.assertRaisesRegex(UnexpectedResponse, "unmapped_energy_channel"):
            parse(text)
        readings = parse(text, channel_kinds={"E2": ChannelKind.CONTROLLED_LOAD})
        self.assertEqual(len(readings), 288)
        self.assertEqual(readings[0].channel.kind, ChannelKind.CONTROLLED_LOAD)

    def test_channel_mapping_preserves_established_directions_and_rejects_nonmappings(self):
        text = nem12(stream(), stream("B1"))
        for mapping in ({"E1": ChannelKind.GRID_EXPORT}, {"B1": ChannelKind.CONTROLLED_LOAD}, [], "E1"):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                parse(text, channel_kinds=mapping)
        self.assertEqual(parse(text, channel_kinds={"E1": ChannelKind.GRID_IMPORT}), parse(text))

    def test_empty_invalid_duplicate_or_mismatched_data_is_never_silently_accepted(self):
        original = nem12(stream())
        invalid = [nem12(stream(value=v)) for v in ("", "NaN", "Infinity", "-1")]
        invalid += [original.replace("nmi-supply", "other-nmi"), original.replace("KWH", "KW"),
                    original.replace("900\n", ""), nem12(stream(), stream()),
                    original.replace(",A,,,", ",garbled,,,"), original.replace("0.25,", "", 1)]
        for text in invalid:
            with self.subTest(case=invalid.index(text)), self.assertRaises(UnexpectedResponse):
                parse(text)
        with self.assertRaises(DataUnavailable):
            parse(nem12(stream(day=date(2026, 10, 3))))
        # The caller's inclusive bounds filter wider service results.
        self.assertEqual(len(parse(nem12(stream(day=date(2026, 10, 3)), stream()))), 48)
