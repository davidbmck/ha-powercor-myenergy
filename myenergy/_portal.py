"""Observed myEnergy/Salesforce wire protocol; no storage or user interaction."""

from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit

import aiohttp
from yarl import URL

from .errors import AuthenticationError, AuthenticationExpired, AccessDenied, TransportError, UnexpectedResponse

ORIGIN = "https://myenergy.powercor.com.au"
ENERGY_USAGE = ORIGIN + "/s/energy-usage"
AURA_PATH = "/s/sfsites/aura"
SESSION_TIME_URL = ORIGIN + "/_nc_external/system/security/session/SessionTimeServlet"
SESSION_REFRESH_ACTION = "serviceComponent://ui.identity.components.sessiontimeoutwarn.SessionTimeoutWarnController/ACTION$getSessionRefreshAction"

def page_requires_login(final_url: str, body: bytes) -> bool:
    if final_url.startswith(f"{ORIGIN}/s/login") or b"loginApp2" in body:
        return True
    # Observed HTTP-200 response for an expired session: the browser redirects
    # through redirectOnLoad, while urllib would otherwise accept the page.
    return all(marker in body for marker in (b"redirectOnLoad", b"location.replace", b"/s/login"))

def inline_bootstrap_url(page: bytes) -> str:
    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.sources = []

        def handle_starttag(self, tag, attrs):
            source = dict(attrs).get("src")
            if tag == "script" and source and urlsplit(source).path.endswith("/inline.js"):
                self.sources.append(urljoin(ENERGY_USAGE, source))

    parser = Scripts()
    parser.feed(page.decode("utf-8"))
    if len(parser.sources) != 1:
        raise UnexpectedResponse("Expected one Aura inline bootstrap script")
    url = parser.sources[0]
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "myenergy.powercor.com.au" or not parts.path.startswith("/s/sfsites/l/"):
        raise UnexpectedResponse("Unexpected Aura bootstrap URL")
    return url

def decode_object(body: bytes) -> dict:
    # Observed Salesforce anti-JSON-hijacking prefixes; never execute the body.
    text = body.decode("utf-8").strip()
    if text.startswith("while(1);"):
        text = text[len("while(1);"):].lstrip()
    elif text.startswith("*/"):
        text = text[2:]
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        raise UnexpectedResponse("invalid_json_response") from None
    if not isinstance(value, dict) or text[end:].strip() not in ("", "/*ERROR*/"):
        raise UnexpectedResponse("Unexpected renewal response")
    return value

def login_parameters(credentials):
    # The portal's communityLoginLwc appends its brand to the entered email.
    # c/utilities maps the Powercor/CitiPower portal to the literal "VPN".
    return {"username": credentials["username"] + ".VPN", "password": credentials["password"]}

def portal_url(value: str) -> str:
    url = urljoin(ORIGIN, value)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "myenergy.powercor.com.au":
        raise UnexpectedResponse("unexpected_redirect_origin")
    return url

async def fetch(session, url, *, data=None, json_body=None):
    try:
        return await _fetch(session, url, data=data, json_body=json_body)
    except (aiohttp.ClientError, TimeoutError):
        raise TransportError("portal_transport_failed") from None


async def _fetch(session, url, *, data=None, json_body=None):
    """Keep redirects on the portal, including redirects carrying session state."""
    method = "POST" if data is not None or json_body is not None else "GET"
    for _ in range(10):
        url = portal_url(url)
        async with session.request(method, url, data=data, json=json_body,
                                   allow_redirects=False,
                                   headers={"Origin": ORIGIN, "Referer": ORIGIN + "/s/login/",
                                            "User-Agent": "myEnergy Python client/0.1"},
                                   timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise UnexpectedResponse("redirect_without_location")
                url = portal_url(urljoin(url, location))
                if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                    method, data, json_body = "GET", None, None
                continue
            if response.status == 401:
                raise AuthenticationExpired("authentication_expired")
            if response.status == 403:
                raise AccessDenied("http_access_denied")
            if response.status == 429 or response.status >= 500:
                raise TransportError(f"upstream_http_error:{response.status}")
            if response.status != 200:
                raise UnexpectedResponse(f"unexpected_http_status:{response.status}")
            body = bytearray()
            async for chunk in response.content.iter_chunked(64_000):
                body.extend(chunk)
                if len(body) > 20_000_000:
                    raise UnexpectedResponse("response_too_large")
            return url, bytes(body)
    raise UnexpectedResponse("too_many_redirects")

def bootstrap_context(script: bytes):
    text = script.decode("utf-8")
    match = re.search(r"\bauraConfig\s*=\s*", text)
    if not match:
        raise UnexpectedResponse("bootstrap_config_missing")
    config, _ = json.JSONDecoder().raw_decode(text[match.end():])
    context = config["context"]
    slim = {key: context[key] for key in ("mode", "app", "fwuid", "loaded")}
    if not all(isinstance(slim[k], str) and slim[k] for k in ("mode", "app", "fwuid")):
        raise UnexpectedResponse("bootstrap_context_invalid")
    if not isinstance(slim["loaded"], dict) or type(context.get("uad")) not in (bool, int):
        raise UnexpectedResponse("bootstrap_context_invalid")
    slim.update(dn=[], globals={}, uad=bool(context["uad"]))
    return config, slim


def bootstrap_form(script: bytes, jar, page_path: str) -> dict:
    config, slim = bootstrap_context(script)
    # Observed guest login uses literal "null"; authenticated pages name a
    # temporary token cookie, consumed by the browser after bootstrap.
    token = "null"
    if page_path != "/s/login/":
        name = config["eikoocnekot"]
        cookies = jar.filter_cookies(URL(ENERGY_USAGE))
        if not isinstance(name, str) or not name or name not in cookies or not cookies[name].value:
            raise UnexpectedResponse("bootstrap_token_missing")
        token = cookies[name].value
        jar.clear(lambda cookie: cookie.key == name)
    return {"aura.context": json.dumps(slim), "aura.token": token, "aura.pageURI": page_path}

async def context_form(session, path):
    url, page = await fetch(session, ORIGIN + path)
    if path != "/s/login/" and page_requires_login(url, page):
        raise AuthenticationExpired("reauthentication_required")
    script = page if re.search(rb"\bauraConfig\s*=", page) else (await fetch(session, inline_bootstrap_url(page)))[1]
    return bootstrap_form(script, session.cookie_jar, path)

async def aura(session, form, classname, method, params):
    action = {"id": "1;a", "descriptor": "aura://ApexActionController/ACTION$execute",
              "params": {"classname": classname, "method": method, "params": params}}
    return await aura_action(session, form, action)


async def aura_action(session, form, action):
    url, body = await fetch(session, ORIGIN + AURA_PATH,
                            data={**form, "message": json.dumps({"actions": [action]})})
    # Guest Aura JSON can legitimately name loginApp2 in its context.
    # Only navigation/HTML is a login-page signal here.
    if (url.startswith(ORIGIN + "/s/login") or body.lstrip().startswith(b"<")) and page_requires_login(url, body):
        raise AuthenticationExpired("authentication_expired")
    response = decode_object(body)
    if (response.get("event") or {}).get("descriptor") == "markup://aura:invalidSession":
        raise AuthenticationExpired("api_context_invalid")
    actions = response.get("actions")
    if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
        raise UnexpectedResponse("unexpected_action_response")
    if actions[0].get("state") == "ERROR":
        # This exact access-denial wording was observed by the session probe;
        # it is actionable without assuming that the session itself expired.
        for error in actions[0].get("error") or []:
            message = error.get("message", "") if isinstance(error, dict) else ""
            if isinstance(message, str) and "do not have access" in message.lower() and "apex class" in message.lower():
                raise AccessDenied("meter_access_denied")
        raise UnexpectedResponse("action_rejected")
    if actions[0].get("state") != "SUCCESS":
        raise UnexpectedResponse("unexpected_action_state")
    return actions[0].get("returnValue")

def frontdoor_url(value):
    """Find only the expected navigation target; never execute response JS."""
    inner = value.get("returnValue") if isinstance(value, dict) else None
    if isinstance(inner, dict) and inner.get("isSuccess") is False:
        # The observed refusal flag is sufficient; never echo statusMessage.
        raise AuthenticationError("login_rejected")
    data = inner.get("data") if isinstance(inner, dict) else None
    url = data.get("loginUrl") if isinstance(data, dict) else None
    if not isinstance(inner, dict) or inner.get("isSuccess") is not True or not isinstance(url, str):
        raise UnexpectedResponse("unexpected_login_response")
    url = portal_url(url)
    if urlsplit(url).path != "/secur/frontdoor.jsp":
        raise UnexpectedResponse("unexpected_login_target")
    return url

def mfa_redirect(page):
    """Read the observed frontdoor lhdoredir target without running JS."""
    text = page.decode("utf-8")
    match = re.search(r"window\.location\.replace\(\s*", text)
    if not match:
        raise UnexpectedResponse("mfa_redirect_missing")
    value, end = json.JSONDecoder().raw_decode(text[match.end():])
    if not isinstance(value, str) or not text[match.end() + end:].lstrip().startswith(")"):
        raise UnexpectedResponse("unexpected_mfa_redirect")
    url = portal_url(value)
    if urlsplit(url).path != "/apex/MFA_Enabler":
        raise UnexpectedResponse("unexpected_mfa_redirect")
    return url

class MFAHTML(HTMLParser):
    """Only the fields needed to replay the observed Visualforce completion."""

    def __init__(self):
        super().__init__()
        self.forms = {}
        self.current = None
        self.viewstate = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = attrs.get("name") or attrs.get("id")
            self.forms[self.current] = {"action": portal_url(attrs["action"]), "fields": {}}
        if tag == "input" and attrs.get("type") == "hidden" and attrs.get("name"):
            name = attrs["name"]
            if self.current:
                self.forms[self.current]["fields"][name] = attrs.get("value", "")
            if name.startswith("com.salesforce.visualforce.ViewState"):
                self.viewstate[name] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None

def mfa_page(body):
    text = body.decode("utf-8")
    marker = "new $VFRM.RemotingProviderImpl("
    start = text.find(marker)
    if start < 0:
        raise UnexpectedResponse("mfa_provider_missing")
    provider, _ = json.JSONDecoder().raw_decode(text[start + len(marker):])
    if portal_url(provider["service"]) != ORIGIN + "/apexremote":
        raise UnexpectedResponse("unexpected_mfa_service")
    methods = {m["name"]: m for m in provider["actions"]["MFAEnablerController"]["ms"]}
    parser = MFAHTML()
    parser.feed(text)
    # The form/action IDs are generated; derive them from redirectToHome rather
    # than hard-coding IDs from a particular page version.
    match = re.search(r"function redirectToHome\(\).*?jsfcljs\(document.forms\[['\"]([^'\"]+)['\"]\],['\"]([^'\"]+)['\"]", text, flags=re.DOTALL)
    if not match or match[1] not in parser.forms:
        raise UnexpectedResponse("mfa_completion_form_missing")
    form = parser.forms[match[1]]
    if urlsplit(form["action"]).path != "/MFA_Enabler":
        raise UnexpectedResponse("unexpected_mfa_completion_target")
    parts = match[2].split(",")
    if len(parts) != 2:
        raise UnexpectedResponse("unexpected_mfa_completion_action")
    form["fields"].update(parser.viewstate)
    form["fields"][parts[0]] = parts[1]
    required = {"com.salesforce.visualforce." + key for key in
                ("ViewState", "ViewStateVersion", "ViewStateMAC", "ViewStateCSRF")}
    if not required.issubset(form["fields"]):
        raise UnexpectedResponse("mfa_viewstate_missing")
    return provider, methods, form

async def remote(session, provider, methods, method, data, tid):
    definition = methods[method]
    if definition["len"] != len(data):
        raise UnexpectedResponse("mfa_signature_changed")
    ctx = {key: definition[key] for key in ("csrf", "ns", "ver", "authorization")}
    ctx["vid"] = provider["vf"]["vid"]
    url, body = await fetch(session, ORIGIN + "/apexremote", json_body={
        "action": "MFAEnablerController", "method": method, "data": data,
        "type": "rpc", "tid": tid, "ctx": ctx,
    })
    if page_requires_login(url, body):
        raise AuthenticationExpired("authentication_expired")
    response = json.loads(body)
    if not isinstance(response, list) or len(response) != 1:
        raise UnexpectedResponse("unexpected_mfa_response")
    action = response[0]
    if not isinstance(action, dict) or action.get("type") != "rpc" or action.get("tid") != tid or action.get("method") != method:
        raise UnexpectedResponse("unexpected_mfa_response")
    result = action.get("result")
    if not isinstance(result, dict) or result.get("success") is not True:
        raise AuthenticationError("mfa_rejected")
    return result
