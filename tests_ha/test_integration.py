"""Focused HA boundary checks; run inside the disposable Docker environment."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.powercor_myenergy import async_setup
from custom_components.powercor_myenergy.config_flow import MyEnergyConfigFlow
from custom_components.powercor_myenergy.const import CONF_LAST_LOGIN, MAX_SYNC_DAYS
from custom_components.powercor_myenergy.coordinator import MyEnergyCoordinator
from custom_components.powercor_myenergy.diagnostics import async_get_config_entry_diagnostics
from custom_components.powercor_myenergy.statistics import AEST, async_import_history
from custom_components.powercor_myenergy.myenergy import (
    AuthenticationExpired, DataUnavailable, MailboxError, TransportError, UnexpectedResponse,
)
from custom_components.powercor_myenergy.myenergy.model import Channel, ChannelKind, Reading

PREFIX = "custom_components.powercor_myenergy"
LOGIN = {"username": "account@example.invalid", "password": "synthetic-password", "automatic_login": True}
MAILBOX = {"host": "imap.example.invalid", "username": "otp@example.invalid", "password": "synthetic-app-password",
           "folder": "INBOX", "sender": "sender@example.invalid", "recipient": "otp@example.invalid",
           "subject": "{code} verification"}


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant("/config")
        self.hass.state = CoreState.running
        self.entry = SimpleNamespace(data={**LOGIN, "imap": MAILBOX, "session": {"version": 1, "cookies": []}},
                                     entry_id="entry-example", pref_disable_polling=False,
                                     async_on_unload=Mock())
        self.client = Mock()
        self.client.get_meters = AsyncMock(return_value=[object()])
        self.client.login = AsyncMock()
        self.client.start_login = AsyncMock()
        self.client.submit_otp = AsyncMock()
        self.client.export_state.return_value = {"version": 1, "cookies": []}
        self.session = SimpleNamespace(close=AsyncMock())
        self.updates = []

        def update(entry, *, data):
            entry.data = data
            self.updates.append(dict(data))

        self.hass.config_entries = SimpleNamespace(async_update_entry=Mock(side_effect=update))
        self.patch_client = patch(f"{PREFIX}.coordinator.MyEnergyClient", return_value=self.client)
        self.patch_client.start()
        self.addCleanup(self.patch_client.stop)
        self.patch_sync = patch(f"{PREFIX}.coordinator.async_import_history",
                                new_callable=AsyncMock,
                                return_value={"latest_source_timestamp": datetime(2026, 7, 2, tzinfo=timezone.utc).isoformat(),
                                              "complete": True, "skipped_meters": 0, "incomplete_hours": 0})
        self.mock_sync = self.patch_sync.start()
        self.addCleanup(self.patch_sync.stop)
        self.patch_recorder = patch(f"{PREFIX}.statistics.get_instance")
        self.recorder = self.patch_recorder.start()
        self.recorder.return_value.async_add_executor_job = AsyncMock(return_value={})
        self.recorder.return_value.async_block_till_done = AsyncMock()
        self.addCleanup(self.patch_recorder.stop)

    async def asyncTearDown(self):
        await self.hass.async_stop()

    def coordinator(self):
        return MyEnergyCoordinator(self.hass, self.entry, self.session)

    async def test_restore_and_persist_rotated_state_without_meter_metadata(self):
        coordinator = self.coordinator()
        data = await coordinator._async_update_data()
        self.client.restore_state.assert_called_once_with(self.entry.data["session"])
        self.assertEqual(set(data), {"last_update", "meter_count", "latest_source_timestamp",
                                     "last_successful_sync", "sync_complete", "skipped_meters", "incomplete_hours",
                                     "unresolved_history_start"})
        self.assertEqual(data["meter_count"], 1)
        self.assertIsNotNone(data["last_update"].tzinfo)
        self.client.login.assert_not_called()
        self.assertIn("session", self.updates[-1])
        self.assertIn("last_successful_sync", self.updates[-1])
        first_range = self.mock_sync.await_args.args[2:4]
        self.assertEqual((first_range[1] - first_range[0]).days, 29)
        await coordinator._async_update_data()
        self.assertEqual(self.mock_sync.await_count, 1)
        old_sync = datetime.now(timezone.utc) - timedelta(days=20)
        self.entry.data["last_successful_sync"] = old_sync.isoformat()
        await coordinator._async_update_data()
        recent_range = self.mock_sync.await_args.args[2:4]
        self.assertEqual(recent_range[0], old_sync.astimezone(AEST).date() - timedelta(days=1))

    async def test_session_renews_when_history_not_due(self):
        self.entry.data["last_successful_sync"] = datetime.now(timezone.utc).isoformat()
        await self.coordinator()._async_update_data()
        self.client.get_meters.assert_awaited_once()
        self.mock_sync.assert_not_awaited()
        self.assertIn("session", self.updates[-1])

    async def test_startup_refresh_persists_session_without_waiting_for_recorder(self):
        self.hass.state = CoreState.starting
        coordinator = self.coordinator()
        await coordinator._async_update_data()
        self.mock_sync.assert_not_awaited()
        self.assertIn("session", self.updates[-1])
        self.hass.state = CoreState.running
        await coordinator._async_update_data()
        self.mock_sync.assert_awaited_once()

    async def test_partial_initial_backfill_is_retried_before_switching_windows(self):
        self.mock_sync.return_value["complete"] = False
        coordinator = self.coordinator()
        await coordinator._async_update_data()
        self.assertIn("last_successful_sync", self.entry.data)
        self.assertIn("incomplete_period_start", self.entry.data)
        self.entry.data["last_successful_sync"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        self.mock_sync.return_value["complete"] = True
        await coordinator._async_update_data()
        recent_range = self.mock_sync.await_args.args[2:4]
        self.assertEqual((recent_range[1] - recent_range[0]).days, 29)
        self.assertIn("last_successful_sync", self.entry.data)
        self.assertNotIn("incomplete_period_start", self.entry.data)

    async def test_automatic_window_is_capped_and_reports_older_gaps(self):
        today = datetime.now(AEST).date()
        old_sync = datetime.now(timezone.utc) - timedelta(days=90)
        self.entry.data["last_successful_sync"] = old_sync.isoformat()
        result = await self.coordinator()._async_update_data()
        requested = self.mock_sync.await_args.args[2:4]
        self.assertEqual(requested[0], today - timedelta(days=MAX_SYNC_DAYS))
        self.assertEqual(requested[1], today - timedelta(days=1))
        self.assertEqual(result["unresolved_history_start"],
                         (old_sync.astimezone(AEST).date() - timedelta(days=1)).isoformat())
        self.assertFalse(result["sync_complete"])

    async def test_aged_partial_gap_stays_reported_after_recent_sync(self):
        today = datetime.now(AEST).date()
        anchor = today - timedelta(days=MAX_SYNC_DAYS + 5)
        self.entry.data["incomplete_period_start"] = anchor.isoformat()
        self.entry.data["last_successful_sync"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        result = await self.coordinator()._async_update_data()
        self.assertEqual(self.mock_sync.await_args.args[2], today - timedelta(days=MAX_SYNC_DAYS))
        self.assertEqual(result["unresolved_history_start"], anchor.isoformat())
        self.assertFalse(result["sync_complete"])

    async def test_complete_manual_backfill_clears_older_gap_marker(self):
        today = datetime.now(AEST).date()
        anchor = today - timedelta(days=MAX_SYNC_DAYS + 5)
        self.entry.data["incomplete_period_start"] = anchor.isoformat()
        self.entry.state = ConfigEntryState.LOADED
        self.entry.runtime_data = self.coordinator()
        self.hass.config_entries.async_entries = Mock(return_value=[self.entry])
        with (patch(f"{PREFIX}.async_import_history", new_callable=AsyncMock,
                    return_value={"complete": True}),
              patch("homeassistant.core.ServiceRegistry.async_register") as register):
            await async_setup(self.hass, {})
            handler = register.call_args.args[2]
            await handler(SimpleNamespace(data={"start_date": anchor,
                                            "end_date": today - timedelta(days=MAX_SYNC_DAYS + 1)}))
        self.assertNotIn("incomplete_period_start", self.entry.data)

    async def test_failed_historical_sync_does_not_advance_success_timestamp(self):
        self.mock_sync.side_effect = HomeAssistantError("temporary source failure")
        with self.assertRaises(UpdateFailed):
            await self.coordinator()._async_update_data()
        self.assertNotIn("last_successful_sync", self.entry.data)

    async def test_success_timestamp_waits_for_import_completion(self):
        release = asyncio.Event()

        async def delayed_import(*args, **kwargs):
            await release.wait()
            return {"complete": True, "latest_source_timestamp": None,
                    "skipped_meters": 0, "incomplete_hours": 0}

        self.mock_sync.side_effect = delayed_import
        task = asyncio.create_task(self.coordinator()._async_update_data())
        await asyncio.sleep(0)
        self.assertNotIn("last_successful_sync", self.entry.data)
        release.set()
        await task
        self.assertIn("last_successful_sync", self.entry.data)

    async def test_historical_auth_and_mailbox_failures_keep_classification(self):
        with patch(f"{PREFIX}.coordinator.async_import_history", new=async_import_history):
            self.client.get_readings = AsyncMock(side_effect=AuthenticationExpired())
            self.client.login.side_effect = MailboxError("imap_authentication_failed")
            with self.assertRaises(ConfigEntryAuthFailed):
                await self.coordinator()._async_update_data()
            self.assertNotIn("last_successful_sync", self.entry.data)

            self.entry.data.pop(CONF_LAST_LOGIN)
            self.client.login.side_effect = MailboxError("imap_otp_timeout")
            with self.assertRaises(UpdateFailed):
                await self.coordinator()._async_update_data()
            self.assertNotIn("last_successful_sync", self.entry.data)

            self.client.get_readings.side_effect = TransportError()
            with self.assertRaises(UpdateFailed):
                await self.coordinator()._async_update_data()

    async def test_expiry_logs_in_once_then_resumes_and_persists(self):
        self.client.get_meters.side_effect = [AuthenticationExpired(), [object()]]
        coordinator = self.coordinator()
        await coordinator._async_update_data()
        self.client.login.assert_awaited_once_with(LOGIN["username"], LOGIN["password"], imap_config=MAILBOX)
        self.assertIn(CONF_LAST_LOGIN, self.updates[0])
        self.assertEqual(self.client.get_meters.await_count, 2)

    async def test_mailbox_outage_cooldown_survives_coordinator_restart(self):
        self.client.get_meters.side_effect = AuthenticationExpired()
        self.client.login.side_effect = MailboxError("imap_otp_timeout")
        with self.assertRaises(UpdateFailed):
            await self.coordinator()._async_update_data()
        with self.assertRaises(UpdateFailed):
            await self.coordinator()._async_update_data()
        self.assertEqual(self.client.login.await_count, 1)

    async def test_manual_expiry_and_rejected_mailbox_credentials_request_reauth(self):
        self.client.get_meters.side_effect = AuthenticationExpired()
        self.entry.data["automatic_login"] = False
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator()._async_update_data()
        self.client.login.assert_not_called()
        self.entry.data["automatic_login"] = True
        self.client.login.side_effect = MailboxError("imap_authentication_failed")
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator()._async_update_data()

    async def test_transport_or_changed_response_does_not_start_login(self):
        coordinator = self.coordinator()
        for error in (TransportError(), UnexpectedResponse()):
            self.client.get_meters.side_effect = error
            with self.assertRaises(UpdateFailed):
                await coordinator._async_update_data()
        self.client.login.assert_not_called()

    async def test_manual_flow_keeps_code_out_of_entry_and_closes_session(self):
        flow = MyEnergyConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = Mock()
        with (patch(f"{PREFIX}.config_flow.async_create_session", return_value=self.session),
              patch(f"{PREFIX}.config_flow.MyEnergyClient", return_value=self.client)):
            result = await flow.async_step_user({**LOGIN, "automatic_login": False})
            self.assertEqual(result["step_id"], "otp")
            result = await flow.async_step_otp({"code": "123456"})
        self.assertEqual(result["type"], "create_entry")
        self.assertNotIn("123456", str(result["data"]))
        self.assertNotIn("code", result["data"])
        self.session.close.assert_awaited_once()

    async def test_mailbox_form_accepts_a_separate_non_email_login(self):
        flow = MyEnergyConfigFlow()
        flow.hass = self.hass
        flow._data = dict(LOGIN)
        result = await flow.async_step_mailbox()
        values = {**MAILBOX, "username": "synthetic-mailbox-login"}
        self.assertEqual(result["data_schema"](values), values)

    async def test_automatic_setup_preserves_separate_mailbox_credentials(self):
        flow = MyEnergyConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = Mock()
        flow.async_show_progress = Mock(return_value={"type": "progress"})
        with (patch(f"{PREFIX}.config_flow.async_create_session", return_value=self.session),
              patch(f"{PREFIX}.config_flow.MyEnergyClient", return_value=self.client)):
            result = await flow.async_step_user(LOGIN)
            self.assertEqual(result["step_id"], "mailbox")
            await flow.async_step_mailbox(MAILBOX)
            await flow._login_task
            result = await flow.async_step_authenticate()
            self.assertEqual(result["step_id"], "finish")
            result = await flow.async_step_finish()
        self.client.login.assert_awaited_once_with(
            LOGIN["username"], LOGIN["password"], imap_config=MAILBOX,
        )
        self.assertEqual(result["data"]["imap"], MAILBOX)
        self.assertEqual(result["data"]["username"], LOGIN["username"])
        self.session.close.assert_awaited_once()

    async def test_reauth_updates_existing_entry_with_new_session(self):
        flow = MyEnergyConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "reauth"}
        flow._data = {**LOGIN, "imap": MAILBOX}
        flow._client, flow._session = self.client, self.session
        flow.async_set_unique_id = AsyncMock()
        import hashlib
        self.entry.unique_id = hashlib.sha256(LOGIN["username"].encode()).hexdigest()
        flow._get_reauth_entry = Mock(return_value=self.entry)
        flow.async_update_reload_and_abort = Mock(return_value={"type": "abort", "reason": "reauth_successful"})
        result = await flow.async_step_finish()
        self.assertEqual(result["reason"], "reauth_successful")
        saved = flow.async_update_reload_and_abort.call_args.kwargs["data"]
        self.assertEqual(saved["session"], self.client.export_state.return_value)
        self.session.close.assert_awaited_once()

    async def test_diagnostics_are_an_allowlist(self):
        coordinator = self.coordinator()
        coordinator.async_set_updated_data({"last_update": datetime.now(timezone.utc), "meter_count": 1})
        self.entry.runtime_data = coordinator
        result = await async_get_config_entry_diagnostics(self.hass, self.entry)
        self.assertEqual(set(result), {"automatic_login", "last_update_success", "last_update", "meter_count",
                                       "last_successful_sync", "sync_complete", "skipped_meters", "incomplete_hours",
                                       "unresolved_history_start"})
        for sensitive in (LOGIN["username"], LOGIN["password"], MAILBOX["password"]):
            self.assertNotIn(sensitive, str(result))

    async def test_historical_import_queues_separate_cumulative_energy_streams(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        imported = Channel("meter-001", "a" * 64, ChannelKind.GRID_IMPORT)
        exported = Channel("meter-001", "b" * 64, ChannelKind.GRID_EXPORT)
        self.client.get_readings = AsyncMock(return_value=[
            Reading(imported, start, start + timedelta(minutes=30), Decimal("0.125")),
            Reading(imported, start + timedelta(minutes=30), start + timedelta(hours=1), Decimal("0.375")),
            Reading(imported, start + timedelta(hours=1), start + timedelta(hours=2), Decimal("0.250")),
            Reading(exported, start, start + timedelta(hours=1), Decimal("0.100")),
        ])
        with (patch(f"{PREFIX}.statistics.async_list_statistic_ids", new_callable=AsyncMock, return_value=[]),
              patch(f"{PREFIX}.statistics.async_add_external_statistics") as add):
            result = await async_import_history(self.hass, self.coordinator(), date(2026, 7, 1), date(2026, 7, 1))
        self.assertEqual(len(result["streams"]), 2)
        self.assertEqual(result["skipped_meters"], 0)
        self.assertEqual(add.call_count, 2)
        self.assertEqual(result["streams"][f"powercor_myenergy:energy_{'a' * 64}_grid_import"],
                         {"hours": 2, "kwh": 0.75, "incomplete_hours": 22, "changed_hours": 2})
        self.assertEqual(result["streams"][f"powercor_myenergy:energy_{'b' * 64}_grid_export"],
                         {"hours": 1, "kwh": 0.1, "incomplete_hours": 23, "changed_hours": 1})

    async def test_historical_import_reports_unavailable_and_incomplete_streams(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        complete = Channel("meter-001", "a" * 64, ChannelKind.GRID_IMPORT)
        incomplete = Channel("meter-001", "b" * 64, ChannelKind.GRID_EXPORT)
        self.client.get_meters.return_value = [object(), object()]
        self.client.get_readings = AsyncMock(side_effect=[DataUnavailable(), [
            Reading(complete, start, start + timedelta(hours=1), Decimal("1")),
            Reading(incomplete, start, start + timedelta(minutes=30), Decimal("1")),
        ]])
        with (patch(f"{PREFIX}.statistics.async_list_statistic_ids", new_callable=AsyncMock, return_value=[]),
              patch(f"{PREFIX}.statistics.async_add_external_statistics") as add):
            result = await async_import_history(self.hass, self.coordinator(), date(2026, 7, 1), date(2026, 7, 1))
        self.assertEqual(result["skipped_meters"], 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["unimported_streams"], [f"powercor_myenergy:energy_{'b' * 64}_grid_export"])
        self.assertEqual(next(iter(result["streams"].values()))["incomplete_hours"], 23)
        add.assert_called_once()
        self.recorder.return_value.async_block_till_done.assert_awaited_once()

    async def test_manual_historical_import_recovers_session_without_refresh(self):
        coordinator = self.coordinator()
        coordinator.async_refresh = AsyncMock(side_effect=AssertionError("recursive refresh"))
        self.client.get_meters.side_effect = [AuthenticationExpired(), [object()]]
        self.client.get_readings = AsyncMock(return_value=[])
        result = await asyncio.wait_for(
            async_import_history(self.hass, coordinator, date(2026, 7, 1), date(2026, 7, 1)), 2,
        )
        self.assertFalse(result["complete"])
        self.client.login.assert_awaited_once()
        coordinator.async_refresh.assert_not_awaited()
        self.assertIn("session", self.updates[-1])

    async def test_all_unavailable_history_reports_partial_without_writes(self):
        self.client.get_readings = AsyncMock(side_effect=DataUnavailable())
        result = await async_import_history(self.hass, self.coordinator(), date(2026, 7, 1), date(2026, 7, 1))
        self.assertFalse(result["complete"])
        self.assertEqual(result["skipped_meters"], 1)
        self.assertEqual(result["streams"], {})
        self.recorder.return_value.async_block_till_done.assert_not_awaited()

    async def test_historical_import_reconciles_change_and_is_idempotent(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        channel = Channel("meter-001", "a" * 64, ChannelKind.GRID_IMPORT)
        self.client.get_readings = AsyncMock(return_value=[
            Reading(channel, start, start + timedelta(hours=1), Decimal("2")),
        ])
        stream = f"powercor_myenergy:energy_{'a' * 64}_grid_import"
        original = [
            {"start": start.timestamp(), "state": 1.0, "sum": 1.0},
            {"start": (start + timedelta(hours=1)).timestamp(), "state": 3.0, "sum": 4.0},
        ]
        revised = [
            {"start": start.timestamp(), "state": 2.0, "sum": 2.0},
            {"start": (start + timedelta(hours=1)).timestamp(), "state": 3.0, "sum": 5.0},
        ]
        self.recorder.return_value.async_add_executor_job.side_effect = [
            {stream: original}, {stream: revised},
        ]
        with (patch(f"{PREFIX}.statistics.async_list_statistic_ids", new_callable=AsyncMock,
                    return_value=[{"statistic_id": stream, "source": "powercor_myenergy",
                                   "statistics_unit_of_measurement": "kWh"}]),
              patch(f"{PREFIX}.statistics.async_add_external_statistics") as add):
            coordinator = self.coordinator()
            first = await async_import_history(self.hass, coordinator, date(2026, 7, 1), date(2026, 7, 1))
            second = await async_import_history(self.hass, coordinator, date(2026, 7, 1), date(2026, 7, 1))
        self.assertEqual(first["streams"][stream]["changed_hours"], 1)
        self.assertEqual(second["streams"][stream]["changed_hours"], 0)
        add.assert_called_once()
        self.assertEqual([row["sum"] for row in add.call_args.args[2]], [2.0, 5.0])


if __name__ == "__main__":
    unittest.main()
