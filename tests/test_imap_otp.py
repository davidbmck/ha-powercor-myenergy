"""Synthetic mail and IMAP responses; no live mailbox is needed."""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
import imaplib
import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from myenergy._imap import MailboxError, extract_code, open_mailbox, wait_for_code
from tools.probe_login import load_imap_config as load_config


START = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
CONFIG = {"host": "imap.example.invalid", "username": "inbox@example.invalid",
          "password": "synthetic-app-password", "folder": "INBOX",
          "sender": "codes@example.invalid", "recipient": "inbox@example.invalid",
          "subject": "{code} is your one-time verification code."}


def message(code="123456", *, sent=START, sender=None):
    mail = EmailMessage()
    mail["From"] = sender or CONFIG["sender"]
    mail["To"] = CONFIG["recipient"]
    mail["Subject"] = CONFIG["subject"].replace("{code}", code)
    mail["Date"] = format_datetime(sent)
    return mail.as_bytes()


def client_fixture():
    client = MagicMock()
    client.login.return_value = ("OK", [])
    client.select.return_value = ("OK", [b"1"])
    client.response.side_effect = lambda key: (key, [b"7" if key == "UIDVALIDITY" else b"10"])
    return client


class IMAPTests(unittest.TestCase):
    def test_private_config_rejects_missing_fields_and_search_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "imap.json"
            path.write_text(json.dumps(CONFIG))
            os.chmod(path, 0o600)
            self.assertEqual(load_config(path), CONFIG)
            path.write_text(json.dumps({**CONFIG, "subject": "code\r\nALL"}))
            with self.assertRaises(MailboxError):
                load_config(path)
            path.write_text(json.dumps(CONFIG))
            os.chmod(path, 0o644)
            with self.assertRaises(MailboxError):
                load_config(path)

    def test_exact_filters_dates_and_subject_code_extraction(self):
        now = START + timedelta(seconds=10)
        self.assertEqual(extract_code(message(), CONFIG, START, now), "123456")
        self.assertEqual(extract_code(message(sent=START - timedelta(seconds=1)), CONFIG, START, now), "123456")
        self.assertIsNone(extract_code(message(sent=START - timedelta(seconds=6)), CONFIG, START, now))
        self.assertIsNone(extract_code(message(sender="other@example.invalid"), CONFIG, START, now))
        self.assertIsNone(extract_code(message(), {**CONFIG, "recipient": "other@example.invalid"}, START, now))
        self.assertIsNone(extract_code(message(), {**CONFIG, "subject": "Other subject"}, START, now))
        self.assertIsNone(extract_code(message("123456 or 234567"), CONFIG, START, now))

    def test_connection_verifies_tls_and_selects_mailbox_read_only_before_challenge(self):
        client = client_fixture()
        with patch("myenergy._imap.imaplib.IMAP4_SSL", return_value=client) as connect:
            self.assertEqual(open_mailbox(CONFIG), (client, 7, 10))
            tls = connect.call_args.kwargs["ssl_context"]
            self.assertTrue(tls.check_hostname)
            self.assertEqual(tls.verify_mode, ssl.CERT_REQUIRED)
            client.select.assert_called_once_with('"INBOX"', readonly=True)
        client.login.side_effect = imaplib.IMAP4.error("synthetic private server detail")
        with patch("myenergy._imap.imaplib.IMAP4_SSL", return_value=client):
            with self.assertRaisesRegex(MailboxError, "^imap_authentication_failed$"):
                open_mailbox(CONFIG)
        client.logout.assert_called_once()

    def test_delayed_delivery_ignores_reversed_uid_range_and_never_marks_seen(self):
        client = client_fixture()
        raw = message()
        searches = iter([b"9", b"9 10"])

        def uid(command, *args):
            if command == "SEARCH":
                return "OK", [next(searches)]
            self.assertEqual(command, "FETCH")
            self.assertEqual(args[0], "10")
            self.assertIn("BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]", args[1])
            return "OK", [(f"1 (UID 10 RFC822.SIZE {len(raw)} BODY[] {{0}}".encode(), raw), b")"]

        client.uid.side_effect = uid
        with patch("myenergy._imap.time.sleep") as sleep, patch("myenergy._imap.datetime") as clock:
            clock.now.return_value = START + timedelta(seconds=10)
            self.assertEqual(wait_for_code(client, CONFIG, 7, 10, START), "123456")
        sleep.assert_called_once()
        self.assertEqual([call.args[0] for call in client.uid.call_args_list], ["SEARCH", "SEARCH", "FETCH"])

    def test_stale_mail_cannot_supply_a_code_and_wait_times_out(self):
        client = client_fixture()
        raw = message(sent=START - timedelta(seconds=10))
        client.uid.side_effect = [
            ("OK", [b"10"]),
            ("OK", [(f"1 (UID 10 RFC822.SIZE {len(raw)} BODY[] {{0}}".encode(), raw)]),
        ]
        with patch("myenergy._imap.time.monotonic", side_effect=[0, 0, 0, 121]), patch("myenergy._imap.datetime") as clock:
            clock.now.return_value = START + timedelta(seconds=10)
            with self.assertRaisesRegex(MailboxError, "^imap_otp_timeout$"):
                wait_for_code(client, CONFIG, 7, 10, START)

    def test_mailbox_reset_stops_without_fetching_or_reusing_old_uid(self):
        client = client_fixture()
        with self.assertRaisesRegex(MailboxError, "^imap_mailbox_identity_changed$"):
            wait_for_code(client, CONFIG, 6, 10, START)
        client.uid.assert_not_called()

    def test_two_different_fresh_codes_stop_instead_of_guessing(self):
        client = client_fixture()
        first, second = message("123456"), message("234567")
        client.uid.side_effect = [
            ("OK", [b"10 11"]),
            ("OK", [(b"1 (UID 10 BODY[HEADER.FIELDS]", first)]),
            ("OK", [(b"2 (UID 11 BODY[HEADER.FIELDS]", second)]),
        ]
        with patch("myenergy._imap.datetime") as clock:
            clock.now.return_value = START + timedelta(seconds=10)
            with self.assertRaisesRegex(MailboxError, "^imap_otp_ambiguous$"):
                wait_for_code(client, CONFIG, 7, 10, START)


if __name__ == "__main__":
    unittest.main()
