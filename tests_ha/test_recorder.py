"""Import synthetic history through a real disposable Home Assistant recorder."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from homeassistant import bootstrap, loader
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import async_list_statistic_ids, statistics_during_period
from homeassistant.core import HomeAssistant

from custom_components.powercor_myenergy.myenergy.model import Channel, ChannelKind, Reading
from custom_components.powercor_myenergy.statistics import async_import_history


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_totals_are_stored_as_energy_statistics(self):
        hass = HomeAssistant("/config")
        loader.async_setup(hass)
        try:
            await bootstrap.async_from_config_dict({
                "homeassistant": {"name": "Synthetic meter test", "country": "AU", "time_zone": "Australia/Melbourne"},
                "recorder": {},
            }, hass)
            await hass.async_start()
            start = datetime(2026, 7, 1, tzinfo=timezone.utc)
            imported = Channel("meter-001", "a" * 64, ChannelKind.GRID_IMPORT)
            exported = Channel("meter-001", "b" * 64, ChannelKind.GRID_EXPORT)
            client = SimpleNamespace(
                get_meters=AsyncMock(return_value=[object()]),
                get_readings=AsyncMock(return_value=[
                    Reading(imported, start, start + timedelta(minutes=30), Decimal("0.125")),
                    Reading(imported, start + timedelta(minutes=30), start + timedelta(hours=1), Decimal("0.375")),
                    Reading(imported, start + timedelta(hours=1), start + timedelta(hours=2), Decimal("0.250")),
                    Reading(exported, start, start + timedelta(hours=1), Decimal("0.100")),
                ]),
            )
            coordinator = SimpleNamespace(
                client=client, sync_lock=asyncio.Lock(),
                async_authenticated_call=lambda operation, *args: operation(*args),
            )
            result = await async_import_history(hass, coordinator, date(2026, 7, 1), date(2026, 7, 1))
            repeated = await async_import_history(hass, coordinator, date(2026, 7, 1), date(2026, 7, 1))
            self.assertTrue(all(stream["changed_hours"] == 0 for stream in repeated["streams"].values()))
            ids = set(result["streams"])
            async with asyncio.timeout(15):
                while True:
                    rows = await get_instance(hass).async_add_executor_job(
                        statistics_during_period, hass, start, start + timedelta(hours=3),
                        ids, "hour", None, {"sum", "change"},
                    )
                    if all(rows.get(statistic_id) for statistic_id in ids):
                        break
                    await asyncio.sleep(0.1)
            import_id = next(item for item in ids if item.endswith("_grid_import"))
            export_id = next(item for item in ids if item.endswith("_grid_export"))
            self.assertEqual([row["sum"] for row in rows[import_id]], [0.5, 0.75])
            self.assertEqual([row["sum"] for row in rows[export_id]], [0.1])
            self.assertEqual([row["change"] for row in rows[import_id]], [0.5, 0.25])
            self.assertEqual([row["change"] for row in rows[export_id]], [0.1])
            client.get_readings.return_value = [
                Reading(imported, start, start + timedelta(minutes=30), Decimal("0.225")),
                Reading(imported, start + timedelta(minutes=30), start + timedelta(hours=1), Decimal("0.375")),
                Reading(imported, start + timedelta(hours=1), start + timedelta(hours=2), Decimal("0.250")),
                Reading(exported, start, start + timedelta(hours=1), Decimal("0.100")),
            ]
            revised = await async_import_history(hass, coordinator, date(2026, 7, 1), date(2026, 7, 1))
            self.assertEqual(revised["streams"][import_id]["changed_hours"], 1)
            self.assertEqual(revised["streams"][export_id]["changed_hours"], 0)
            async with asyncio.timeout(15):
                while True:
                    rows = await get_instance(hass).async_add_executor_job(
                        statistics_during_period, hass, start, start + timedelta(hours=3),
                        ids, "hour", None, {"sum", "change"},
                    )
                    if [row["sum"] for row in rows[import_id]] == [0.6, 0.85]:
                        break
                    await asyncio.sleep(0.1)
            repeated = await async_import_history(hass, coordinator, date(2026, 7, 1), date(2026, 7, 1))
            self.assertEqual(repeated["streams"][import_id]["changed_hours"], 0)
            metadata = await async_list_statistic_ids(hass, statistic_ids=ids)
            self.assertEqual(len(metadata), 2)
            self.assertTrue(all(item["has_sum"] and item["unit_class"] == "energy" and
                                item["statistics_unit_of_measurement"] == "kWh" for item in metadata))
        finally:
            await hass.async_stop()


if __name__ == "__main__":
    unittest.main()


class CostRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_cost_backfill_correction_and_repeated_import_after_restart(self):
        from tempfile import TemporaryDirectory
        from zoneinfo import ZoneInfo
        from custom_components.powercor_myenergy.statistics import cost_statistic_id
        from homeassistant.components.energy.validate import _async_validate_cost_stat, ValidationIssues

        with TemporaryDirectory(dir="/config") as directory:
            config = {"homeassistant": {"name": "Synthetic cost test", "country": "AU", "currency": "AUD", "time_zone": "Australia/Melbourne"}, "recorder": {}}
            local_start = datetime(2026, 7, 1, tzinfo=ZoneInfo("Australia/Melbourne"))
            start = local_start.astimezone(timezone.utc)
            imported = Channel("meter-002", "c" * 64, ChannelKind.GRID_IMPORT)
            exported = Channel("meter-002", "d" * 64, ChannelKind.GRID_EXPORT)
            options = {"tariff": {"currency": "AUD", "plans": [{
                "effective_from": None, "timezone": "Australia/Melbourne", "bands": [],
                "import_price": "0.20", "export_price": "0.05",
                "import_block": {"kwh": "2", "after_price": "0.40", "reset": "daily", "anchor": "2026-01-01"},
            }]}}
            def source(first=1):
                return [Reading(imported, start, start + timedelta(hours=1), Decimal(first)),
                        Reading(imported, start + timedelta(hours=1), start + timedelta(hours=2), Decimal(2)),
                        Reading(imported, start + timedelta(days=1), start + timedelta(days=1, hours=1), Decimal(1)),
                        Reading(exported, start, start + timedelta(hours=1), Decimal(2))]

            client = SimpleNamespace(get_meters=AsyncMock(return_value=[object()]), get_readings=AsyncMock(return_value=source()))
            coordinator = SimpleNamespace(client=client, sync_lock=asyncio.Lock(), config_entry=SimpleNamespace(options=options),
                                          async_authenticated_call=lambda operation, *args: operation(*args))
            ids = {cost_statistic_id(imported), cost_statistic_id(exported)}
            import_id = cost_statistic_id(imported)

            async def query(hass):
                return await get_instance(hass).async_add_executor_job(
                    statistics_during_period, hass, start, start + timedelta(days=2), ids, "hour", None, {"state", "sum", "change"})

            hass = HomeAssistant(directory)
            loader.async_setup(hass)
            try:
                await bootstrap.async_from_config_dict(config, hass)
                await hass.async_start()
                # Import a later day first, then backfill the earlier block period.
                client.get_readings.return_value = source()[2:3]
                coordinator.config_entry.options = {}
                unpriced = await async_import_history(hass, coordinator, date(2026, 7, 2), date(2026, 7, 2))
                self.assertFalse(unpriced["cost_streams"])
                coordinator.config_entry.options = options
                client.get_readings.return_value = source()
                result = await async_import_history(hass, coordinator, date(2026, 7, 1), date(2026, 7, 2))
                rows = await query(hass)
                self.assertEqual([round(row["sum"], 9) for row in rows[import_id]], [0.2, 0.8, 1.0])
                self.assertEqual([round(row["change"], 9) for row in rows[import_id]], [0.2, 0.6, 0.2])
                self.assertEqual(set(result["cost_streams"]), ids)
                client.get_readings.return_value = source(2)
                await async_import_history(hass, coordinator, date(2026, 7, 1), date(2026, 7, 1))
                rows = await query(hass)
                self.assertEqual([round(row["sum"], 9) for row in rows[import_id]], [0.4, 1.2, 1.4])
                metadata = await async_list_statistic_ids(hass, statistic_ids=ids)
                self.assertTrue(all(item["statistics_unit_of_measurement"] == "AUD" and item["has_sum"] and item["unit_class"] is None for item in metadata))
                issues = ValidationIssues()
                for item in ids:
                    _async_validate_cost_stat(hass, {item: (1, {})}, item, issues)
                self.assertFalse(issues.issues)
            finally:
                await hass.async_stop()

            hass = HomeAssistant(directory)
            loader.async_setup(hass)
            try:
                await bootstrap.async_from_config_dict(config, hass)
                await hass.async_start()
                # A fresh coordinator has no in-memory consumption counters.
                coordinator.sync_lock = asyncio.Lock()
                repeated = await async_import_history(hass, coordinator, date(2026, 7, 1), date(2026, 7, 2))
                self.assertTrue(all(stream["changed_hours"] == 0 for stream in repeated["streams"].values()))
                self.assertTrue(all(stream["changed_hours"] == 0 for stream in repeated["cost_streams"].values()))
                rows = await query(hass)
                self.assertEqual([round(row["sum"], 9) for row in rows[import_id]], [0.4, 1.2, 1.4])
            finally:
                await hass.async_stop()

    async def test_dst_retrieval_padding_is_not_imported_or_reported_as_missing_cost(self):
        from tempfile import TemporaryDirectory
        from custom_components.powercor_myenergy.statistics import cost_statistic_id, statistic_id

        with TemporaryDirectory(dir="/config") as directory:
            hass = HomeAssistant(directory)
            loader.async_setup(hass)
            try:
                await bootstrap.async_from_config_dict({
                    "homeassistant": {"country": "AU", "currency": "AUD", "time_zone": "Australia/Melbourne"}, "recorder": {},
                }, hass)
                await hass.async_start()
                imported = Channel("meter-dst", "e" * 64, ChannelKind.GRID_IMPORT)
                exported = Channel("meter-dst", "f" * 64, ChannelKind.GRID_EXPORT)
                config = {"currency": "AUD", "plans": [{
                    "effective_from": None, "timezone": "Australia/Melbourne", "bands": [],
                    "import_price": "0.20", "export_price": "0.05",
                    "import_block": {"kwh": "2", "after_price": "0.40", "reset": "daily", "anchor": "2026-01-01"},
                }]}
                async def source(meter, first, last):
                    start = datetime.combine(first, datetime.min.time(), timezone(timedelta(hours=10))).astimezone(timezone.utc)
                    count = ((last-first).days+1)*48
                    return [Reading(channel, start+i*timedelta(minutes=30), start+(i+1)*timedelta(minutes=30), energy)
                            for channel, energy in ((imported, Decimal(1)), (exported, Decimal("0.5"))) for i in range(count)]

                client = SimpleNamespace(get_meters=AsyncMock(return_value=[object()]), get_readings=AsyncMock(side_effect=source))
                coordinator = SimpleNamespace(client=client, sync_lock=asyncio.Lock(), config_entry=SimpleNamespace(options={"tariff": config}),
                                              async_authenticated_call=lambda operation, *args: operation(*args))
                result = await async_import_history(hass, coordinator, date(2026, 10, 6), date(2026, 10, 6))
                self.assertTrue(result["complete"])
                self.assertEqual(result["unpriced_hours"], 0)
                self.assertEqual(result["streams"][statistic_id(imported)]["hours"], 48)
                self.assertEqual(result["streams"][statistic_id(exported)]["hours"], 24)
                ids = {statistic_id(imported), statistic_id(exported), cost_statistic_id(imported), cost_statistic_id(exported)}
                rows = await get_instance(hass).async_add_executor_job(
                    statistics_during_period, hass, datetime(2026, 10, 4, tzinfo=timezone.utc), datetime(2026, 10, 9, tzinfo=timezone.utc),
                    ids, "hour", None, {"sum"})
                for channel in (imported, exported):
                    self.assertEqual([row["start"] for row in rows[statistic_id(channel)]],
                                     [row["start"] for row in rows[cost_statistic_id(channel)]])
                self.assertAlmostEqual(rows[cost_statistic_id(imported)][-1]["sum"], 37.6)
                self.assertAlmostEqual(rows[cost_statistic_id(exported)][-1]["sum"], 1.2)
                repeated = await async_import_history(hass, coordinator, date(2026, 10, 6), date(2026, 10, 6))
                self.assertTrue(all(item["changed_hours"] == 0 for item in repeated["cost_streams"].values()))
            finally:
                await hass.async_stop()

    async def test_correction_before_a_price_change_reconciles_later_block_costs(self):
        from tempfile import TemporaryDirectory
        from zoneinfo import ZoneInfo
        from custom_components.powercor_myenergy.statistics import cost_statistic_id

        with TemporaryDirectory(dir="/config") as directory:
            hass = HomeAssistant(directory)
            loader.async_setup(hass)
            try:
                await bootstrap.async_from_config_dict({
                    "homeassistant": {"country": "AU", "currency": "AUD", "time_zone": "Australia/Melbourne"}, "recorder": {},
                }, hass)
                await hass.async_start()
                start = datetime(2026, 7, 1, tzinfo=ZoneInfo("Australia/Melbourne")).astimezone(timezone.utc)
                channel = Channel("meter-revised", "1" * 64, ChannelKind.GRID_IMPORT)
                before = {"effective_from": None, "timezone": "Australia/Melbourne", "bands": [],
                          "import_price": "0.20", "export_price": None,
                          "import_block": {"kwh": "500", "after_price": "0.40", "reset": "monthly", "anchor": "2026-01-01"}}
                after = {**before, "effective_from": datetime(2026, 7, 15, tzinfo=ZoneInfo("Australia/Melbourne")).isoformat(),
                         "import_price": "0.30", "import_block": {**before["import_block"], "after_price": "0.50"}}
                revised = False
                async def source(meter, first, last):
                    return [Reading(channel, hour, hour+timedelta(hours=1), Decimal(11 if revised and index == 9*24 else 1))
                            for index in range(31*24) if first <= (hour := start+timedelta(hours=index)).astimezone(timezone(timedelta(hours=10))).date() <= last]

                client = SimpleNamespace(get_meters=AsyncMock(return_value=[object()]), get_readings=AsyncMock(side_effect=source))
                coordinator = SimpleNamespace(client=client, sync_lock=asyncio.Lock(),
                                              config_entry=SimpleNamespace(options={"tariff": {"currency": "AUD", "plans": [before, after]}}),
                                              async_authenticated_call=lambda operation, *args: operation(*args))
                await async_import_history(hass, coordinator, date(2026, 7, 10), date(2026, 7, 10))
                revised = True
                result = await async_import_history(hass, coordinator, date(2026, 7, 10), date(2026, 7, 10))
                stream = cost_statistic_id(channel)
                self.assertTrue(result["complete"])
                self.assertEqual(result["cost_streams"][stream]["changed_hours"], 11)
                stored = await get_instance(hass).async_add_executor_job(
                    statistics_during_period, hass, start, start+timedelta(days=31), {stream}, "hour", None, {"state", "sum"})
                rows = stored[stream]
                self.assertAlmostEqual(rows[-1]["sum"], 242.4)
                self.assertAlmostEqual(rows[490]["state"], 0.50)
                repeated = await async_import_history(hass, coordinator, date(2026, 7, 10), date(2026, 7, 10))
                self.assertEqual(repeated["cost_streams"][stream]["changed_hours"], 0)
            finally:
                await hass.async_stop()
