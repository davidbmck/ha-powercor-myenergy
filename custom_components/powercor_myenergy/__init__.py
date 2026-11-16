"""Powercor / CitiPower myEnergy integration."""

from datetime import date, datetime, timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import CoreState, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started

from .const import CONF_RETRY_START, DOMAIN, MAX_SYNC_DAYS
from .coordinator import MyEnergyCoordinator, async_create_session
from .myenergy import AuthenticationError, MailboxError, MyEnergyError, TransportError
from .statistics import AEST, async_import_history

PLATFORMS = [Platform.SENSOR]


async def async_setup(hass, config):
    async def import_history(call):
        entries = [entry for entry in hass.config_entries.async_entries(DOMAIN)
                   if entry.state == ConfigEntryState.LOADED]
        selected = call.data.get("config_entry_id")
        if selected:
            entries = [entry for entry in entries if entry.entry_id == selected]
        if len(entries) != 1:
            raise HomeAssistantError("Select one loaded myEnergy integration entry")
        entry = entries[0]
        start, end = call.data["start_date"], call.data["end_date"]
        try:
            result = await async_import_history(hass, entry.runtime_data, start, end)
        except (ConfigEntryAuthFailed, AuthenticationError) as err:
            raise HomeAssistantError("myEnergy authentication needs attention") from err
        except MailboxError as err:
            if str(err) == "imap_authentication_failed":
                raise HomeAssistantError("Update the OTP mailbox credentials") from err
            raise HomeAssistantError("Automatic email verification could not complete") from err
        except TransportError as err:
            raise HomeAssistantError("myEnergy is temporarily unavailable") from err
        except MyEnergyError as err:
            raise HomeAssistantError("Unexpected response from myEnergy") from err
        retry_start = entry.data.get(CONF_RETRY_START)
        cutoff = datetime.now(AEST).date() - timedelta(days=MAX_SYNC_DAYS)
        if (result["complete"] and retry_start and date.fromisoformat(retry_start) < cutoff
                and start <= date.fromisoformat(retry_start) and end >= cutoff - timedelta(days=1)):
            saved = dict(entry.data)
            saved.pop(CONF_RETRY_START)
            hass.config_entries.async_update_entry(entry, data=saved)
        return result

    hass.services.async_register(
        DOMAIN, "import_history", import_history,
        schema=vol.Schema({
            vol.Required("start_date"): cv.date,
            vol.Required("end_date"): cv.date,
            vol.Optional("config_entry_id"): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass, entry):
    session = await async_create_session(hass)
    coordinator = MyEnergyCoordinator(hass, entry, session)

    async def close_session(event):
        await session.close()

    async def refresh_after_start(started_hass):
        await coordinator.async_request_refresh()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_session))
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if hass.state != CoreState.running:
            entry.async_on_unload(async_at_started(hass, refresh_after_start))
    except BaseException:
        await session.close()
        raise
    return True


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.session.close()
        return True
    return False
