"""Live setup, lost-session recovery and restart in disposable HA storage."""

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import sys

from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntryState
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period

from custom_components.powercor_myenergy.const import DOMAIN
from custom_components.powercor_myenergy.diagnostics import async_get_config_entry_diagnostics
from custom_components.powercor_myenergy.statistics import statistic_id

AEST = timezone(timedelta(hours=10))


def passed(outcome):
    print(outcome, flush=True)


async def start():
    hass = HomeAssistant("/config")
    loader.async_setup(hass)
    result = await bootstrap.async_from_config_dict({
        "homeassistant": {"name": "myEnergy disposable test", "country": "AU", "time_zone": "Australia/Melbourne"},
        "recorder": {},
    }, hass)
    if result is None:
        raise RuntimeError("HA bootstrap failed")
    await hass.async_start()
    return hass


async def loaded(hass):
    async with asyncio.timeout(60):
        while True:
            entries = hass.config_entries.async_entries(DOMAIN)
            if entries and entries[0].state == ConfigEntryState.LOADED:
                return entries[0]
            if entries and entries[0].state in (ConfigEntryState.SETUP_ERROR, ConfigEntryState.SETUP_RETRY):
                raise RuntimeError("Integration setup failed")
            await asyncio.sleep(0.5)


async def run():
    private = json.load(sys.stdin)
    logging.getLogger().setLevel(logging.ERROR)
    hass = None
    stage = "bootstrap"
    try:
        history_day = date.fromisoformat(private["history_date"]) if "history_date" in private else datetime.now(AEST).date() - timedelta(days=30)
        hass = await start()
        stage = "configuration_flow"
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            **private["myenergy"], "automatic_login": True,
        })
        result = await hass.config_entries.flow.async_configure(result["flow_id"], private["imap"])
        del private
        async with asyncio.timeout(180):
            while result["type"] not in ("create_entry", "abort"):
                if result["type"] == "form":
                    raise RuntimeError("Login flow returned an error")
                await asyncio.sleep(1)
                result = await hass.config_entries.flow.async_configure(result["flow_id"])
        if result["type"] != "create_entry":
            raise RuntimeError("Login flow did not create an entry")
        entry = await loaded(hass)
        passed("Fresh UI configuration and automatic OTP: passed")
        coordinator = entry.runtime_data
        if not coordinator.last_update_success:
            raise RuntimeError("Initial discovery failed")
        sensors = [s for s in hass.states.async_all("sensor") if s.entity_id.startswith("sensor.myenergy_")]
        if not sensors or all(s.state in ("unknown", "unavailable") for s in sensors):
            raise RuntimeError("Status sensor unavailable")
        passed("Protected discovery and last-update sensor: passed")

        stage = "lost_session_recovery"
        coordinator.client.restore_state({"version": 1, "cookies": []})
        await coordinator.async_refresh()
        if not coordinator.last_update_success or "last_login_attempt" not in entry.data:
            raise RuntimeError("Automatic recovery failed")
        login_attempt = entry.data["last_login_attempt"]
        passed("Lost-session automatic login and resumed discovery: passed")
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        if set(diagnostics) != {"automatic_login", "last_update_success", "last_update", "meter_count",
                                "last_successful_sync", "sync_complete", "skipped_meters",
                                "incomplete_hours", "unresolved_history_start"}:
            raise RuntimeError("Unexpected diagnostics fields")
        passed("Diagnostics contain operational status only: passed")

        stage = "historical_import"
        source = []
        for meter in await coordinator.client.get_meters():
            source.extend(await coordinator.client.get_readings(meter, history_day, history_day))
        start_utc = datetime.combine(history_day, datetime.min.time(), AEST).astimezone(timezone.utc)
        by_channel = defaultdict(list)
        for reading in source:
            by_channel[reading.channel].append(reading)
        expected = {}
        for channel, intervals in by_channel.items():
            cursor = start_utc
            changes = defaultdict(Decimal)
            for reading in sorted(intervals, key=lambda value: value.start):
                if reading.start != cursor or reading.kwh is None:
                    raise RuntimeError("Historical period is incomplete")
                hour = reading.start.replace(minute=0, second=0, microsecond=0)
                if reading.end > hour + timedelta(hours=1):
                    raise RuntimeError("Historical interval crosses an hour")
                changes[hour] += reading.kwh
                cursor = reading.end
            if cursor != start_utc + timedelta(days=1) or len(changes) != 24:
                raise RuntimeError("Historical period is incomplete")
            expected[statistic_id(channel)] = changes
        if not expected:
            raise RuntimeError("No historical streams")
        response = await hass.services.async_call(
            DOMAIN, "import_history",
            {"start_date": history_day.isoformat(), "end_date": history_day.isoformat()},
            blocking=True, return_response=True,
        )
        if set(response["streams"]) != set(expected) or response["skipped_meters"] or response["unimported_streams"]:
            raise RuntimeError("Imported streams differ from source")
        if any(summary["incomplete_hours"] for summary in response["streams"].values()):
            raise RuntimeError("Imported history is incomplete")
        async with asyncio.timeout(30):
            while True:
                rows = await get_instance(hass).async_add_executor_job(
                    statistics_during_period, hass, start_utc, start_utc + timedelta(days=1),
                    set(expected), "hour", None, {"sum", "change"},
                )
                if all(rows.get(stream) for stream in expected):
                    break
                await asyncio.sleep(0.1)
        for stream, hourly in expected.items():
            actual = rows[stream]
            if len(actual) != 24:
                raise RuntimeError("Stored history has incomplete hours")
            for row in actual:
                hour = datetime.fromtimestamp(row["start"], timezone.utc)
                if hour not in hourly or abs(row["change"] - float(hourly[hour])) > 0.000001:
                    raise RuntimeError("Stored hourly energy differs from source")
            if abs(sum(row["change"] for row in actual) - float(sum(hourly.values()))) > 0.000001:
                raise RuntimeError("Stored daily energy differs from source")
        passed("Historical hourly changes and totals match source in HA Recorder: passed")

        stage = "restart"
        await hass.async_stop()
        hass = None
        hass = await start()
        entry = await loaded(hass)
        if entry.data.get("last_login_attempt") != login_attempt:
            raise RuntimeError("Restart required another login")
        if not entry.runtime_data.last_update_success:
            raise RuntimeError("Restart did not resume discovery")
        passed("HA restart with persisted session, without another login: passed")
        passed("Live HA check passed; two fresh automatic logins completed")
    except Exception as error:
        # Do not include arbitrary exception text, flow data or response bodies.
        passed("Live HA check failed at " + stage + ": " + type(error).__name__)
        raise SystemExit(1) from None
    finally:
        if hass is not None:
            await hass.async_stop()
