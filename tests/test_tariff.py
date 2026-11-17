"""Synthetic tariff calculations without a Home Assistant installation."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from myenergy.model import Channel, ChannelKind, Reading

ROOT = Path(__file__).resolve().parents[1] / "custom_components/powercor_myenergy"


def load(name):
    spec = importlib.util.spec_from_file_location("test_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tariff, hourly = load("tariff"), load("hourly")
IMPORT = Channel("meter-001", "channel-import", ChannelKind.GRID_IMPORT)
EXPORT = Channel("meter-001", "channel-export", ChannelKind.GRID_EXPORT)
CONTROLLED = Channel("meter-001", "channel-controlled", ChannelKind.CONTROLLED_LOAD)
ZONE = ZoneInfo("Australia/Melbourne")


def settings(model="flat", **overrides):
    plan = {"model": model, "timezone": "Australia/Melbourne", "effective_from": None,
            "import_price": "0.20", "export_price": "0.05", "bands": tariff.preset(model)}
    for band in plan["bands"]:
        band["price"] = {"free": "0", "peak": "0.50", "shoulder": "0.30", "overnight": "0.08"}[band["name"]]
    plan.update(overrides)
    return {"currency": "AUD", "plans": [plan]}


def readings(start, values, channel=IMPORT, minutes=60):
    start = start.astimezone(timezone.utc)
    return [Reading(channel, start + i * timedelta(minutes=minutes), start + (i + 1) * timedelta(minutes=minutes), Decimal(str(value)))
            for i, value in enumerate(values)]


def cost(rows, config):
    return tariff.hourly_costs(rows, hourly.hourly_energy(rows), config)


def block(reset="daily", anchor="2026-01-01", kwh="2", after="0.40"):
    return {"reset": reset, "anchor": anchor, "kwh": kwh, "after_price": after}


class TariffTests(unittest.TestCase):
    def test_original_half_hours_are_priced_before_hourly_grouping(self):
        config = settings("two_rate")
        config["plans"][0]["bands"][0]["start"] = "16:30:00"
        start = datetime(2026, 7, 1, 16, tzinfo=ZONE)
        rows = readings(start, [1, 3], minutes=30)
        self.assertEqual(cost(rows, config)[IMPORT][0][1], Decimal("1.70"))
        # A coarse interval cannot truthfully be split between two time bands.
        with self.assertRaisesRegex(ValueError, "inside a source interval"):
            cost(readings(start, [4]), config)

    def test_presets_weekdays_weekends_free_soak_and_overnight(self):
        cases = [("flat", 16, 2, "0.20"), ("two_rate", 16, 2, "0.50"),
                 ("three_rate", 16, 2, "0.50"), ("three_rate", 16, 5, "0.30"),
                 ("three_rate", 23, 2, "0.20"), ("free_midday", 12, 2, "0"),
                 ("midday_saver", 12, 2, "0"), ("midday_saver", 15, 2, "0.30"),
                 ("midday_saver", 17, 2, "0.50"), ("overnight", 3, 2, "0.08")]
        for model, hour, weekday, expected in cases:
            instant = datetime(2026, 7, 1 + (weekday - 2) % 7, hour, tzinfo=ZONE)
            with self.subTest(model=model, hour=hour, weekday=weekday):
                self.assertEqual(cost(readings(instant, [1]), settings(model))[IMPORT][0][1], Decimal(expected))

    def test_wrapping_window_uses_start_weekday(self):
        plan = settings()["plans"][0]
        plan["bands"] = [{"name": "overnight", "start": "22:00:00", "end": "06:00:00", "days": [4], "price": "0.08"}]
        self.assertEqual(tariff.interval_price(plan, datetime(2026, 7, 4, 2, tzinfo=ZONE), "grid_import"), Decimal("0.08"))
        self.assertEqual(tariff.interval_price(plan, datetime(2026, 7, 3, 2, tzinfo=ZONE), "grid_import"), Decimal("0.20"))

    def test_dated_changes_keep_delayed_history_at_original_price(self):
        config = settings()
        updated = {**deepcopy(config["plans"][0]), "import_price": "0.60",
                   "effective_from": datetime(2026, 7, 2, tzinfo=ZONE).astimezone(timezone.utc).isoformat()}
        config["plans"].append(updated)
        rows = readings(datetime(2026, 7, 1, 23, tzinfo=ZONE), [1, 2])
        self.assertEqual([amount for _, amount in cost(rows, config)[IMPORT]], [Decimal("0.20"), Decimal("1.20")])
        self.assertEqual(cost(rows, deepcopy(config)), cost(rows, config))

    def test_daily_blocks_split_threshold_share_channels_and_credit_export(self):
        config = settings(import_block=block(), export_block=block(kwh="1", after="0.01"))
        other = Channel("meter-001", "channel-import-2", ChannelKind.GRID_IMPORT)
        start = datetime(2026, 7, 1, tzinfo=ZONE)
        rows = readings(start, [1, 1]) + readings(start, [1, 1], other) + readings(start, [2, 1], EXPORT)
        amounts = cost(rows, config)
        self.assertEqual([v for _, v in amounts[IMPORT]], [Decimal("0.20"), Decimal("0.40")])
        self.assertEqual([v for _, v in amounts[other]], [Decimal("0.20"), Decimal("0.40")])
        self.assertEqual([v for _, v in amounts[EXPORT]], [Decimal("0.06"), Decimal("0.01")])

    def test_monthly_quarterly_cycles_clamp_and_do_not_drift(self):
        monthly = block("monthly", "2026-01-31")
        self.assertEqual(tariff.period_bounds(date(2026, 2, 28), monthly), (date(2026, 2, 28), date(2026, 3, 31)))
        quarterly = block("quarterly", "2025-11-30")
        self.assertEqual(tariff.period_bounds(date(2026, 3, 1), quarterly), (date(2026, 2, 28), date(2026, 5, 30)))
        self.assertEqual(tariff.request_dates(settings(import_block=monthly), date(2026, 3, 10), date(2026, 3, 11), date(2026, 4, 15)),
                         (date(2026, 2, 27), date(2026, 3, 30)))

    def test_block_corrections_reprice_later_intervals_and_midcycle_rate_change_keeps_usage(self):
        config = settings(import_block=block())
        start = datetime(2026, 7, 1, tzinfo=ZONE)
        original = cost(readings(start, [1, 2]), config)[IMPORT]
        corrected = cost(readings(start, [2, 2]), config)[IMPORT]
        self.assertEqual([v for _, v in original], [Decimal("0.20"), Decimal("0.60")])
        self.assertEqual([v for _, v in corrected], [Decimal("0.40"), Decimal("0.80")])
        newer = deepcopy(config["plans"][0])
        newer.update(effective_from=(start + timedelta(hours=1)).isoformat(), import_price="0.30")
        config["plans"].append(newer)
        self.assertEqual([v for _, v in cost(readings(start, [1, 2]), config)[IMPORT]], [Decimal("0.20"), Decimal("0.70")])

    def test_monthly_and_quarterly_thresholds_reset_from_billing_anchor(self):
        for reset, start, end_date in (("monthly", datetime(2026, 1, 31, tzinfo=ZONE), date(2026, 2, 28)),
                                       ("quarterly", datetime(2026, 2, 28, tzinfo=ZONE), date(2026, 5, 28))):
            with self.subTest(reset=reset):
                finish = datetime.combine(end_date, datetime.min.time(), ZONE)
                hour_count = int((finish.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 3600)
                rows = readings(start, [1] * (hour_count + 1))
                amounts = cost(rows, settings(import_block=block(reset, start.date().isoformat())))[IMPORT]
                self.assertEqual(amounts[0][1], Decimal("0.20"))
                self.assertEqual(amounts[-2][1], Decimal("0.40"))
                self.assertEqual(amounts[-1][1], Decimal("0.20"))

    def test_missing_earlier_block_usage_withholds_cost_until_complete(self):
        config = settings(import_block=block())
        start = datetime(2026, 7, 1, tzinfo=ZONE)
        rows = readings(start, [1]) + readings(start + timedelta(hours=2), [1])
        self.assertEqual(len(cost(rows, config)[IMPORT]), 1)
        self.assertEqual(cost(readings(start + timedelta(hours=1), [1]), config), {})
        self.assertEqual(len(cost(readings(start, [1, 0, 1]), config)[IMPORT]), 3)

    def test_dst_days_keep_real_23_and_25_hour_block_allowances(self):
        for day, count in ((date(2026, 4, 5), 25), (date(2026, 10, 4), 23)):
            with self.subTest(day=day):
                start = datetime.combine(day, datetime.min.time(), ZONE)
                rows = readings(start, [1] * (count + 1))
                amounts = cost(rows, settings(import_block=block()))[IMPORT]
                self.assertEqual(sum(value for _, value in amounts[:-1]), Decimal("0.40") + Decimal("0.40") * (count - 2))
                self.assertEqual(amounts[-1][1], Decimal("0.20"))

    def test_zero_optional_controlled_load_and_invalid_prices(self):
        start = datetime(2026, 7, 1, tzinfo=ZONE)
        rows = readings(start, [1], CONTROLLED) + readings(start, [1], EXPORT)
        result = cost(rows, settings(controlled_load_price="0", export_price="0"))
        self.assertEqual(result[CONTROLLED][0][1], 0)
        self.assertEqual(result[EXPORT][0][1], 0)
        self.assertEqual(cost(rows, settings(export_price=None)), {})
        for value in ("NaN", "Infinity", "-0.1", "nonsense"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tariff.price(value)
