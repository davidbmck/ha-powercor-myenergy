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
