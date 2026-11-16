"""Standalone async myEnergy client, using a caller-owned aiohttp session."""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import time
from urllib.parse import urlencode

from . import _imap, _portal as portal
from ._session import export_cookies, restore_cookies
from .errors import (
    AuthenticationError, AuthenticationExpired, ChallengeRequired, DataUnavailable,
    MyEnergyError, UnexpectedResponse,
)
from .nem12 import parse_nem12, identifier


@dataclass(frozen=True)
class Meter:
    """Private access metadata for one supply point, not a physical stream."""

    nmi: str = field(repr=False)
    company: str

    @property
    def id(self):
        return identifier("supply", self.nmi)


class MyEnergyClient:
    """Owns authentication state, never the session's lifecycle or file storage.

    Operations are serialized because the temporary Aura token is consumed.
    Session state and meter metadata are private; callers must not log them.
    """

    def __init__(self, session):
        self._session = session
        self._lock = asyncio.Lock()
        self._pending = None
        self._challenge = None
        self._form = None

    def export_state(self):
        if self._lock.locked() or self._pending is not None:
            raise ChallengeRequired("finish_client_operation_before_export")
        return export_cookies(self._session.cookie_jar)

    def restore_state(self, state):
        if self._lock.locked() or self._pending is not None:
            raise AuthenticationError("client_operation_in_progress")
        restore_cookies(self._session.cookie_jar, state)
        self._form = None  # Context is refreshed from the portal, never replayed.

    async def start_login(self, email, password):
        """Submit credentials and request one email code for manual completion."""
        async with self._lock:
            await self._prepare_login(email, password)
            await self._request_challenge()

    async def submit_otp(self, code):
        """Complete the pending challenge; no automatic retries or resends."""
        async with self._lock:
            await self._submit_otp(code)

    async def login(self, email, password, *, imap_config=None):
        """Coordinate direct login and IMAP OTP retrieval.

        Without IMAP, request a code and raise ChallengeRequired; the caller
        can then pass a manually entered code to submit_otp().
        """
        async with self._lock:
            if imap_config is not None:
                _imap.validate_config(imap_config)
            await self._prepare_login(email, password)
            if imap_config is None:
                await self._request_challenge()
                raise ChallengeRequired("email_otp_required")
            mailbox = None
            opening = waiting = None
            try:
                opening = asyncio.create_task(asyncio.to_thread(_imap.open_mailbox, imap_config))
                mailbox, validity, first_uid = await asyncio.shield(opening)
                requested = await self._request_challenge()
                waiting = asyncio.create_task(asyncio.to_thread(
                    _imap.wait_for_code, mailbox, imap_config, validity, first_uid, requested))
                code = await asyncio.shield(waiting)
            except BaseException:
                self._pending = self._challenge = None
                raise
            finally:
                # Cancelling an await cannot stop a thread. Join pending IMAP
                # work before logout, so it cannot race cleanup or leak a login.
                if waiting is not None and not waiting.done():
                    try:
                        await waiting
                    except MyEnergyError:
                        pass
                if mailbox is None and opening is not None:
                    try:
                        mailbox, _, _ = await opening
                    except MyEnergyError:
                        pass
                if mailbox is not None:
                    await asyncio.to_thread(_imap.close_mailbox, mailbox)
            await self._submit_otp(code)

    async def _prepare_login(self, email, password):
        if self._pending is not None:
            raise ChallengeRequired("email_challenge_already_pending")
        if not isinstance(email, str) or not email or not isinstance(password, str) or not password:
            raise ValueError("Nonempty email and password are required")
        self._form = None
        try:
            form = await portal.context_form(self._session, "/s/login/")
            result = await portal.aura(self._session, form, "LightningLoginFormController", "communityLogin",
                                       portal.login_parameters({"username": email, "password": password}))
            _, page = await portal.fetch(self._session, portal.frontdoor_url(result))
            if b"new $VFRM.RemotingProviderImpl(" not in page:
                _, page = await portal.fetch(self._session, portal.mfa_redirect(page))
            self._pending = portal.mfa_page(page)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise UnexpectedResponse("invalid_login_response") from None

    async def _request_challenge(self):
        try:
            provider, methods, _ = self._pending
            requested = datetime.now(timezone.utc).replace(microsecond=0)
            challenge = await portal.remote(self._session, provider, methods, "initiateMFA", ["email"], 1)
            identifier_value = challenge["verificationIdentifier"]
            registration = challenge["isRegistration"]
            if not isinstance(identifier_value, str) or not identifier_value or type(registration) is not bool:
                raise UnexpectedResponse("unexpected_mfa_challenge")
            if registration:
                raise ChallengeRequired("mfa_registration_requires_browser")
            self._challenge = identifier_value
            return requested
        except (ValueError, KeyError, TypeError, AttributeError):
            self._pending = self._challenge = None
            raise UnexpectedResponse("invalid_mfa_challenge") from None
        except BaseException:
            self._pending = self._challenge = None
            raise

    async def _submit_otp(self, code):
        if self._pending is None or self._challenge is None:
            raise ChallengeRequired("no_pending_email_challenge")
        if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ValueError("OTP must be six ASCII digits")
        try:
            provider, methods, completion = self._pending
            await portal.remote(self._session, provider, methods, "verifyMFA",
                                [code, self._challenge, False, "email"], 2)
            url, page = await portal.fetch(self._session, completion["action"], data=completion["fields"])
            if portal.page_requires_login(url, page):
                raise AuthenticationExpired("authentication_expired")
            self._form = await portal.context_form(self._session, "/s/energy-usage")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise UnexpectedResponse("invalid_mfa_response") from None
        finally:
            self._pending = self._challenge = None

    async def _authenticated_context(self):
        if self._pending is not None:
            raise ChallengeRequired("finish_pending_email_challenge")
        try:
            self._form = await portal.context_form(self._session, "/s/energy-usage")
            # Renew, update the timer, then confirm independently. The session
            # probe's pre-renewal timer read is diagnostic, not needed here.
            renewed = await portal.aura_action(self._session, self._form, {
                "id": "901;a", "descriptor": portal.SESSION_REFRESH_ACTION, "params": {},
            })
            if renewed is not True:
                raise UnexpectedResponse("session_renewal_rejected")
            await self._timer(update=True)
            await self._timer()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise UnexpectedResponse("invalid_session_response") from None

    async def _timer(self, *, update=False):
        params = {"buster": str(time.time_ns() // 1_000_000)}
        if update:
            params["updateTimedOutSession"] = "true"
        url, body = await portal.fetch(self._session, portal.SESSION_TIME_URL + "?" + urlencode(params))
        if portal.page_requires_login(url, body):
            raise AuthenticationExpired("authentication_expired")
        value = portal.decode_object(body)
        if any(type(value.get(key)) is not int for key in ("sp", "sr")):
            raise UnexpectedResponse("invalid_session_timer")
        if value["sr"] <= 0:
            raise AuthenticationExpired("authentication_expired")

    async def _action(self, classname, method, params):
        try:
            value = await portal.aura(self._session, self._form, classname, method, params)
            inner = value["returnValue"]
            if inner.get("isSuccess") is not True or not isinstance(inner.get("data"), dict):
                raise UnexpectedResponse("unexpected_service_result")
            return inner["data"]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise UnexpectedResponse("invalid_service_response") from None

    async def get_meters(self):
        """Retrieve only the supply-point metadata needed to request readings."""
        async with self._lock:
            await self._authenticated_context()
            data = await self._action("DashboardController", "getEnergyUsageData",
                                      {"nmiAssignId": None, "noOfLimit": 50, "isDashboard": True})
            try:
                assignments = data["nmiAssignmentsForEnergyUsage"]
                total = data["totalCountNmiAssignsForEnergyUsage"]
                if not isinstance(assignments, list) or type(total) is not int or total < len(assignments):
                    raise UnexpectedResponse("invalid_meter_assignments")
                if total > len(assignments):
                    raise DataUnavailable("meter_assignment_list_incomplete")
                meters = []
                for assignment in assignments:
                    nmi = assignment["NMI__r"]["Name"]
                    if not isinstance(nmi, str) or not nmi:
                        raise UnexpectedResponse("invalid_meter_identifier")
                    account = await self._action("EnergyInformationController", "retrieveAccountHolderInfo", {"nmi": nmi})
                    company = account["companyName"]
                    if company not in ("Powercor", "CitiPower"):
                        raise UnexpectedResponse("unsupported_meter_company")
                    meters.append(Meter(nmi, company))
                return meters
            except (ValueError, KeyError, TypeError, AttributeError):
                raise UnexpectedResponse("invalid_meter_metadata") from None

    async def get_readings(self, meter, start, end, *, channel_kinds=None):
        """Retrieve inclusive NEM/AEST dates and return UTC interval kWh.

        E1/B1 have observed import/export semantics. Other suffixes require
        an explicit channel_kinds mapping; controlled load is never guessed.
        """
        if type(start) is not date or type(end) is not date or end < start:
            raise ValueError("Expected ordered inclusive dates")
        if not isinstance(meter, Meter):
            raise TypeError("Expected meter metadata from get_meters")
        async with self._lock:
            await self._authenticated_context()
            data = await self._action("EnergyInformationController", "getMeterData", {
                "nmi": meter.nmi, "formatType": "Customer Access NEM12",
                "startDate": start.isoformat(),
                # The portal rejects equal date bounds; filter the extra day below.
                "endDate": (end + timedelta(days=1) if end == start else end).isoformat(),
                "company": meter.company, "jobId": 0,
            })
            try:
                body = data["body"]
                if body.get("status") != "Success" or body.get("formatType") != "Customer Access NEM12" or body.get("nmi") != meter.nmi:
                    raise UnexpectedResponse("unexpected_interval_result")
                text = body["nmiData"]
                if not isinstance(text, str):
                    raise UnexpectedResponse("invalid_interval_data")
                if not text:
                    raise DataUnavailable("no_interval_data")
            except (ValueError, KeyError, TypeError, AttributeError):
                raise UnexpectedResponse("invalid_interval_response") from None
            return parse_nem12(text, nmi=meter.nmi, start=start, end=end, channel_kinds=channel_kinds)
