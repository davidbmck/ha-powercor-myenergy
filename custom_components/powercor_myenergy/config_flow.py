"""UI setup and reauthentication; OTPs remain transient flow input."""

import asyncio
import hashlib

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_AUTOMATIC, CONF_IMAP, CONF_SESSION, DOMAIN
from .coordinator import async_create_session
from .tariff_flow import TariffOptionsFlow
from .myenergy import (
    AuthenticationError, MailboxError, MyEnergyClient, MyEnergyError, TransportError,
)

TEXT = selector.TextSelector()
EMAIL = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL))
PASSWORD = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))


class MyEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TariffOptionsFlow()

    def __init__(self):
        self._data = {}
        self._session = None
        self._client = None
        self._login_task = None
        self._errors = {}

    async def _async_client(self):
        if self._session is None:
            self._session = await async_create_session(self.hass)
            self._client = MyEnergyClient(self._session)
        return self._client

    async def _async_close(self):
        if self._session is not None:
            await self._session.close()
            self._session = self._client = None

    @callback
    def async_remove(self):
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
            self.hass.async_create_task(self._async_cancel_login())
        elif self._session is not None:
            self.hass.async_create_task(self._async_close())

    async def _async_cancel_login(self):
        # The client joins bounded IMAP work before cancellation completes.
        try:
            await self._login_task
        except (asyncio.CancelledError, MyEnergyError):
            pass
        finally:
            await self._async_close()

    async def async_step_reauth(self, entry_data):
        self._data = dict(entry_data)
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            await self._async_close()
            self._data.update(user_input)
            unique_id = hashlib.sha256(self._data[CONF_USERNAME].strip().casefold().encode()).hexdigest()
            await self.async_set_unique_id(unique_id)
            if self.source != "reauth":
                self._abort_if_unique_id_configured()
            elif self._get_reauth_entry().unique_id != unique_id:
                return self.async_abort(reason="wrong_account")
            if self._data[CONF_AUTOMATIC]:
                return await self.async_step_mailbox()
            self._data.pop(CONF_IMAP, None)
            try:
                client = await self._async_client()
                await client.start_login(self._data[CONF_USERNAME], self._data[CONF_PASSWORD])
                return await self.async_step_otp()
            except MyEnergyError as err:
                await self._async_failed(err)
        schema = vol.Schema({
            vol.Required(CONF_USERNAME, default=self._data.get(CONF_USERNAME, "")): EMAIL,
            vol.Required(CONF_PASSWORD): PASSWORD,
            vol.Required(CONF_AUTOMATIC, default=self._data.get(CONF_AUTOMATIC, True)): selector.BooleanSelector(),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=self._take_errors())

    async def async_step_mailbox(self, user_input=None):
        if user_input is not None:
            self._data[CONF_IMAP] = user_input
            self._login_task = self.hass.async_create_task(self._async_automatic_login())
            return await self.async_step_authenticate()
        previous = self._data.get(CONF_IMAP, {})
        defaults = {
            "host": "imap.gmail.com", "username": self._data.get(CONF_USERNAME, ""),
            "folder": "INBOX", "sender": "no-reply@powercor.com.au", "recipient": "",
            "subject": "{code} is your one-time verification code.",
        }
        schema = {}
        for key, default in defaults.items():
            schema[vol.Required(key, default=previous.get(key, default))] = EMAIL if key in ("sender", "recipient") else TEXT
        schema[vol.Required(CONF_PASSWORD)] = PASSWORD
        return self.async_show_form(step_id="mailbox", data_schema=vol.Schema(schema), errors=self._take_errors())

    async def _async_automatic_login(self):
        client = await self._async_client()
        await client.login(self._data[CONF_USERNAME], self._data[CONF_PASSWORD],
                           imap_config=self._data[CONF_IMAP])
        if not await client.get_meters():
            raise MyEnergyError("no_accessible_meters")

    async def async_step_authenticate(self, user_input=None):
        if not self._login_task.done():
            return self.async_show_progress(step_id="authenticate", progress_action="login",
                                            progress_task=self._login_task)
        try:
            self._login_task.result()
        except MyEnergyError as err:
            await self._async_failed(err)
            next_step = "mailbox" if isinstance(err, MailboxError) else "user"
            return self.async_show_progress_done(next_step_id=next_step)
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_otp(self, user_input=None):
        if user_input is not None:
            try:
                await self._client.submit_otp(user_input["code"])
                if not await self._client.get_meters():
                    raise MyEnergyError("no_accessible_meters")
                return await self.async_step_finish()
            except (MyEnergyError, ValueError) as err:
                await self._async_failed(err)
                return await self.async_step_user()
        return self.async_show_form(step_id="otp", data_schema=vol.Schema({vol.Required("code"): PASSWORD}))

    async def async_step_finish(self, user_input=None):
        try:
            data = {**self._data, CONF_SESSION: self._client.export_state()}
        finally:
            await self._async_close()
        unique_id = hashlib.sha256(data[CONF_USERNAME].strip().casefold().encode()).hexdigest()
        await self.async_set_unique_id(unique_id)
        if self.source == "reauth":
            entry = self._get_reauth_entry()
            if entry.unique_id != unique_id:
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(entry, data=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="myEnergy", data=data)

    async def _async_failed(self, error):
        await self._async_close()
        if isinstance(error, TransportError):
            key = "cannot_connect"
        elif isinstance(error, MailboxError):
            key = "mailbox_error"
        elif isinstance(error, (AuthenticationError, ValueError)):
            key = "invalid_auth"
        else:
            key = "unexpected_response"
        self._errors = {"base": key}

    def _take_errors(self):
        errors, self._errors = self._errors, {}
        return errors
