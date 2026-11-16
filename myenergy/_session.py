"""JSON-safe cookie state; callers own storage and must keep it private."""

from email.utils import formatdate
from http.cookies import CookieError, SimpleCookie
import math
import time

import aiohttp
from yarl import URL

from ._portal import ORIGIN


HOST = "myenergy.powercor.com.au"


def portal_domain(domain):
    domain = domain.lstrip(".").lower()
    return domain == HOST or bool(domain) and HOST.endswith("." + domain)


def export_cookies(jar):
    if not isinstance(jar, aiohttp.CookieJar):
        raise ValueError("A real aiohttp CookieJar is required")
    cookies = []
    for cookie in jar:  # Iteration also expires elapsed cookies.
        domain, path, name = cookie["domain"], cookie["path"], cookie.key
        if not portal_domain(domain):
            continue
        # aiohttp exposes no public JSON snapshot API. These two attributes
        # preserve host-only scope and absolute Max-Age deadlines across restart.
        expires = jar._expirations.get((domain, path.rstrip("/"), name))
        if expires is None:
            expires = jar._expirations.get((domain, path, name))
        cookies.append({"name": name, "value": cookie.value, "domain": domain,
                        "path": path, "secure": bool(cookie["secure"]),
                        "http_only": bool(cookie["httponly"]), "same_site": cookie["samesite"],
                        "host_only": any(key in jar._host_only_cookies for key in (
                            (domain, name), (domain, path.rstrip("/"), name), (domain, path, name))),
                        "expires": expires})
    return {"version": 1, "cookies": cookies}


def restore_cookies(jar, state):
    if not isinstance(jar, aiohttp.CookieJar):
        raise ValueError("A real aiohttp CookieJar is required")
    prepared = []
    try:
        if not isinstance(state, dict) or type(state.get("version")) is not int or state.get("version") != 1 or not isinstance(state.get("cookies"), list):
            raise ValueError
        for item in state["cookies"]:
            if not isinstance(item, dict):
                raise ValueError
            for key in ("name", "value", "domain", "path", "same_site"):
                if not isinstance(item[key], str):
                    raise ValueError
            if not item["name"] or not item["path"].startswith("/") or not portal_domain(item["domain"]):
                raise ValueError
            if any(type(item[key]) is not bool for key in ("secure", "http_only", "host_only")):
                raise ValueError
            if item["host_only"] and item["domain"] != HOST:
                raise ValueError
            expires = item["expires"]
            if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires)):
                raise ValueError
            if expires is not None and expires <= time.time():
                continue
            cookie = SimpleCookie()
            cookie[item["name"]] = item["value"]
            morsel = cookie[item["name"]]
            morsel["domain"] = "" if item["host_only"] else item["domain"]
            for key, target in (("path", "path"), ("secure", "secure"), ("http_only", "httponly"), ("same_site", "samesite")):
                morsel[target] = item[key]
            if expires is not None:
                morsel["expires"] = formatdate(expires, usegmt=True)
            prepared.append((cookie, item["path"]))
    except (KeyError, TypeError, ValueError, OverflowError, CookieError):
        raise ValueError("Invalid myEnergy session state") from None
    jar.clear(lambda cookie: portal_domain(cookie["domain"]))
    for cookie, path in prepared:
        jar.update_cookies(cookie, response_url=URL(ORIGIN + path))
