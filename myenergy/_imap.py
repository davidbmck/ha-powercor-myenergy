"""Transient, read-only email OTP retrieval; no file access or logging."""

from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
import imaplib
import re
import ssl
import time

from .errors import MailboxError


MAX_HEADER_BYTES = 16_384
WAIT_SECONDS = 120
POLL_SECONDS = 5
CLOCK_SKEW_SECONDS = 5  # A fresh portal email was observed one second behind.


def validate_config(config):
    required = {"host", "username", "password", "folder", "sender", "recipient", "subject"}
    if not isinstance(config, dict) or set(config) != required:
        raise MailboxError("imap_config_fields_invalid")
    if any(not isinstance(v, str) or not v or any(c in v for c in "\r\n\x00") for v in config.values()):
        raise MailboxError("imap_config_values_invalid")
    for key in ("sender", "recipient"):
        parsed = getaddresses([config[key]])
        if len(parsed) != 1 or parsed[0][1] != config[key] or "@" not in config[key]:
            raise MailboxError("imap_config_requires_exact_email_addresses")
    if config["subject"].count("{code}") != 1:
        raise MailboxError("imap_subject_requires_one_code_placeholder")
    return config


def quoted(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mailbox_position(client, folder):
    status, _ = client.select(quoted(folder), readonly=True)
    if status != "OK":
        raise MailboxError("imap_folder_unavailable")
    values = []
    for key in ("UIDVALIDITY", "UIDNEXT"):
        _, data = client.response(key)
        if not data or len(data) != 1 or not isinstance(data[0], bytes) or not data[0].isdigit():
            raise MailboxError("imap_position_unavailable")
        value = int(data[0])
        if value < 1:
            raise MailboxError("imap_position_invalid")
        values.append(value)
    return tuple(values)


def close_mailbox(client):
    try:
        client.logout()
    except (imaplib.IMAP4.error, OSError, EOFError):
        pass  # Cleanup must not hide the original outcome.


def open_mailbox(config):
    client = None
    try:
        client = imaplib.IMAP4_SSL(config["host"], 993,
                                  ssl_context=ssl.create_default_context(), timeout=15)
        try:
            status, _ = client.login(config["username"], config["password"])
            if status != "OK":
                raise MailboxError("imap_authentication_failed")
        except imaplib.IMAP4.error:
            raise MailboxError("imap_authentication_failed") from None
        validity, next_uid = mailbox_position(client, config["folder"])
        return client, validity, next_uid
    except (MailboxError, imaplib.IMAP4.error, OSError, EOFError) as exc:
        if client is not None:
            close_mailbox(client)
        if isinstance(exc, MailboxError):
            raise
        raise MailboxError("imap_connection_failed") from None


def extract_code(raw, config, not_before, now):
    """Read only the known subject format, exact addresses and a fresh Date."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if any(len(message.get_all(key, [])) != 1 for key in ("From", "Subject", "Date")):
            return None
        senders = getaddresses(message.get_all("From", []))
        recipients = getaddresses(message.get_all("To", []))
        if len(senders) != 1 or senders[0][1].casefold() != config["sender"].casefold():
            return None
        if config["recipient"].casefold() not in {address.casefold() for _, address in recipients}:
            return None
        prefix, suffix = config["subject"].split("{code}")
        match = re.fullmatch(re.escape(prefix) + r"([0-9]{6})" + re.escape(suffix), str(message["Subject"]))
        if not match:
            return None
        sent = parsedate_to_datetime(message["Date"])
        skew = timedelta(seconds=CLOCK_SKEW_SECONDS)
        if sent.utcoffset() is None or not not_before - skew <= sent <= now + skew:
            return None
        return match[1]
    except (ValueError, TypeError, LookupError, AttributeError):
        return None


def wait_for_code(client, config, validity, first_uid, not_before):
    """Called in a worker thread, with a baseline taken before initiateMFA."""
    deadline = time.monotonic() + WAIT_SECONDS
    examined = set()
    # The subject has a changing code; use its fixed portion for server-side
    # filtering, then require the complete template match in extract_code.
    search_subject = max(config["subject"].split("{code}"), key=len)
    try:
        while time.monotonic() < deadline:
            current_validity, _ = mailbox_position(client, config["folder"])
            if current_validity != validity:
                raise MailboxError("imap_mailbox_identity_changed")
            status, data = client.uid("SEARCH", None, "UID", f"{first_uid}:*",
                                      "FROM", quoted(config["sender"]),
                                      "TO", quoted(config["recipient"]),
                                      "SUBJECT", quoted(search_subject))
            if status != "OK" or not data or not isinstance(data[0], bytes):
                raise MailboxError("imap_search_failed")
            # IMAP ranges can reverse when first_uid exceeds the current last
            # UID, so explicitly exclude old UIDs even after a server search.
            raw_uids = data[0].split()
            if any(not value.isdigit() or int(value) < 1 for value in raw_uids):
                raise MailboxError("imap_search_response_invalid")
            uids = {int(value) for value in raw_uids}
            uids = {uid for uid in uids if uid >= first_uid and uid not in examined}
            if len(uids) > 20:
                raise MailboxError("imap_too_many_matching_messages")
            codes = set()
            for uid in sorted(uids):
                if time.monotonic() >= deadline:
                    raise MailboxError("imap_otp_timeout")
                status, parts = client.uid("FETCH", str(uid),
                    f"(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]<0.{MAX_HEADER_BYTES + 1}>)")
                if status != "OK":
                    raise MailboxError("imap_fetch_failed")
                literals = [part for part in parts if isinstance(part, tuple)]
                if len(literals) != 1:
                    raise MailboxError("imap_message_response_invalid")
                metadata, raw = literals[0]
                returned_uid = re.search(rb"\bUID ([0-9]+)\b", metadata)
                if not returned_uid or int(returned_uid[1]) != uid:
                    raise MailboxError("imap_message_response_invalid")
                if len(raw) > MAX_HEADER_BYTES:
                    raise MailboxError("imap_headers_too_large")
                code = extract_code(raw, config, not_before, datetime.now(timezone.utc))
                examined.add(uid)
                if code is not None:
                    codes.add(code)
            if len(codes) > 1:
                raise MailboxError("imap_otp_ambiguous")
            if time.monotonic() >= deadline:
                raise MailboxError("imap_otp_timeout")
            if codes:
                return codes.pop()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(POLL_SECONDS, remaining))
        raise MailboxError("imap_otp_timeout")
    except (imaplib.IMAP4.error, OSError, EOFError):
        raise MailboxError("imap_poll_failed") from None
