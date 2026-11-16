"""Small, manual probe for myEnergy session reuse.

The cookie file is a private JSON export stored outside this repository.
No response body, cookie value, or customer identifier is printed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.cookiejar
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPCookieProcessor, Request, build_opener


# Preserve direct script execution used by the existing session trial.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myenergy._portal import (
    ORIGIN, ENERGY_USAGE, AURA_PATH, SESSION_TIME_URL, SESSION_REFRESH_ACTION,
    page_requires_login, inline_bootstrap_url as _inline_bootstrap_url,
    decode_object, bootstrap_context,
)
from myenergy.errors import UnexpectedResponse
from tools._private import REPOSITORY, require_private_file

RETRY_WAIT_SECONDS = 10


def load_request(path: Path) -> Request:
    require_private_file(path)
    payload = json.loads(path.read_text())
    url = payload["url"]
    body = payload["body"]
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "myenergy.powercor.com.au" or parts.path != AURA_PATH:
        raise ValueError("Request must target the myEnergy Aura endpoint")
    if not isinstance(body, str):
        raise ValueError("Request body must be form-encoded text")
    form = parse_qs(body)
    message = json.loads(form["message"][0])
    actions = message.get("actions", [])
    if len(actions) != 1:
        raise ValueError("Expected one meter-data action")
    params = actions[0].get("params", {})
    if params.get("classname") != "EnergyInformationController" or params.get("method") != "getMeterData":
        raise ValueError("Request must call getMeterData")
    if params.get("params", {}).get("formatType") != "Customer Access NEM12":
        raise ValueError("Request must ask for NEM12 data")
    return Request(
        url,
        data=body.encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": ENERGY_USAGE,
            "Origin": ORIGIN,
            "User-Agent": "myEnergy session probe/0.1",
        },
    )


def meter_response_outcome(body: bytes) -> str:
    text = body.decode("utf-8").strip()
    if text.startswith("*/"):
        text = text[2:]
    try:
        response, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return "unexpected_meter_response"
    if text[end:].strip() not in ("", "/*ERROR*/") or not isinstance(response, dict):
        return "unexpected_meter_response"
    if (response.get("event") or {}).get("descriptor") == "markup://aura:invalidSession":
        return "api_context_invalid"
    actions = response.get("actions", [])
    if len(actions) != 1:
        return "unexpected_meter_response"
    if actions[0].get("state") == "ERROR":
        # Observed after a long idle gap. Access denial is not proof of expiry,
        # but replaying this same request cannot establish a live session.
        for error in actions[0].get("error") or []:
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"].lower()
                if "do not have access" in message and "apex class" in message:
                    return "meter_access_denied"
        return "meter_action_error"
    if actions[0].get("state") != "SUCCESS":
        return "unexpected_meter_response"
    outer = actions[0].get("returnValue") or {}
    inner = outer.get("returnValue") or {}
    data = inner.get("data") or {}
    meter = data.get("body") or {}
    if inner.get("isSuccess") and meter.get("status") == "Success" and meter.get("nmiData"):
        return "meter_data_retrieved"
    return "unexpected_meter_response"


def refresh_meter_request(request: Request, page: bytes, jar: http.cookiejar.CookieJar) -> None:
    """Mirror the observed page bootstrap without executing its JavaScript."""
    try:
        config, refreshed = bootstrap_context(page)
    except UnexpectedResponse as exc:
        raise ValueError(str(exc)) from None
    cookie_name = config["eikoocnekot"]
    if not isinstance(cookie_name, str) or not cookie_name:
        raise ValueError("Invalid Aura bootstrap configuration")
    tokens = [cookie for cookie in jar if cookie.name == cookie_name
              and cookie.domain.lstrip(".") == "myenergy.powercor.com.au" and not cookie.is_expired()]
    if len(tokens) != 1 or not tokens[0].value:
        raise ValueError("Fresh Aura token cookie is unavailable")
    token = tokens[0]
    form = parse_qs(request.data.decode(), keep_blank_values=True)
    form["aura.context"] = [json.dumps(refreshed, separators=(",", ":"))]
    form["aura.token"] = [token.value]
    form["aura.pageURI"] = [urlsplit(ENERGY_USAGE).path]
    request.data = urlencode(form, doseq=True).encode()
    # The browser consumes and deletes this temporary cookie in the same way.
    jar.clear(token.domain, token.path, token.name)


def inline_bootstrap_url(page: bytes) -> str:
    try:
        return _inline_bootstrap_url(page)
    except UnexpectedResponse as exc:
        raise ValueError(str(exc)) from None


def renewal_json(body: bytes) -> dict:
    try:
        return decode_object(body)
    except UnexpectedResponse as exc:
        raise ValueError(str(exc)) from None


def renew_session(opener, meter_request: Request, diagnostics: dict) -> tuple[int, str]:
    """Observed Continue working sequence; success still needs a meter check."""
    def fetch(request):
        with opener.open(request, timeout=20) as response:
            body = response.read(64_001)
            if page_requires_login(response.geturl(), body):
                return None
            if response.status != 200 or len(body) > 64_000:
                raise ValueError("Unexpected renewal response")
            return renewal_json(body)

    def timer(stage, update=False):
        query = {"buster": str(time.time_ns() // 1_000_000)}
        if update:
            query["updateTimedOutSession"] = "true"
        value = fetch(Request(SESSION_TIME_URL + "?" + urlencode(query), headers={
            "User-Agent": "myEnergy session probe/0.1", "Referer": ENERGY_USAGE,
        }))
        if value is None:
            return False
        # Only sanitized numeric durations enter logs, never response payloads.
        if any(type(value.get(key)) is not int for key in ("sp", "sr")):
            raise ValueError("Unexpected session timing")
        diagnostics[stage] = {"warning_seconds": value["sp"], "remaining_seconds": value["sr"]}
        return value["sr"] > 0

    try:
        if not timer("before"):
            return 1, "reauthentication_required"
        form = parse_qs(meter_request.data.decode(), keep_blank_values=True)
        form["message"] = [json.dumps({"actions": [{
            "id": "901;a", "descriptor": SESSION_REFRESH_ACTION, "params": {},
        }]}, separators=(",", ":"))]
        request = Request(meter_request.full_url, data=urlencode(form, doseq=True).encode(),
                          headers=dict(meter_request.header_items()))
        result = fetch(request)
        if result is None:
            return 1, "reauthentication_required"
        if (result.get("event") or {}).get("descriptor") == "markup://aura:invalidSession":
            return 2, "api_context_invalid"
        actions = result.get("actions")
        if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
            return 2, "unexpected_renewal_response"
        action = actions[0]
        if action.get("state") == "ERROR":
            return 2, "session_renewal_rejected"
        if action.get("state") != "SUCCESS" or action.get("returnValue") is not True:
            return 2, "unexpected_renewal_response"
        if not timer("update", update=True) or not timer("after"):
            return 1, "reauthentication_required"
        # The timer may already be at its maximum. Do not require an increase.
        return 0, "session_renewed"
    except HTTPError as exc:
        exc.close()
        return 2, f"renewal_http_error:{exc.code}"
    except (URLError, TimeoutError, ConnectionError):
        return 2, "renewal_transport_error"
    except (ValueError, KeyError, TypeError, AttributeError):
        return 2, "unexpected_renewal_response"


def retryable_outcome(outcome: str) -> bool:
    if outcome in {"transport_error", "meter_transport_error", "context_transport_error", "renewal_transport_error", "meter_action_error"}:
        return True
    if outcome.startswith(("upstream_http_error:", "meter_http_error:", "context_http_error:", "renewal_http_error:")):
        status = int(outcome.rsplit(":", 1)[1])
        return status == 429 or 500 <= status <= 599
    return False


def load_cookies(cookie_file: Path) -> http.cookiejar.CookieJar:
    require_private_file(cookie_file)

    raw_cookies = json.loads(cookie_file.read_text())
    if not isinstance(raw_cookies, list):
        raise ValueError("Cookie file must contain a JSON array")
    jar = http.cookiejar.CookieJar()
    for item in raw_cookies:
        if not isinstance(item, dict) or item.get("domain", "").lstrip(".") != "myenergy.powercor.com.au":
            raise ValueError("Cookie file contains an invalid domain")
        expires = item.get("expires")
        if expires is not None and expires <= 0:
            expires = None  # Browser session cookie.
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=item["name"],
                value=item["value"],
                port=None,
                port_specified=False,
                domain=item["domain"],
                domain_specified=not item.get("hostOnly", not item["domain"].startswith(".")),
                domain_initial_dot=item["domain"].startswith("."),
                path=item.get("path", "/"),
                path_specified=True,
                secure=item.get("secure", True),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )
    return jar


def validate_cookie_state(path: Path, *protected_paths: Path | None) -> None:
    if not path.is_absolute() or path.resolve().is_relative_to(REPOSITORY):
        raise ValueError("Keep cookie state outside the repository at an absolute path")
    if any(path.resolve() == protected.resolve() for protected in protected_paths if protected is not None):
        raise ValueError("Cookie state must not overwrite an input or trial log")
    if path.is_symlink():
        raise ValueError("Cookie state must not be a symlink")
    if path.exists():
        require_private_file(path)


def save_cookies(jar: http.cookiejar.CookieJar, path: Path, cookie_file: Path) -> None:
    validate_cookie_state(path, cookie_file)
    payload = [{
        "name": cookie.name, "value": cookie.value, "domain": cookie.domain,
        "path": cookie.path, "secure": cookie.secure, "expires": cookie.expires,
        "hostOnly": not cookie.domain_specified,
    } for cookie in jar if cookie.domain.lstrip(".") == "myenergy.powercor.com.au"
       and not cookie.is_expired()]
    # Atomic owner-only snapshots preserve session cookies for an explicit resume.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(payload, output)
            output.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def probe_session(
    cookie_file: Path,
    intermediate_ca: Path | None = None,
    request_file: Path | None = None,
    *,
    jar: http.cookiejar.CookieJar | None = None,
    refresh_context: bool = False,
    renew: bool = False,
    renewal_diagnostics: list[dict] | None = None,
) -> tuple[int, str]:
    if renew and request_file is None:
        raise ValueError("Session renewal requires a meter request")
    if jar is None:
        jar = load_cookies(cookie_file)
    context = ssl.create_default_context()
    if intermediate_ca is not None:
        context.load_verify_locations(cafile=str(intermediate_ca))
    opener = build_opener(HTTPCookieProcessor(jar), HTTPSHandler(context=context))
    request = Request(ENERGY_USAGE, headers={"User-Agent": "myEnergy session probe/0.1"})

    try:
        with opener.open(request, timeout=20) as response:
            final_url = response.geturl()
            status = response.status
            # Read a bounded amount, then discard it. The portal may return personal data.
            body = response.read(2_000_001)
    except HTTPError as exc:
        return 2, f"upstream_http_error:{exc.code}"
    except URLError:
        return 2, "transport_error"

    if page_requires_login(final_url, body):
        return 1, "reauthentication_required"
    if status == 200 and final_url.startswith(ENERGY_USAGE):
        if request_file is None:
            # The page shell is also served without cookies.
            return 0, "page_reachable_auth_unverified"
        meter_request = load_request(request_file)
        if refresh_context or renew:
            if len(body) > 2_000_000:
                return 2, "api_context_unavailable"
            try:
                if not re.search(rb"\bauraConfig\s*=\s*", body):
                    bootstrap = Request(inline_bootstrap_url(body), headers={
                        "User-Agent": "myEnergy session probe/0.1", "Referer": ENERGY_USAGE,
                    })
                    with opener.open(bootstrap, timeout=20) as response:
                        body = response.read(2_000_001)
                        if page_requires_login(response.geturl(), body):
                            return 1, "reauthentication_required"
                        if response.status != 200 or len(body) > 2_000_000:
                            return 2, "api_context_unavailable"
                refresh_meter_request(meter_request, body, jar)
            except HTTPError as exc:
                return 2, f"context_http_error:{exc.code}"
            except URLError:
                return 2, "context_transport_error"
            except (ValueError, KeyError, TypeError):
                return 2, "api_context_unavailable"
        if renew:
            detail = {}
            code, outcome = renew_session(opener, meter_request, detail)
            detail["result"] = outcome
            if renewal_diagnostics is not None:
                renewal_diagnostics.append(detail)
            if code != 0:
                return code, outcome
        try:
            with opener.open(meter_request, timeout=30) as response:
                if response.geturl().startswith(f"{ORIGIN}/s/login"):
                    return 1, "reauthentication_required"
                if response.status != 200:
                    return 2, "unexpected_meter_response"
                meter_body = response.read(16_000_000)
        except HTTPError as exc:
            return 2, f"meter_http_error:{exc.code}"
        except URLError:
            return 2, "meter_transport_error"
        outcome = meter_response_outcome(meter_body)
        return (0 if outcome == "meter_data_retrieved" else 2), outcome
    return 2, "unexpected_response"


def probe_with_retry(
    cookie_file: Path,
    intermediate_ca: Path | None,
    request_file: Path | None,
    *,
    jar: http.cookiejar.CookieJar | None = None,
    cookie_state_file: Path | None = None,
    refresh_context: bool = False,
    renew: bool = False,
    renewal_diagnostics: list[dict] | None = None,
) -> tuple[int, str, str | None]:
    if jar is None:
        jar = load_cookies(cookie_file)
    if cookie_state_file is not None:
        validate_cookie_state(cookie_state_file, cookie_file, request_file, intermediate_ca)
    try:
        code, outcome = probe_session(cookie_file, intermediate_ca, request_file, jar=jar, refresh_context=refresh_context, renew=renew, renewal_diagnostics=renewal_diagnostics)
        if code == 2 and retryable_outcome(outcome):
            initial_outcome = outcome
            sleep_until(time.monotonic() + RETRY_WAIT_SECONDS)
            code, outcome = probe_session(cookie_file, intermediate_ca, request_file, jar=jar, refresh_context=refresh_context, renew=renew, renewal_diagnostics=renewal_diagnostics)
            return code, outcome, initial_outcome
        return code, outcome, None
    finally:
        if cookie_state_file is not None:
            save_cookies(jar, cookie_state_file, cookie_file)


def check_session(
    cookie_file: Path, intermediate_ca: Path | None = None,
    request_file: Path | None = None, cookie_state_file: Path | None = None,
    refresh_context: bool = False,
    renew: bool = False,
) -> int:
    renewal_diagnostics = []
    code, outcome, initial_outcome = probe_with_retry(
        cookie_file, intermediate_ca, request_file, cookie_state_file=cookie_state_file,
        refresh_context=refresh_context, renew=renew, renewal_diagnostics=renewal_diagnostics,
    )
    if renew:
        print(json.dumps({"renewal_attempts": renewal_diagnostics}))
    if initial_outcome is not None:
        print(f"initial_result:{initial_outcome}")
    print(outcome)
    return code


def write_event(log_file: Path, event: dict[str, object]) -> None:
    line = json.dumps(event, separators=(",", ":")) + "\n"
    if not log_file.is_absolute() or log_file.resolve().is_relative_to(REPOSITORY):
        raise ValueError("Keep the trial log outside the repository")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(log_file, flags, 0o600)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError("Trial log must be a private regular file (chmod 600)")
        os.write(fd, line.encode())
    finally:
        os.close(fd)
    print(line, end="", flush=True)


def sleep_until(deadline: float) -> None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


def run_trial(
    trial: str,
    hours: float | None,
    every_minutes: float | None,
    cookie_file: Path,
    intermediate_ca: Path | None,
    request_file: Path,
    log_file: Path,
    initial_minutes: float = 5,
    cookie_state_file: Path | None = None,
    refresh_context: bool = False,
    renew: bool = False,
) -> int:
    # Validate private inputs before a long idle wait, without contacting myEnergy.
    jar = load_cookies(cookie_file)
    load_request(request_file)
    if cookie_state_file is not None:
        validate_cookie_state(cookie_state_file, cookie_file, request_file, intermediate_ca, log_file)
    if intermediate_ca is not None:
        ssl.create_default_context().load_verify_locations(cafile=str(intermediate_ca))

    started = time.monotonic()
    duration = hours * 3600 if trial == "active" else None
    write_event(log_file, {
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "event": "trial_started",
        "trial": trial,
        "duration_seconds": duration,
        "interval_seconds": every_minutes * 60 if trial == "active" else None,
        "initial_wait_seconds": initial_minutes * 60 if trial == "idle" else None,
        "refresh_context": refresh_context or renew,
        "renew_session": renew,
    })

    def attempt(wait_seconds: float | None = None) -> int:
        initial_outcome = None
        renewal_diagnostics = []
        try:
            code, outcome, initial_outcome = probe_with_retry(
                cookie_file, intermediate_ca, request_file,
                jar=jar, cookie_state_file=cookie_state_file,
                refresh_context=refresh_context, renew=renew, renewal_diagnostics=renewal_diagnostics,
            )
        except (ValueError, KeyError, TypeError, OSError, ssl.SSLError):
            code, outcome = 2, "probe_error"
        event = {
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "event": "probe",
            "trial": trial,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "result": outcome,
        }
        if renew:
            event["renewal_attempts"] = renewal_diagnostics
        if wait_seconds is not None:
            event["wait_seconds"] = wait_seconds
        if initial_outcome is not None:
            event["initial_result"] = initial_outcome
            event["retry_wait_seconds"] = RETRY_WAIT_SECONDS
        write_event(log_file, event)
        return code

    try:
        if trial == "idle":
            wait_seconds = initial_minutes * 60
            next_time = started + wait_seconds
            while True:
                sleep_until(next_time)
                code = attempt(wait_seconds)
                if code != 0:
                    return code
                wait_seconds *= 2
                next_time += wait_seconds

        interval = every_minutes * 60  # Validated by main().
        deadline = started + duration
        next_time = started
        while True:
            sleep_until(next_time)
            code = attempt()
            if code != 0 or next_time >= deadline:
                return code
            now = time.monotonic()
            step = max(math.floor((now - started) / interval) + 1, 1)
            next_time = min(started + step * interval, deadline)
    except KeyboardInterrupt:
        write_event(log_file, {
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "event": "trial_interrupted",
            "trial": trial,
            "elapsed_seconds": round(time.monotonic() - started, 1),
        })
        return 130


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cookie_file", type=Path, help="Absolute path to a private JSON cookie export")
    parser.add_argument("--intermediate-ca", type=Path, help="PEM intermediate CA if the server omits it")
    parser.add_argument("--request-file", type=Path, help="Private JSON file with captured NEM12 request URL and body")
    parser.add_argument("--cookie-state-file", type=Path, help="Optional private output file for updated cookies; distinct from the input export")
    parser.add_argument("--refresh-context", action="store_true", help="Derive fresh Aura context and token from the page before every meter request")
    parser.add_argument("--renew-session", action="store_true", help="Run the observed Continue working renewal sequence before each meter check; implies --refresh-context")
    parser.add_argument("--trial", choices=("idle", "active"), help="idle: doubling waits; active: fixed intervals")
    parser.add_argument("--hours", type=float, help="Active-trial duration measured from process start")
    parser.add_argument("--every-minutes", type=float, help="Active-trial interval")
    parser.add_argument("--initial-minutes", type=float, help="First idle wait; later waits double (default: 5)")
    parser.add_argument("--log-file", type=Path, help="Private JSONL trial log outside the repository")
    args = parser.parse_args()
    if (args.refresh_context or args.renew_session) and args.request_file is None:
        parser.error("--refresh-context and --renew-session require --request-file")
    if args.trial:
        if args.request_file is None or args.log_file is None:
            parser.error("timed trials require --request-file and --log-file")
        if args.trial == "active":
            if args.hours is None or not math.isfinite(args.hours) or args.hours <= 0:
                parser.error("active trials require positive --hours")
            if args.every_minutes is None or not math.isfinite(args.every_minutes) or args.every_minutes <= 0:
                parser.error("active trials require positive --every-minutes")
            if args.every_minutes * 60 >= args.hours * 3600:
                parser.error("active interval must be shorter than trial duration")
            if args.initial_minutes is not None:
                parser.error("--initial-minutes applies only to idle trials")
        else:
            if args.hours is not None or args.every_minutes is not None:
                parser.error("--hours and --every-minutes apply only to active trials")
            if args.initial_minutes is not None and (not math.isfinite(args.initial_minutes) or args.initial_minutes <= 0):
                parser.error("--initial-minutes must be positive")
    elif any(value is not None for value in (args.hours, args.every_minutes, args.initial_minutes, args.log_file)):
        parser.error("timing and log options require --trial")
    try:
        if args.trial:
            return run_trial(
                args.trial, args.hours, args.every_minutes,
                args.cookie_file, args.intermediate_ca, args.request_file, args.log_file,
                args.initial_minutes if args.initial_minutes is not None else 5,
                args.cookie_state_file,
                args.refresh_context, args.renew_session,
            )
        return check_session(args.cookie_file, args.intermediate_ca, args.request_file, args.cookie_state_file, args.refresh_context, args.renew_session)
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError, ssl.SSLError):
        # File errors can include private paths; parser errors can echo values.
        print("probe_error: input, TLS or response validation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
