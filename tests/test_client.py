"""Client transitions and service responses using synthetic protocol fixtures."""

import asyncio
from datetime import date
from http.cookies import SimpleCookie
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from yarl import URL

from myenergy import (
    AuthenticationExpired, ChallengeRequired, ChannelKind, MailboxError, Meter, MyEnergyClient,
    UnexpectedResponse,
)
from myenergy import _imap, _portal
from myenergy._session import export_cookies, restore_cookies
from test_imap_otp import CONFIG
from test_nem12 import nem12, stream, DAY
from test_probe_login import mfa_fixture

ORIGIN = URL(_portal.ORIGIN)


def service(data):
    return {"returnValue": {"isSuccess": True, "data": data}}


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_restored_session_refreshes_context_renews_and_returns_normalized_period(self):
        jar = aiohttp.CookieJar()
        jar.update_cookies({"sid": "synthetic-session"}, response_url=ORIGIN)
        state = json.loads(json.dumps(export_cookies(jar)))
        client = MyEnergyClient(SimpleNamespace(cookie_jar=aiohttp.CookieJar()))
        client.restore_state(state)
        calls = []

        async def context(session, path):
            self.assertEqual(session.cookie_jar.filter_cookies(ORIGIN)["sid"].value, "synthetic-session")
            calls.append("context")
            return {"aura.token": "synthetic-fresh-token"}

        async def timer(_session, url):
            calls.append("timer-update" if "updateTimedOutSession=true" in url else "timer")
            return url, b'while(1);{"sp":7170,"sr":7200}'

        async def renew(_session, form, action):
            self.assertEqual(form["aura.token"], "synthetic-fresh-token")
            self.assertEqual(action["descriptor"], _portal.SESSION_REFRESH_ACTION)
            calls.append("renew")
            return True

        async def action(_session, form, classname, method, params):
            calls.append(method)
            if method == "getEnergyUsageData":
                return service({"nmiAssignmentsForEnergyUsage": [{"NMI__r": {"Name": "nmi-supply"}}],
                                "totalCountNmiAssignsForEnergyUsage": 1})
            if method == "retrieveAccountHolderInfo":
                return service({"companyName": "Powercor"})
            self.assertEqual(params["nmi"], "nmi-supply")
            self.assertEqual(params["endDate"], "2026-10-05")
            return service({"body": {"status": "Success", "formatType": "Customer Access NEM12",
                                     "nmi": "nmi-supply", "nmiData": nem12(stream())}})

        with (patch("myenergy.client.portal.context_form", side_effect=context),
              patch("myenergy.client.portal.fetch", side_effect=timer),
              patch("myenergy.client.portal.aura_action", side_effect=renew),
              patch("myenergy.client.portal.aura", side_effect=action)):
            meters = await client.get_meters()
            readings = await client.get_readings(meters[0], DAY, DAY)
        sequence = ["context", "renew", "timer-update", "timer"]
        self.assertEqual(calls, sequence + ["getEnergyUsageData", "retrieveAccountHolderInfo"] + sequence + ["getMeterData"])
        self.assertEqual(len(readings), 48)
        self.assertNotIn("nmi-supply", repr(meters))

    async def test_expired_context_and_changed_response_have_distinct_safe_errors(self):
        client = MyEnergyClient(SimpleNamespace(cookie_jar=aiohttp.CookieJar()))
        with patch("myenergy.client.portal.context_form", side_effect=AuthenticationExpired("reauthentication_required")):
            with self.assertRaises(AuthenticationExpired):
                await client.get_meters()
        with (patch.object(client, "_authenticated_context", new_callable=AsyncMock),
              patch("myenergy.client.portal.aura", return_value={"private": "synthetic private detail"})):
            with self.assertRaisesRegex(UnexpectedResponse, "^invalid_service_response$"):
                await client.get_meters()

    async def test_expired_timer_after_renewal_stops_before_meter_access(self):
        client = MyEnergyClient(SimpleNamespace(cookie_jar=aiohttp.CookieJar()))
        with (patch("myenergy.client.portal.context_form", new_callable=AsyncMock),
              patch("myenergy.client.portal.aura_action", return_value=True),
              patch("myenergy.client.portal.fetch", side_effect=[
                  (str(ORIGIN), b'{"sp":7170,"sr":7200}'),
                  (str(ORIGIN), b'{"sp":7170,"sr":0}'),
              ]), patch("myenergy.client.portal.aura", new_callable=AsyncMock) as action):
            with self.assertRaises(AuthenticationExpired):
                await client.get_meters()
            action.assert_not_called()

    async def test_invalid_channel_mapping_remains_a_caller_error(self):
        client = MyEnergyClient(SimpleNamespace(cookie_jar=aiohttp.CookieJar()))
        data = {"body": {"status": "Success", "formatType": "Customer Access NEM12",
                         "nmi": "nmi-supply", "nmiData": nem12(stream())}}
        with (patch.object(client, "_authenticated_context", new_callable=AsyncMock),
              patch.object(client, "_action", return_value=data)):
            for mapping in ([], {"E1": ChannelKind.GRID_EXPORT}):
                with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                    await client.get_readings(Meter("nmi-supply", "Powercor"), DAY, DAY, channel_kinds=mapping)

    async def test_manual_login_retains_one_challenge_until_submission(self):
        client = MyEnergyClient(SimpleNamespace(cookie_jar=aiohttp.CookieJar()))
        with (patch("myenergy.client.portal.context_form", new_callable=AsyncMock),
              patch("myenergy.client.portal.aura", return_value=service({"loginUrl": "/secur/frontdoor.jsp"})),
              patch("myenergy.client.portal.fetch", return_value=(str(ORIGIN), mfa_fixture())),
              patch("myenergy.client.portal.remote", side_effect=[
                  {"verificationIdentifier": "synthetic-challenge", "isRegistration": False}, {"success": True},
              ]) as remote):
            with self.assertRaises(ChallengeRequired):
                await client.login("user@example.invalid", 'synthetic"password')
            with self.assertRaises(ChallengeRequired):
                client.export_state()
            with self.assertRaises(ChallengeRequired):
                await client.start_login("user@example.invalid", "synthetic-password")
            with self.assertRaises(ValueError):
                await client.submit_otp("not-six-digits")
            await client.submit_otp("123456")
            self.assertEqual([call.args[3] for call in remote.call_args_list], ["initiateMFA", "verifyMFA"])
            client.export_state()

    async def test_coordinated_imap_baseline_precedes_send_and_failure_never_verifies(self):
        async def worker(function, *args):
            return function(*args)

        for fail in (False, True):
            order = []
            client = MyEnergyClient(SimpleNamespace(cookie_jar=aiohttp.CookieJar()))
            async def prepare(*_args):
                client._pending = ({}, {}, {"action": "/MFA_Enabler", "fields": {}})
            async def remote(_session, _provider, _methods, method, _data, _tid):
                order.append(method)
                return {"success": True, "verificationIdentifier": "synthetic-challenge", "isRegistration": False}
            def wait(*_args):
                order.append("wait")
                if fail:
                    raise MailboxError("imap_otp_timeout")
                return "123456"
            def open_inbox(_config):
                order.append("baseline")
                return object(), 7, 10
            with (self.subTest(fail=fail), patch.object(client, "_prepare_login", side_effect=prepare),
                  patch("myenergy.client.asyncio.to_thread", side_effect=worker),
                  patch("myenergy.client._imap.open_mailbox", side_effect=open_inbox),
                  patch("myenergy.client._imap.wait_for_code", side_effect=wait),
                  patch("myenergy.client._imap.close_mailbox", side_effect=lambda _: order.append("close")),
                  patch("myenergy.client.portal.remote", side_effect=remote),
                  patch("myenergy.client.portal.fetch", return_value=(str(ORIGIN), b"completed")),
                  patch("myenergy.client.portal.context_form", new_callable=AsyncMock)):
                if fail:
                    with self.assertRaises(MailboxError):
                        await client.login("user@example.invalid", "synthetic-password", imap_config=CONFIG)
                else:
                    await client.login("user@example.invalid", "synthetic-password", imap_config=CONFIG)
                client.export_state()
            expected = ["baseline", "initiateMFA", "wait", "close"]
            self.assertEqual(order, expected if fail else expected + ["verifyMFA"])

    async def test_cancelled_login_joins_mailbox_open_before_cleanup(self):
        opened, release = asyncio.Event(), asyncio.Event()
        mailbox = object()
        order = []
        client = MyEnergyClient(SimpleNamespace(cookie_jar=aiohttp.CookieJar()))
        async def worker(function, *args):
            if function is _imap.open_mailbox:
                opened.set()
                await release.wait()
                order.append("opened")
                return mailbox, 7, 10
            self.assertIs(function, _imap.close_mailbox)
            self.assertIs(args[0], mailbox)
            order.append("closed")
        with (patch.object(client, "_prepare_login", new_callable=AsyncMock),
              patch("myenergy.client.asyncio.to_thread", side_effect=worker),
              patch("myenergy.client.portal.remote", new_callable=AsyncMock) as remote):
            task = asyncio.create_task(client.login("user@example.invalid", "synthetic-password", imap_config=CONFIG))
            await opened.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            remote.assert_not_called()
        self.assertEqual(order, ["opened", "closed"])
        client.export_state()


class SessionStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_round_trip_preserves_scope_and_absolute_max_age(self):
        jar = aiohttp.CookieJar()
        cookie = SimpleCookie()
        cookie["sid"] = "synthetic-session"
        cookie["sid"]["max-age"] = "120"
        cookie["sid"]["secure"] = True
        cookie["sid"]["httponly"] = True
        jar.update_cookies(cookie, response_url=ORIGIN)
        jar.update_cookies({"other": "synthetic-other"}, response_url=URL("https://example.invalid/"))
        cookie = SimpleCookie("path-cookie=synthetic; Domain=powercor.com.au; Path=/s; Secure")
        jar.update_cookies(cookie, response_url=ORIGIN)
        state = json.loads(json.dumps(export_cookies(jar)))
        self.assertEqual(len(state["cookies"]), 2)
        deadline = next(c["expires"] for c in state["cookies"] if c["name"] == "sid")
        with patch("myenergy._session.time.time", return_value=time.time() + 30):
            restored = aiohttp.CookieJar()
            restore_cookies(restored, state)
            snapshot = export_cookies(restored)
        restored_deadline = next(c["expires"] for c in snapshot["cookies"] if c["name"] == "sid")
        self.assertAlmostEqual(deadline, restored_deadline, delta=1)
        self.assertNotIn("sid", restored.filter_cookies(URL("https://sub.myenergy.powercor.com.au/")))
        self.assertNotIn("sid", restored.filter_cookies(URL("http://myenergy.powercor.com.au/")))
        self.assertNotIn("path-cookie", restored.filter_cookies(ORIGIN))
        self.assertIn("path-cookie", restored.filter_cookies(URL(str(ORIGIN) + "/s/energy-usage")))
        with self.assertRaises(ValueError):
            restore_cookies(restored, {"version": 1, "cookies": [{**state["cookies"][0], "domain": "example.invalid"}]})
        self.assertIn("sid", restored.filter_cookies(ORIGIN))  # Validation is atomic.
        restore_cookies(restored, {"version": 1, "cookies": []})
        self.assertEqual(list(restored), [])
