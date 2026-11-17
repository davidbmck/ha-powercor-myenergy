"""Exercise the actual options manager and selectors with synthetic prices."""

from copy import deepcopy
from datetime import datetime, timedelta
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntry, SOURCE_USER
from homeassistant.core import HomeAssistant

PREFIX = "custom_components.powercor_myenergy"


class TariffFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(dir="/config")
        self.hass = HomeAssistant(self.directory.name)
        loader.async_setup(self.hass)
        await bootstrap.async_from_config_dict({"homeassistant": {"country": "AU", "time_zone": "Australia/Melbourne", "currency": "AUD"}, "recorder": {}}, self.hass)
        self.entry = ConfigEntry(version=1, minor_version=1, domain="powercor_myenergy", title="Synthetic",
                                 data={}, options={}, source=SOURCE_USER, unique_id="synthetic-account",
                                 discovery_keys={}, subentries_data=[])
        with patch("homeassistant.config_entries.ConfigEntries.async_setup", new=AsyncMock(return_value=True)):
            await self.hass.config_entries.async_add(self.entry)

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.directory.cleanup()

    async def start(self):
        return await self.hass.config_entries.options.async_init(self.entry.entry_id)

    async def choose(self, flow_id, **extra):
        return await self.hass.config_entries.options.async_configure(flow_id, {
            "model": "flat", "export_model": "none", "timezone": "victorian",
            "customise_times": False, "extras": False, **extra,
        })

    async def test_flat_setup_and_price_rise_each_need_one_form_submission(self):
        result = await self.start()
        self.assertEqual({key.schema for key in result["data_schema"].schema}, {"import_price", "export_price", "advanced"})
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {"import_price": "0.30", "export_price": "0", "advanced": False})
        self.assertEqual(result["type"], "create_entry")
        saved = deepcopy(self.entry.options["tariff"])
        self.assertEqual(saved["currency"], "AUD")
        self.assertIsNone(saved["plans"][0]["effective_from"])
        self.assertEqual(saved["plans"][0]["export_price"], "0")
        result = await self.start()
        defaults = {key.schema: key.default() for key in result["data_schema"].schema}
        tomorrow = (datetime.now(ZoneInfo("Australia/Melbourne")).date() + timedelta(days=1)).isoformat()
        self.assertEqual(defaults["effective_date"], tomorrow)
        self.assertEqual(defaults["import_price"], "0.30")
        self.assertFalse(defaults["advanced"])
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {
            "import_price": "0.40", "export_price": "0.03", "effective_date": tomorrow, "operation": "new", "advanced": False})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.entry.options["tariff"]["plans"][0], saved["plans"][0])
        self.assertEqual(len(self.entry.options["tariff"]["plans"]), 2)
        result = await self.start()
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {
            "import_price": "0.40", "export_price": "0.03", "effective_date": tomorrow, "operation": "edit", "advanced": False})
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {"plan": "0"})
        result = await self.choose(result["flow_id"], export_model="flat")
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {"import_price": "0.25", "export_price": "0.01"})
        self.assertEqual(result["step_id"], "confirm")
        self.assertEqual(self.entry.options["tariff"]["plans"][0], saved["plans"][0])
        await self.hass.config_entries.options.async_configure(result["flow_id"], {})
        self.assertEqual(self.entry.options["tariff"]["plans"][0]["import_price"], "0.25")
        self.assertEqual(self.entry.options["tariff"]["plans"][1]["import_price"], "0.40")

    async def test_provider_change_reuses_hours_and_keeps_old_plan(self):
        result = await self.start()
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {"import_price": "0.20", "export_price": "0.05", "advanced": True})
        result = await self.choose(result["flow_id"], model="two_rate", export_model="flat", customise_times=True)
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {"peak_price": "0.50"})
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        await self.hass.config_entries.options.async_configure(result["flow_id"], {"peak_start": "15:30:00", "peak_end": "21:30:00", "peak_days": days})
        original = deepcopy(self.entry.options["tariff"]["plans"][0])
        result = await self.start()
        defaults = {key.schema: key.default() for key in result["data_schema"].schema}
        self.assertFalse(defaults["advanced"])
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {
            "import_price": "0.25", "export_price": "0.04", "peak_price": "0.60", "advanced": False,
            "operation": "new", "effective_date": defaults["effective_date"]})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.entry.options["tariff"]["plans"][0], original)
        new = self.entry.options["tariff"]["plans"][1]
        self.assertEqual(new["bands"][0]["start"], "15:30:00")
        self.assertEqual(new["bands"][0]["price"], "0.60")
        result = await self.start()
        defaults = {key.schema: key.default() for key in result["data_schema"].schema}
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {
            "import_price": "0.35", "export_price": "0.02", "advanced": True,
            "operation": "new", "effective_date": defaults["effective_date"]})
        result = await self.choose(result["flow_id"], model="flat", export_model="flat")
        self.assertEqual(result["type"], "create_entry")
        plans = self.entry.options["tariff"]["plans"]
        self.assertEqual(len(plans), 3)
        self.assertEqual(plans[0], original)
        self.assertEqual(plans[2]["bands"], [])

    async def test_block_options_and_invalid_price_are_validated(self):
        result = await self.start()
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {"import_price": "0.30", "export_price": "0.05", "advanced": True})
        result = await self.choose(result["flow_id"], model="block", export_model="block")
        values = {"import_block_kwh": "10", "import_after_price": "0.40",
                  "import_reset": "monthly", "import_anchor": "2026-01-31",
                  "export_block_kwh": "5", "export_after_price": "0.01",
                  "export_reset": "daily", "export_anchor": "2026-01-01"}
        invalid = await self.hass.config_entries.options.async_configure(result["flow_id"], {**values, "import_after_price": "NaN"})
        self.assertEqual(invalid["errors"], {"base": "invalid_price"})
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], values)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.entry.options["tariff"]["plans"][0]["import_block"]["anchor"], "2026-01-31")

    async def test_midday_preset_custom_times_and_optional_special_prices(self):
        result = await self.start()
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {"import_price": "0.20", "advanced": True})
        result = await self.choose(result["flow_id"], model="midday_saver", customise_times=True, extras=True)
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {"peak_price": "0.50", "shoulder_price": "0.15"})
        self.assertEqual(result["step_id"], "times")
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {
            "free_start": "11:00:00", "free_end": "15:00:00", "free_days": days,
            "peak_start": "16:00:00", "peak_end": "21:00:00", "peak_days": days,
            "shoulder_start": "15:00:00", "shoulder_end": "16:00:00", "shoulder_days": days,
        })
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {
            "controlled_load_price": "0.10", "controlled_load_register": "E2", "extra_overnight_price": "0.08", "extra_overnight_start": "00:00:00", "extra_overnight_end": "06:00:00",
        })
        plan = self.entry.options["tariff"]["plans"][0]
        self.assertEqual(plan["bands"][0]["price"], "0.08")
        self.assertEqual(plan["bands"][1]["end"], "15:00:00")
        self.assertEqual(plan["controlled_load_price"], "0.10")

    async def test_every_import_preset_can_be_saved_through_the_actual_forms(self):
        from custom_components.powercor_myenergy.tariff import MODELS
        prices = {
            "two_rate": {"peak_price": "0.50"},
            "three_rate": {"peak_price": "0.50", "shoulder_price": "0.30"},
            "midday_saver": {"peak_price": "0.50", "shoulder_price": "0.15"},
            "overnight": {"overnight_price": "0.08"},
            "block": {"import_block_kwh": "10", "import_after_price": "0.40",
                      "import_reset": "daily", "import_anchor": "2026-01-01"},
        }
        for model in MODELS:
            with self.subTest(model=model):
                self.hass.config_entries.async_update_entry(self.entry, options={})
                result = await self.start()
                result = await self.hass.config_entries.options.async_configure(result["flow_id"], {
                    "import_price": "0.30", "advanced": True})
                result = await self.choose(result["flow_id"], model=model)
                if model in prices:
                    result = await self.hass.config_entries.options.async_configure(result["flow_id"], prices[model])
                self.assertEqual(result["type"], "create_entry")
                plan = self.entry.options["tariff"]["plans"][0]
                self.assertNotIn("overnight_price", plan)
                self.assertNotIn("extra_overnight", plan)
                if model == "free_midday":
                    self.assertEqual(plan["bands"][0]["price"], "0")

    async def test_switching_export_blocks_to_flat_removes_inactive_tier_prices(self):
        from datetime import timezone
        from decimal import Decimal
        from custom_components.powercor_myenergy.tariff import hourly_costs
        from custom_components.powercor_myenergy.hourly import hourly_energy
        from custom_components.powercor_myenergy.myenergy.model import Channel, ChannelKind, Reading

        result = await self.start()
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {
            "import_price": "0.30", "export_price": "0.10", "advanced": True})
        result = await self.choose(result["flow_id"], export_model="block")
        await self.hass.config_entries.options.async_configure(result["flow_id"], {
            "export_block_kwh": "1", "export_after_price": "0.01",
            "export_reset": "daily", "export_anchor": "2026-01-01"})
        result = await self.start()
        defaults = {key.schema: key.default() for key in result["data_schema"].schema}
        result = await self.hass.config_entries.options.async_configure(result["flow_id"], {
            "import_price": "0.30", "export_price": "0.10", "advanced": True,
            "effective_date": defaults["effective_date"], "operation": "new"})
        result = await self.choose(result["flow_id"], export_model="flat")
        self.assertEqual(result["type"], "create_entry")
        plan = self.entry.options["tariff"]["plans"][-1]
        self.assertNotIn("export_block", plan)
        start = datetime.fromisoformat(plan["effective_from"]).astimezone(timezone.utc)
        channel = Channel("meter-example", "channel-export", ChannelKind.GRID_EXPORT)
        rows = [Reading(channel, start, start+timedelta(hours=1), Decimal(2))]
        money = hourly_costs(rows, hourly_energy(rows), self.entry.options["tariff"])
        self.assertEqual(money[channel][0][1], Decimal("0.20"))
