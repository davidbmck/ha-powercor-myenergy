"""Keep the session usable and expose protected discovery status."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import ssl

import aiohttp
from homeassistant.core import CoreState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AUTOMATIC, CONF_IMAP, CONF_LAST_LOGIN, CONF_SESSION, DOMAIN,
    LOGIN_COOLDOWN, UPDATE_INTERVAL, HISTORY_INTERVAL, FIRST_SYNC_DAYS, RECENT_SYNC_DAYS,
    MAX_SYNC_DAYS,
    CONF_LAST_SYNC, CONF_RETRY_START,
)
from .myenergy import (
    AuthenticationError, AuthenticationExpired, ChallengeRequired,
    MailboxError, MyEnergyClient, MyEnergyError, TransportError,
)
from .statistics import AEST, async_import_history

_LOGGER = logging.getLogger(__name__)


async def async_create_session(hass):
    """Use a dedicated cookie jar; the caller closes this account's session."""
    def context():
        tls = ssl.create_default_context()
        # The observed portal omits this public intermediate from its chain.
        # Add it to normal roots, retaining certificate and hostname checks.
        tls.load_verify_locations(Path(__file__).with_name("intermediate.pem"))
        return tls

    tls = await hass.async_add_executor_job(context)
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls))


class MyEnergyCoordinator(DataUpdateCoordinator):
    """One refresh and at most one login attempt at a time."""

    def __init__(self, hass, entry, session):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=UPDATE_INTERVAL)
        self.session = session
        self.sync_lock = asyncio.Lock()
        self.auth_lock = asyncio.Lock()
        self.client = MyEnergyClient(session)
        state = entry.data.get(CONF_SESSION)
        if state:
            try:
                self.client.restore_state(state)
            except ValueError:
                # A bad snapshot can be replaced using the configured login.
                _LOGGER.warning("Stored myEnergy session is invalid")

    async def _async_update_data(self):
        try:
            meters = await self.async_authenticated_call(self.client.get_meters)
            if not meters:
                raise UpdateFailed("No accessible meters returned")
            now = datetime.now(timezone.utc)
            last_sync_text = self.config_entry.data.get(CONF_LAST_SYNC)
            last_sync = datetime.fromisoformat(last_sync_text) if last_sync_text else None
            result = None
            # Recorder's queue starts after HA startup. Do not wait for its
            # commit barrier during the config entry's first refresh.
            if self.hass.state == CoreState.running and (last_sync is None or now - last_sync >= HISTORY_INTERVAL):
                today = datetime.now(AEST).date()
                if last_sync is None:
                    start = today - timedelta(days=FIRST_SYNC_DAYS)
                else:
                    start = min(today - timedelta(days=RECENT_SYNC_DAYS),
                                last_sync.astimezone(AEST).date() - timedelta(days=1))
                if retry_start := self.config_entry.data.get(CONF_RETRY_START):
                    start = min(start, datetime.fromisoformat(retry_start).date())
                requested_start = start
                start = max(start, today - timedelta(days=MAX_SYNC_DAYS))
                result = await async_import_history(
                    self.hass, self, start, today - timedelta(days=1), meters=meters,
                )
                last_sync_text = now.isoformat()
            saved = {**self.config_entry.data, CONF_SESSION: self.client.export_state()}
            if result:
                if not result["complete"] or requested_start < start:
                    # Keep one unresolved start date for reporting and manual
                    # recovery, but never expand an automatic request past 30 days.
                    saved[CONF_RETRY_START] = requested_start.isoformat()
                else:
                    saved.pop(CONF_RETRY_START, None)
            if last_sync_text:
                saved[CONF_LAST_SYNC] = last_sync_text
            self.hass.config_entries.async_update_entry(self.config_entry, data=saved)
            previous = self.data or {}
            return {"last_update": now, "meter_count": len(meters),
                    "last_successful_sync": last_sync_text,
                    "latest_source_timestamp": (result or {}).get("latest_source_timestamp") or previous.get("latest_source_timestamp"),
                    "sync_complete": (result["complete"] and CONF_RETRY_START not in saved)
                    if result else previous.get("sync_complete"),
                    "skipped_meters": (result or {}).get("skipped_meters", previous.get("skipped_meters")),
                    "incomplete_hours": (result or {}).get("incomplete_hours", previous.get("incomplete_hours")),
                    "unpriced_hours": (result or {}).get("unpriced_hours", previous.get("unpriced_hours")),
                    "unresolved_history_start": saved.get(CONF_RETRY_START)}
        except (ConfigEntryAuthFailed, UpdateFailed):
            raise
        except (AuthenticationError, ChallengeRequired) as err:
            raise ConfigEntryAuthFailed("myEnergy verification is required") from err
        except MailboxError as err:
            if str(err) == "imap_authentication_failed":
                raise ConfigEntryAuthFailed("Update the OTP mailbox credentials") from err
            raise UpdateFailed("Automatic email verification could not complete; it will be retried") from err
        except TransportError as err:
            raise UpdateFailed("myEnergy or the OTP mailbox is temporarily unavailable") from err
        except MyEnergyError as err:
            raise UpdateFailed("Unexpected response from myEnergy") from err
        except HomeAssistantError as err:
            raise UpdateFailed("Historical myEnergy sync did not complete") from err

    async def async_authenticated_call(self, operation, *args, **kwargs):
        """Run one protected client operation, logging in once if it expired."""
        async with self.auth_lock:
            try:
                return await operation(*args, **kwargs)
            except AuthenticationExpired:
                await self._async_login()
                result = await operation(*args, **kwargs)
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, CONF_SESSION: self.client.export_state()},
                )
                return result

    async def _async_login(self):
        data = self.config_entry.data
        if not data.get(CONF_AUTOMATIC) or not data.get(CONF_IMAP):
            raise ConfigEntryAuthFailed("Complete email verification in the integration setup")
        now = datetime.now(timezone.utc).timestamp()
        last_attempt = data.get(CONF_LAST_LOGIN, 0)
        if now - last_attempt < LOGIN_COOLDOWN.total_seconds():
            raise UpdateFailed("Waiting before another myEnergy login attempt")
        # Persist before requesting a challenge: restart/setup retries must not
        # repeatedly send OTPs during a mailbox or portal outage.
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**data, CONF_LAST_LOGIN: now},
        )
        await self.client.login(data[CONF_USERNAME], data[CONF_PASSWORD],
                                imap_config=data[CONF_IMAP])
