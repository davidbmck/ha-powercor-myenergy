"""Minimal synthetic login/MFA shapes; no credentials or customer data."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlencode

import aiohttp
from yarl import URL

from myenergy._portal import (
    aura, bootstrap_form, fetch, frontdoor_url, login_parameters, mfa_page,
    mfa_redirect, portal_url, remote,
)
from myenergy.errors import (
    MyEnergyError as ProbeError, AccessDenied, AuthenticationExpired, TransportError, UnexpectedResponse,
)
from tools.probe_login import load_credentials, ProbeError as InputError


CONTEXT = {"mode": "PROD", "app": "siteforce:loginApp2", "fwuid": "synthetic-framework",
           "loaded": {"APPLICATION@markup://siteforce:loginApp2": "synthetic-version"}, "uad": 0}
PROVIDER = {
    "service": "apexremote", "vf": {"vid": "synthetic-page"},
    "actions": {"MFAEnablerController": {"ms": [
        {"name": name, "len": count, "csrf": "synthetic-csrf", "ns": "",
         "ver": 60, "authorization": "synthetic-authorization"}
        for name, count in (("initiateMFA", 1), ("verifyMFA", 4))
    ]}},
}


def mfa_fixture():
    state = "".join(f'<input type="hidden" name="com.salesforce.visualforce.{name}" value="synthetic-{name}">'
                    for name in ("ViewState", "ViewStateVersion", "ViewStateMAC", "ViewStateCSRF"))
    return (f'<script>Visualforce.remoting.Manager.add(new $VFRM.RemotingProviderImpl({json.dumps(PROVIDER)}));</script>'
            '<form name="form-example" action="/MFA_Enabler" method="post">'
            '<input type="hidden" name="form-example" value="form-example"></form>' + state +
            '<script>function redirectToHome() {jsfcljs(document.forms["form-example"],'
            '"form-example:continue,form-example:continue","");return false}</script>').encode()


class LoginParsingTests(unittest.IsolatedAsyncioTestCase):
    def test_frontdoor_script_only_follows_expected_same_origin_mfa_target(self):
        target = "/apex/MFA_Enabler?retURL=%2Fapex%2FCommunitiesLanding"
        page = ('function lhdoredir(){window.location.replace?window.location.replace('
                + json.dumps(target) + '):window.location.href=' + json.dumps(target) + '}').encode()
        self.assertEqual(mfa_redirect(page), "https://myenergy.powercor.com.au" + target)
        for url in ("https://example.invalid/apex/MFA_Enabler", "/secur/logout.jsp"):
            with self.subTest(url=url), self.assertRaises(ProbeError):
                mfa_redirect(('window.location.replace(' + json.dumps(url) + ')').encode())

    async def test_login_brands_email_and_preserves_quoted_password_on_the_wire(self):
        credentials = {"username": "user@example.invalid", "password": 'synthetic"quote\\and&plus+'}
        with patch("myenergy._portal.fetch", new_callable=AsyncMock) as request:
            request.return_value = ("unused", b'{"context":{"app":"siteforce:loginApp2"},"actions":[{"state":"SUCCESS","returnValue":{}}]}')
            value = await aura(None, {"aura.token": "null"}, "LightningLoginFormController", "communityLogin",
                               login_parameters(credentials))
            self.assertEqual(value, {})
            wire = urlencode(request.call_args.kwargs["data"])
            message = json.loads(parse_qs(wire)["message"][0])
            params = message["actions"][0]["params"]["params"]
            self.assertEqual(params["username"], "user@example.invalid.VPN")
            self.assertEqual(params["password"], credentials["password"])
            self.assertEqual(credentials["username"], "user@example.invalid")

    def test_credentials_file_requires_private_permissions_and_exact_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "login.json"
            credentials = {"username": "synthetic-user", "password": "synthetic-password"}
            path.write_text(json.dumps(credentials))
            os.chmod(path, 0o600)
            self.assertEqual(load_credentials(path), credentials)
            os.chmod(path, 0o644)
            with self.assertRaises(InputError):
                load_credentials(path)
            os.chmod(path, 0o600)
            path.write_text('{"username":"synthetic-user"}')
            with self.assertRaises(InputError):
                load_credentials(path)
            path.write_text('{"username":"synthetic-user", "password":"synthetic"quote"}')
            with self.assertRaisesRegex(InputError, "^credentials_file_invalid_json:line=1:column="):
                load_credentials(path)

    async def test_streamed_responses_are_complete_and_external_redirects_are_not_followed(self):
        async def chunks(_size):
            yield b"first part"
            yield b" last part"

        session = MagicMock()
        response = MagicMock(status=200)
        response.content.iter_chunked = chunks
        session.request.return_value.__aenter__ = AsyncMock(return_value=response)
        _, body = await fetch(session, "https://myenergy.powercor.com.au/s/login/")
        self.assertEqual(body, b"first part last part")
        session.request.reset_mock()
        response.status = 302
        response.headers = {"Location": "https://example.invalid/"}
        with self.assertRaisesRegex(ProbeError, "unexpected_redirect_origin"):
            await fetch(session, "https://myenergy.powercor.com.au/s/login/")
        session.request.assert_called_once()

    async def test_http_and_aura_failures_have_typed_safe_outcomes(self):
        session = MagicMock()
        response = MagicMock()
        session.request.return_value.__aenter__ = AsyncMock(return_value=response)
        for status, error in ((401, AuthenticationExpired), (403, AccessDenied),
                              (429, TransportError), (503, TransportError), (418, UnexpectedResponse)):
            response.status = status
            with self.subTest(status=status), self.assertRaises(error):
                await fetch(session, "https://myenergy.powercor.com.au/s/energy-usage")
        session.request.side_effect = aiohttp.ClientError("synthetic private detail")
        with self.assertRaisesRegex(TransportError, "^portal_transport_failed$"):
            await fetch(session, "https://myenergy.powercor.com.au/s/energy-usage")
        with patch("myenergy._portal.fetch", new_callable=AsyncMock) as request:
            request.return_value = ("unused", b'*/{"event":{"descriptor":"markup://aura:invalidSession"}}/*ERROR*/')
            with self.assertRaises(AuthenticationExpired):
                await aura(None, {}, "ExampleController", "example", {})
            request.return_value = ("unused", b'{"actions":[{"state":"ERROR","error":[{"message":"You do not have access to the Apex class"}]}]}')
            with self.assertRaises(AccessDenied):
                await aura(None, {}, "ExampleController", "example", {})

    async def test_guest_and_authenticated_context_use_different_token_sources(self):
        jar = aiohttp.CookieJar()
        script = ("var auraConfig = " + json.dumps({"context": CONTEXT})).encode()
        form = bootstrap_form(script, jar, "/s/login/")
        self.assertEqual(form["aura.token"], "null")
        self.assertEqual(json.loads(form["aura.context"])["loaded"], CONTEXT["loaded"])
        jar.update_cookies({"synthetic-token": "synthetic-value", "sid": "synthetic-session"},
                           response_url=URL("https://myenergy.powercor.com.au/"))
        script = ("var auraConfig = " + json.dumps({"context": CONTEXT, "eikoocnekot": "synthetic-token"})).encode()
        self.assertEqual(bootstrap_form(script, jar, "/s/energy-usage")["aura.token"], "synthetic-value")
        self.assertEqual([cookie.key for cookie in jar], ["sid"])
        with self.assertRaises(ProbeError):
            bootstrap_form(script, jar, "/s/energy-usage")

    def test_frontdoor_target_is_extracted_without_executing_javascript(self):
        result = {"returnValue": {"isSuccess": True, "data": {
            "loginUrl": "/secur/frontdoor.jsp?sid=synthetic-session"}}}
        self.assertEqual(frontdoor_url(result), "https://myenergy.powercor.com.au/secur/frontdoor.jsp?sid=synthetic-session")
        for value in ({"error": "synthetic rejection"}, "https://example.invalid/secur/frontdoor.jsp"):
            with self.subTest(value=value), self.assertRaises(ProbeError):
                frontdoor_url(value)
        with self.assertRaises(ProbeError):
            portal_url("//example.invalid/")

    def test_login_refusal_is_distinct_from_success_with_changed_navigation(self):
        refusal = {"returnValue": {"isSuccess": False, "data": {},
                    "statusMessage": "Synthetic private detail: invalid password"}}
        with self.assertRaisesRegex(ProbeError, "^login_rejected$"):
            frontdoor_url(refusal)
        refusal["returnValue"]["isSuccess"] = True
        with self.assertRaisesRegex(ProbeError, "^unexpected_login_response$"):
            frontdoor_url(refusal)

    def test_mfa_completion_retains_generated_action_and_all_viewstate_fields(self):
        provider, methods, completion = mfa_page(mfa_fixture())
        self.assertEqual(provider["service"], "apexremote")
        self.assertEqual(methods["verifyMFA"]["len"], 4)
        self.assertEqual(completion["action"], "https://myenergy.powercor.com.au/MFA_Enabler")
        self.assertEqual(completion["fields"]["form-example:continue"], "form-example:continue")
        self.assertEqual(len(completion["fields"]), 6)
        with self.assertRaisesRegex(ProbeError, "mfa_viewstate_missing"):
            mfa_page(mfa_fixture().replace(b"com.salesforce.visualforce.ViewStateMAC", b"missing"))

    def test_mfa_completion_accepts_newlines_before_generated_form_action(self):
        page = mfa_fixture().replace(b"redirectToHome() {jsfcljs", b"redirectToHome() {\n  jsfcljs")
        self.assertEqual(mfa_page(page), mfa_page(mfa_fixture()))

    async def test_challenge_response_is_tied_to_its_request_and_errors_are_safe(self):
        methods = {m["name"]: m for m in PROVIDER["actions"]["MFAEnablerController"]["ms"]}
        result = {"success": True, "verificationIdentifier": "synthetic-challenge", "isRegistration": False}
        response = [{"type": "rpc", "tid": 1, "method": "initiateMFA", "result": result}]
        with patch("myenergy._portal.fetch", new_callable=AsyncMock) as fetch:
            fetch.return_value = ("unused", json.dumps(response).encode())
            self.assertEqual(await remote(None, PROVIDER, methods, "initiateMFA", ["email"], 1), result)
            payload = fetch.call_args.kwargs["json_body"]
            self.assertEqual(payload["ctx"]["vid"], "synthetic-page")
            self.assertEqual(payload["data"], ["email"])
            response[0]["tid"] = 2
            fetch.return_value = ("unused", json.dumps(response).encode())
            with self.assertRaisesRegex(ProbeError, "unexpected_mfa_response"):
                await remote(None, PROVIDER, methods, "initiateMFA", ["email"], 1)
            response[0]["tid"] = 1
            response[0]["result"] = {"success": False, "message": "synthetic private server detail"}
            fetch.return_value = ("unused", json.dumps(response).encode())
            with self.assertRaisesRegex(ProbeError, "^mfa_rejected$"):
                await remote(None, PROVIDER, methods, "initiateMFA", ["email"], 1)


if __name__ == "__main__":
    unittest.main()
