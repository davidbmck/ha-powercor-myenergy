"""Private development entry point for the standalone client.

Run with ``python -m tools.probe_login``. No response payloads or IDs are logged.
"""

import argparse
import asyncio
from getpass import getpass
import json
from pathlib import Path
import ssl
import sys

import aiohttp

from myenergy import MyEnergyClient
from myenergy import _imap, _portal
from myenergy.errors import MyEnergyError, MailboxError
from tools._private import require_private_file


class ProbeError(Exception):
    """Fixed development-input errors, safe to display."""


def load_credentials(path):
    try:
        require_private_file(path)
    except (OSError, ValueError):
        raise ProbeError("credentials_file_must_be_private_outside_repository") from None
    try:
        credentials = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ProbeError(f"credentials_file_invalid_json:line={exc.lineno}:column={exc.colno}") from None
    except (OSError, UnicodeError):
        raise ProbeError("credentials_file_unreadable") from None
    if not isinstance(credentials, dict) or set(credentials) != {"username", "password"}:
        raise ProbeError("credentials_file_requires_username_and_password")
    if any(not isinstance(value, str) or not value for value in credentials.values()):
        raise ProbeError("credentials_file_requires_nonempty_strings")
    return credentials


def load_imap_config(path):
    try:
        require_private_file(path)
        return _imap.validate_config(json.loads(path.read_text()))
    except (OSError, ValueError):
        raise MailboxError("imap_config_must_be_private_json_outside_repository") from None


async def run(args):
    tls = ssl.create_default_context()
    if args.intermediate_ca:
        tls.load_verify_locations(args.intermediate_ca)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls)) as session:
        if args.bootstrap_only:
            await _portal.context_form(session, "/s/login/")
            print("guest_bootstrap_ready", flush=True)
            return
        if not sys.stdin.isatty() and not (args.credentials_file and args.imap_config):
            raise ProbeError("interactive_terminal_required")
        credentials = load_credentials(args.credentials_file) if args.credentials_file else {
            "username": getpass("myEnergy email (hidden): "), "password": getpass("myEnergy password: ")}
        client = MyEnergyClient(session)
        if args.imap_config:
            await client.login(credentials["username"], credentials["password"],
                               imap_config=load_imap_config(args.imap_config))
        else:
            await client.start_login(credentials["username"], credentials["password"])
            await client.submit_otp(getpass("Email OTP: "))
        del credentials
        print("login_completed", flush=True)
        meters = await client.get_meters()
        print(f"meter_assignments_retrieved:{len(meters)}", flush=True)
        print("authenticated_discovery_succeeded", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intermediate-ca", type=Path)
    parser.add_argument("--bootstrap-only", action="store_true")
    parser.add_argument("--credentials-file", type=Path,
                        help="Owner-only JSON file outside the repository, containing username and password")
    parser.add_argument("--imap-config", type=Path,
                        help="Owner-only IMAP configuration outside the repository; fetch the OTP automatically")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (ProbeError, MyEnergyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (aiohttp.ClientError, TimeoutError, ssl.SSLError):
        print("transport_or_tls_error", file=sys.stderr)
        return 2
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError) as exc:
        # Report only a local function name, never exception text or upstream
        # values. This makes live protocol mismatches actionable without logs.
        frame = exc.__traceback__
        stage = "run"
        while frame:
            if frame.tb_frame.f_code.co_filename == __file__:
                stage = frame.tb_frame.f_code.co_name
            frame = frame.tb_next
        print(f"unexpected_portal_response:{stage}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("login_cancelled", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
